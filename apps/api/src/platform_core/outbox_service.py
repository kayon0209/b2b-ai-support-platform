"""Outbox service: enqueue within caller's transaction, relay to broker.

enqueue() joins the CURRENT session/transaction — that is the whole point.
relay_pending() is called by a worker loop with FOR UPDATE SKIP LOCKED so
multiple relay workers never double-send.
"""

import time
import uuid
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from platform_core.outbox import OutboxEvent, OutboxStatus


async def enqueue(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    event_type: str,
    aggregate_type: str,
    aggregate_id: str,
    payload: dict[str, Any],
    event_id: uuid.UUID | None = None,
    trace_id: str | None = None,
) -> uuid.UUID:
    """Write an outbox row in the caller's open transaction.

    COMMIT is left to the caller (session_scope / request handler). If the
    surrounding transaction rolls back, the event vanishes with it — the
    invariant that makes business state and event atomic.
    """
    eid = event_id or uuid.uuid4()
    stmt = (
        pg_insert(OutboxEvent)
        .values(
            tenant_id=tenant_id,
            event_id=eid,
            event_type=event_type,
            event_version=1,
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            payload=payload,
            status=OutboxStatus.QUEUED.value,
            created_at=int(time.time()),
            trace_id=trace_id or "",
        )
        .on_conflict_do_nothing(index_elements=["event_id"])
        .returning(OutboxEvent.id)
    )
    await session.execute(stmt)
    return eid


async def claim_pending(session: AsyncSession, *, batch: int = 50) -> list[OutboxEvent]:
    """Claim queued rows with SKIP LOCKED (safe for concurrent relays).

    Claims mark rows 'sent-in-flight' by bumping attempts; actual SENT is
    set by mark_sent after successful publish.
    """
    stmt = (
        select(OutboxEvent)
        .where(OutboxEvent.status == OutboxStatus.QUEUED.value)
        .order_by(OutboxEvent.id)
        .limit(batch)
        .with_for_update(skip_locked=True)
    )
    rows = (await session.execute(stmt)).scalars().all()
    if rows:
        ids = [r.id for r in rows]
        await session.execute(
            update(OutboxEvent)
            .where(OutboxEvent.id.in_(ids))
            .values(attempts=OutboxEvent.attempts + 1)
        )
    return list(rows)


async def mark_sent(session: AsyncSession, row_id: uuid.UUID) -> None:
    await session.execute(
        update(OutboxEvent)
        .where(OutboxEvent.id == row_id)
        .values(status=OutboxStatus.SENT.value, published_at=int(time.time()))
    )


async def mark_failed(session: AsyncSession, row_id: uuid.UUID, error: str) -> None:
    await session.execute(
        update(OutboxEvent).where(OutboxEvent.id == row_id).values(last_error=error[:2000])
    )
