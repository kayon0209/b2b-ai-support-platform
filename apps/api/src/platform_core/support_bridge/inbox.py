"""InboxEvent persistence with delivery deduplication (ticket 6).

The webhook endpoint persists the InboxEvent row and returns 200 in the
same request; the enqueue decision is recorded on the row. Duplicate
delivery IDs hit the UNIQUE constraint and return success without
reprocessing (docs/api-contracts.md rule 4).
"""

import time
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from platform_core.support_bridge.minimize import minimize_chatwoot_payload, payload_hash
from platform_core.support_bridge.models import InboxEvent, InboxEventStatus


class IngestResult:
    __slots__ = ("event_id", "duplicate", "tenant_id")

    def __init__(self, tenant_id: uuid.UUID, event_id: uuid.UUID | None, duplicate: bool) -> None:
        self.tenant_id = tenant_id
        self.event_id = event_id
        self.duplicate = duplicate


async def persist_inbox_event(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    delivery_id: str,
    event_type: str,
    raw_body: bytes,
    raw_payload: dict[str, Any],
) -> IngestResult:
    """Insert-on-conflict-do-nothing dedup by (tenant_id, delivery_id).

    Returns duplicate=True when this delivery was already persisted. The
    caller never reprocesses; the original row keeps its state.
    """
    minimized = minimize_chatwoot_payload(event_type, raw_payload)
    stmt = (
        pg_insert(InboxEvent)
        .values(
            tenant_id=tenant_id,
            delivery_id=delivery_id,
            event_type=event_type,
            payload_hash=payload_hash(raw_body),
            minimized_payload=minimized,
            status=InboxEventStatus.RECEIVED.value,
            received_at=int(time.time()),
        )
        .on_conflict_do_nothing(constraint="uq_inbox_delivery")
        .returning(InboxEvent.id)
    )
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row is None:
        return IngestResult(tenant_id=tenant_id, event_id=None, duplicate=True)
    return IngestResult(tenant_id=tenant_id, event_id=row, duplicate=False)


async def mark_processing(session: AsyncSession, event_id: uuid.UUID) -> None:
    await session.execute(
        pg_insert(InboxEvent)
        .values()  # placeholder to satisfy typing; real update below
        .on_conflict_do_nothing(constraint="uq_inbox_delivery")
    )
    # Simple status transition; idempotent.
    from sqlalchemy import update

    await session.execute(
        update(InboxEvent)
        .where(InboxEvent.id == event_id, InboxEvent.status == InboxEventStatus.RECEIVED.value)
        .values(status=InboxEventStatus.PROCESSING.value)
    )


async def get_event(
    session: AsyncSession, tenant_id: uuid.UUID, delivery_id: str
) -> InboxEvent | None:
    result = await session.execute(
        select(InboxEvent).where(
            InboxEvent.tenant_id == tenant_id, InboxEvent.delivery_id == delivery_id
        )
    )
    return result.scalar_one_or_none()
