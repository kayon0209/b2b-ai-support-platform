"""InboxEvent persistence with delivery deduplication (ticket 6).

The webhook endpoint persists the InboxEvent row and returns 200 in the
same request; the enqueue decision is recorded on the row. Duplicate
delivery IDs hit the UNIQUE constraint and return success without
reprocessing (docs/api-contracts.md rule 4).
"""

import time
import uuid
from typing import Any

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from platform_core.support_bridge.minimize import payload_hash
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
    minimized_payload: dict[str, Any],
) -> IngestResult:
    """Insert-on-conflict-do-nothing dedup by (tenant_id, delivery_id).

    Returns duplicate=True when this delivery was already persisted. The
    caller never reprocesses; the original row keeps its state.

    `minimized_payload` is **required and explicit**: every producer already
    knows what its own payload means, so it hands over the routing fields the
    consumer reads rather than a raw body this function would have to guess at.
    A generic extractor run over a payload the caller understood is how a
    silently empty row gets stored, and a silent empty row is worse than an
    error.
    """
    stmt = (
        pg_insert(InboxEvent)
        .values(
            tenant_id=tenant_id,
            delivery_id=delivery_id,
            event_type=event_type,
            payload_hash=payload_hash(raw_body),
            minimized_payload=minimized_payload,
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
