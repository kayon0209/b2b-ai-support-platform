"""Dead-letter records: exhausted retries become visible work, not silence.

The defect this closes
----------------------
`DeadLetterItem` existed as a model with **no producer**: the only code that
touched the table was the retention sweep deleting resolved rows. So a
connector operation that failed on every bounded retry - which is the exact
case `sdk.http_request` reports as `ambiguous=True` after exhausting the
retry budget - left nothing behind except a `ToolExecution` row in a failed
state. Nothing listed it, nothing aggregated it, and nobody was told.

What is stored, and what deliberately is not
--------------------------------------------
`DeadLetterItem` holds a **digest** of the operation, never the operation:
`operation_digest` is `"{tool_name}:{sha256(canonical_json)[:32]}"`. Storing
the arguments would put customer-derived values into a table that is read by
an operational endpoint and copied into backups, for a record whose purpose
is "something needs a human" rather than "here is what to retry verbatim".
The digest still answers the operator's real question - *is this the same
operation failing repeatedly?* - by matching rows against each other.

Consequence worth stating plainly: **automatic replay is not possible from
this row alone**, because the payload is not here. Re-execution needs the
originating `ToolProposal` (which does hold the sanitized input) and, when
the outcome was ambiguous, an external postcondition check before any retry,
because the first attempt may in fact have landed. That is an operator
decision, not a loop, which is why this module exposes visibility and
resolution rather than a replay endpoint.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from platform_core.audit import service as audit_service
from platform_core.identity.tenant_context import TenantContext
from platform_core.integrations.models import DeadLetterItem
from platform_core.integrations.sdk import AUTH_FAILURE_CODES

PENDING = "pending"
RESOLVED = "resolved"

AUDIT_ACTION = "dead_letter.resolved"

# The only connector failure that is not a dead letter.
#
# An auth rejection is a *reauthorization* problem with its own state
# (Connector.status = NEEDS_REAUTH), its own event and its own operator
# action. Recording it here as well would put the same incident in two
# queues, and the dead-letter queue would fill with rows whose correct
# disposition is "rotate the credential", not "retry the operation".
NOT_DEAD_LETTER_CODES = AUTH_FAILURE_CODES

# Cap the stored detail. A provider's error body can be large and can echo
# the request; `sdk.http_request` already truncates to a classified code, and
# this bounds whatever a future adapter passes in.
MAX_DETAIL = 2000


def operation_digest(*, tool_name: str, parameters: dict[str, Any]) -> str:
    """A stable, payload-free fingerprint of one operation.

    Stable across retries of the same arguments, so repeated failures of one
    operation group together, and useless as a data exfiltration channel
    because the parameters are hashed rather than stored.

    `sort_keys` matters: without it, a dict that serialises in a different
    insertion order would produce a different digest and the grouping this
    exists for would silently stop working.
    """
    canonical = json.dumps(parameters, sort_keys=True, separators=(",", ":"), default=str)
    digest = hashlib.sha256(canonical.encode()).hexdigest()[:32]
    return f"{tool_name}:{digest}"[:127]


def should_record(*, error_code: str | None, ambiguous: bool) -> bool:
    """Whether a failed connector outcome belongs in the dead-letter queue.

    Recorded: every failure other than an auth rejection, including
    `ambiguous` transport failures where the write may have landed. Those are
    precisely the rows a human must look at, because the platform cannot
    decide on its own whether retrying is safe.

    Not recorded: auth rejections (they have NEEDS_REAUTH), and successes
    (the caller does not route those here at all).
    """
    if ambiguous:
        # Ambiguity outranks the error code: `CONNECTOR_UNKNOWN` with
        # ambiguous=True still means "we do not know what happened".
        return True
    if not error_code:
        return False
    return error_code not in NOT_DEAD_LETTER_CODES


@dataclass(frozen=True)
class DeadLetterRef:
    """What the producer reports back, safe to log and assert on."""

    item_id: uuid.UUID
    operation_digest: str
    error_code: str
    ambiguous: bool


async def record(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    connector_id: uuid.UUID | None,
    resource_type: str,
    tool_name: str,
    parameters: dict[str, Any],
    error_code: str,
    error_detail: str | None = None,
    attempts: int = 0,
    ambiguous: bool = False,
    now: int | None = None,
) -> DeadLetterRef:
    """Write one dead-letter row in the caller's transaction.

    `created_at` is set explicitly because the column is NOT NULL with no
    default: SQLAlchemy would emit an explicit NULL and the insert would fail
    on the constraint. (`server_default` is deliberately not added here -
    this module is the only producer, so an explicit value is clearer than
    one the database supplies.)
    """
    digest = operation_digest(tool_name=tool_name, parameters=parameters)
    detail = (error_detail or "")[:MAX_DETAIL] or None
    if ambiguous:
        # The operator's first question about an ambiguous row is "did it
        # land?", and that is not answerable from the error code. Say so on
        # the record rather than making them infer it from a code table.
        prefix = "AMBIGUOUS_OUTCOME"
        detail = f"{prefix}: {detail}" if detail else prefix

    item = DeadLetterItem(
        tenant_id=tenant_id,
        connector_id=connector_id,
        resource_type=resource_type,
        operation=tool_name[:63],
        operation_digest=digest,
        error_code=error_code[:63],
        error_detail=detail,
        attempts=max(0, attempts),
        status=PENDING,
        created_at=now if now is not None else int(time.time()),
    )
    session.add(item)
    await session.flush()
    return DeadLetterRef(
        item_id=item.id,
        operation_digest=digest,
        error_code=error_code,
        ambiguous=ambiguous,
    )


async def list_pending(
    session: AsyncSession, *, tenant_id: uuid.UUID, status: str | None = None, limit: int = 100
) -> list[DeadLetterItem]:
    """Rows for the caller's tenant, newest first.

    Bounded by `limit`: an operational list endpoint that can return an
    unbounded set is a memory problem on the server and a useless page in the
    UI.
    """
    stmt = select(DeadLetterItem).where(DeadLetterItem.tenant_id == tenant_id)
    if status is not None:
        stmt = stmt.where(DeadLetterItem.status == status)
    stmt = stmt.order_by(DeadLetterItem.created_at.desc(), DeadLetterItem.id).limit(min(limit, 500))
    return list((await session.execute(stmt)).scalars().all())


async def resolve(
    session: AsyncSession,
    item: DeadLetterItem,
    *,
    reason_code: str,
    ctx: TenantContext,
    now: int | None = None,
    trace_id: str | None = None,
) -> bool:
    """Mark a row resolved. Returns whether this call changed it.

    Idempotent: resolving an already-resolved row is a no-op rather than an
    error, because two operators clicking the same button is not an incident.
    Returns False so the caller can report "already resolved" without writing
    a second audit event for a transition that did not happen.

    Resolving is an attestation that a human handled it. It is deliberately
    not "the retry succeeded": the platform cannot verify that from this row,
    and pretending otherwise would mark work done on the strength of a
    response code.
    """
    if item.status == RESOLVED:
        return False

    item.status = RESOLVED
    item.resolved_at = now if now is not None else int(time.time())
    await audit_service.record(
        session,
        ctx=ctx,
        action=AUDIT_ACTION,
        resource_type="dead_letter_item",
        resource_id=item.id,
        decision="resolved",
        reason_code=reason_code[:63],
        after={"operation": item.operation, "error_code": item.error_code},
        trace_id=trace_id,
    )
    await session.flush()
    return True
