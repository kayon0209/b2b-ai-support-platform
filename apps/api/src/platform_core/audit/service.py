"""Audit service (ticket 24): one append-only write path for AuditEvent.

Every sensitive command (case commands, lease transfers, knowledge
publication, tool executions, denied authorization attempts) records an
audit event in the SAME transaction as the state change. No update or
delete APIs exist for audit rows.
"""

import hashlib
import json
import time
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from observability import redact_value
from platform_core.audit.models import AuditEvent
from platform_core.identity.tenant_context import TenantContext


def _hash_dict(payload: dict[str, Any] | None) -> str | None:
    if payload is None:
        return None
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


async def record(
    session: AsyncSession,
    *,
    ctx: TenantContext,
    action: str,
    resource_type: str,
    resource_id: uuid.UUID | None = None,
    decision: str = "completed",
    reason_code: str = "OK",
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
    trace_id: str | None = None,
) -> uuid.UUID:
    """Append one audit event. Caller owns the transaction.

    `before` / `after` are **hashed, never stored** - the trail records that a
    value changed and what it hashed to, so an audit row cannot become a copy of
    the secrets anyone edited.

    `metadata` is the narrow exception, and it exists because some events'
    *parameters* are the content: "which window did this export cover, and how
    many rows came back" is what an investigator reads, and a hash of it answers
    nothing. It is redacted by key and bounded in size, and it refuses rather
    than truncates - a cap that silently drops fields produces an audit row that
    looks complete and is not.
    """
    occurred = int(time.time())
    event = AuditEvent(
        tenant_id=ctx.tenant_id,
        occurred_at=occurred,
        actor_type=ctx.actor_kind,
        actor_id=ctx.actor_id,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        decision=decision,
        reason_code=reason_code,
        trace_id=trace_id or "",
        before_hash=_hash_dict(before),
        after_hash=_hash_dict(after),
        metadata_redacted=minimise_metadata(metadata),
    )
    session.add(event)
    await session.flush()
    return event.id


# 4 KB. Generous for parameters (a window, a section list, a few counts) and
# small enough that anything larger is a payload arriving where a payload does
# not belong.
MAX_METADATA_BYTES = 4096


def minimise_metadata(metadata: dict[str, Any] | None) -> dict[str, Any]:
    """Redact by key, then refuse anything too large to be parameters.

    Returning `{}` for a missing dict keeps every existing caller's rows
    byte-identical, so adding the parameter changes nothing that was already
    being written.
    """
    if not metadata:
        return {}
    redacted = redact_value(metadata)
    if not isinstance(redacted, dict):  # pragma: no cover - redact_value is total
        raise ValueError("audit metadata must be a mapping")
    encoded = json.dumps(redacted, sort_keys=True, default=str)
    if len(encoded.encode("utf-8")) > MAX_METADATA_BYTES:
        raise ValueError(
            f"audit metadata exceeds {MAX_METADATA_BYTES} bytes; "
            "before/after are for payloads, and those are hashed, not stored"
        )
    return redacted


async def export_events(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    since: int,
    until: int,
    limit: int,
) -> tuple[list[dict[str, Any]], bool]:
    """A bounded, ordered extract of this tenant's audit events.

    Returns `(rows, truncated)`. `truncated` is returned rather than inferred
    from `len(rows) == limit`, because a page that happens to end exactly at the
    bound is not evidence of more rows - and a compliance extract that silently
    stops is worse than one that says it did.

    Deliberately a projection rather than `select(AuditEvent)`: the caller is a
    different module (compliance), and handing it mapped instances would invite
    it to reach into columns this function does not curate. The projection is
    also where `before_hash` / `after_hash` are named, which is what documents
    that the extract carries hashes and never the payloads they hash.
    """
    # One extra row answers "is there more" without a second COUNT over the
    # same predicate.
    rows = (
        await session.execute(
            select(AuditEvent)
            .where(
                AuditEvent.tenant_id == tenant_id,
                AuditEvent.occurred_at >= since,
                AuditEvent.occurred_at <= until,
            )
            .order_by(AuditEvent.occurred_at, AuditEvent.id)
            .limit(limit + 1)
        )
    ).scalars()
    events = list(rows)
    truncated = len(events) > limit
    return [_audit_projection(e) for e in events[:limit]], truncated


def _audit_projection(event: AuditEvent) -> dict[str, Any]:
    return {
        "event_id": str(event.id),
        "occurred_at": event.occurred_at,
        "actor_type": event.actor_type,
        "actor_id": str(event.actor_id) if event.actor_id else None,
        "action": event.action,
        "resource_type": event.resource_type,
        "resource_id": str(event.resource_id) if event.resource_id else None,
        "decision": event.decision,
        "reason_code": event.reason_code,
        "trace_id": event.trace_id,
        # Hashes, not payloads: the audit trail records that a value changed and
        # what it hashed to, never the value. An extract that carried the
        # payloads would be a copy of every secret anybody ever edited.
        "before_hash": event.before_hash,
        "after_hash": event.after_hash,
        "metadata": event.metadata_redacted,
    }
