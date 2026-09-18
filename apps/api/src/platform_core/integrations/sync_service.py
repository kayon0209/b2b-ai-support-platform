"""Connector sync: resumable positions and one bounded sync step.

The defect this closes
----------------------
`SyncCursor` was declared and **never referenced by any code**, and
`ConnectorAdapter.fetch` had **zero callers**. Between them that meant a
connector could only ever be read by asking for one named record
(`get_account`, `search_issues`): there was no way to walk a resource, and
therefore nothing that needed to remember where the last walk stopped.

What a cursor is, and what it deliberately is not
-------------------------------------------------
A cursor is an **opaque token supplied by the provider** (Jira's
`nextPageToken`), stored verbatim. This module never parses it, never
compares it, and never builds one. That restraint is the design: a token's
meaning belongs to the provider, and the moment the platform starts
interpreting it, a provider-side change becomes a data-loss bug here.

`watermark` is separate and does mean something to us: the epoch seconds of
the sync that wrote it. It answers "how stale is this position?" without
understanding the token.

Why the position advances only on success
-----------------------------------------
A failed page must not move the cursor. Advancing first and fetching second
would mean a single provider error silently skips a page of records forever -
the sync would look healthy and quietly lose data, which is the worst of both
outcomes.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from platform_core.audit import service as audit_service
from platform_core.identity.tenant_context import TenantContext
from platform_core.integrations.models import SyncCursor

AUDIT_ACTION = "connector.sync_completed"

# How many records one sync step may return. Bounded because the caller is an
# HTTP request: an unbounded page is a memory problem on the server and an
# unusable response for the operator.
MAX_PAGE_SIZE = 200


@dataclass
class SyncOutcome:
    """Result of one sync step, safe to log and to assert on."""

    connector_id: uuid.UUID
    resource_type: str
    fetched: int = 0
    cursor: str | None = None
    # True when the provider stopped offering pages. Reported explicitly so a
    # caller can tell "caught up" from "there is more to pull" without
    # inspecting the token.
    complete: bool = False
    records: list[Any] = field(default_factory=list)
    error_code: str = ""

    @property
    def ok(self) -> bool:
        return not self.error_code


async def load_cursor(
    session: AsyncSession, *, connector_id: uuid.UUID, resource_type: str
) -> SyncCursor | None:
    """The stored position, or None when this resource has never been synced."""
    return (
        await session.execute(
            select(SyncCursor).where(
                SyncCursor.connector_id == connector_id,
                SyncCursor.resource_type == resource_type,
            )
        )
    ).scalar_one_or_none()


async def advance_cursor(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    connector_id: uuid.UUID,
    resource_type: str,
    cursor: str | None,
    now: int | None = None,
) -> SyncCursor:
    """Store the position the provider handed back.

    Upsert rather than insert-or-fail, because `UNIQUE (connector_id,
    resource_type)` means a second sync of the same resource is the normal
    case, not an error. `updated_at` is written explicitly: the column is NOT
    NULL with a `0` server default, and an explicit value is what separates
    "synced at this time" from "placeholder zero".
    """
    row = await load_cursor(session, connector_id=connector_id, resource_type=resource_type)
    stamp = now if now is not None else int(time.time())
    if row is None:
        row = SyncCursor(
            tenant_id=tenant_id,
            connector_id=connector_id,
            resource_type=resource_type[:63],
            cursor=cursor,
            watermark=stamp,
            updated_at=stamp,
        )
        session.add(row)
    else:
        row.cursor = cursor
        row.watermark = stamp
        row.updated_at = stamp
    await session.flush()
    return row


async def reset_cursor(
    session: AsyncSession, *, connector_id: uuid.UUID, resource_type: str
) -> bool:
    """Forget the position so the next sync starts from the beginning.

    Needed because a cursor is only valid for the query that produced it: if
    an operator changes the connector's `jql`, the stored token refers to a
    page of a different result set and resuming from it would silently skip
    rows. Returns whether anything was reset.
    """
    row = await load_cursor(session, connector_id=connector_id, resource_type=resource_type)
    if row is None:
        return False
    await session.delete(row)
    await session.flush()
    return True


async def sync_once(
    session: AsyncSession,
    *,
    connector: Any,
    adapter: Any,
    resource_type: str,
    ctx: TenantContext,
    page_size: int = 50,
    reset: bool = False,
    trace_id: str | None = None,
    now: int | None = None,
) -> SyncOutcome:
    """Run one bounded sync step for a connector and record the new position.

    The adapter is passed in rather than built here: constructing it needs the
    credential resolver and the provider factory registry, and a sync service
    that knew about those would be a second composition root.

    Returns a `SyncOutcome` whose `error_code` is set when the step failed.
    A failed step writes nothing - see the module docstring.
    """
    limit = max(1, min(page_size, MAX_PAGE_SIZE))

    if reset:
        await reset_cursor(session, connector_id=connector.id, resource_type=resource_type)

    stored = await load_cursor(session, connector_id=connector.id, resource_type=resource_type)
    position = stored.cursor if stored is not None else None

    try:
        records, next_cursor = await adapter.fetch(resource_type, position)
    except NotImplementedError:
        # A lookup-only connector (the CRM pilot) has no sync surface. That is
        # a capability gap, not an outage, so it is named as such and no
        # cursor is written.
        return SyncOutcome(
            connector_id=connector.id,
            resource_type=resource_type,
            error_code="CONNECTOR_SYNC_UNSUPPORTED",
        )
    except Exception as exc:  # noqa: BLE001 - classified below
        # The cursor is left exactly where it was: a failed page must not move
        # the position, or one provider error skips that page forever.
        return SyncOutcome(
            connector_id=connector.id,
            resource_type=resource_type,
            error_code=f"CONNECTOR_SYNC_FAILED:{type(exc).__name__}",
        )

    bounded = list(records[:limit])
    # A provider that returns no token has reached the end of the result set.
    # Storing None is meaningful: the next sync restarts the walk rather than
    # resuming from a token that no longer addresses anything.
    await advance_cursor(
        session,
        tenant_id=connector.tenant_id,
        connector_id=connector.id,
        resource_type=resource_type,
        cursor=next_cursor,
        now=now,
    )

    outcome = SyncOutcome(
        connector_id=connector.id,
        resource_type=resource_type,
        fetched=len(bounded),
        cursor=next_cursor,
        complete=next_cursor is None,
        records=bounded,
    )
    await audit_service.record(
        session,
        ctx=ctx,
        action=AUDIT_ACTION,
        resource_type="connector",
        resource_id=connector.id,
        decision="completed",
        reason_code="OK",
        # No records, no tokens: an audit trail records that a sync ran and
        # how much it saw, not what was in it.
        after={
            "resource_type": resource_type,
            "fetched": outcome.fetched,
            "complete": outcome.complete,
            "reset": reset,
        },
        trace_id=trace_id,
    )
    return outcome
