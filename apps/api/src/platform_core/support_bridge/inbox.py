"""InboxEvent persistence with delivery deduplication (ticket 6).

The webhook endpoint persists the InboxEvent row and returns 200 in the
same request; the enqueue decision is recorded on the row. Duplicate
delivery IDs hit the UNIQUE constraint and return success without
reprocessing (docs/api-contracts.md rule 4).
"""

import time
import uuid
from typing import Any

from sqlalchemy import and_, or_, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from platform_core.support_bridge.conversation_ref import conversation_ref_for
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
    payload = dict(minimized_payload)
    conversation_ref = _conversation_ref(tenant_id, payload)
    raw_message_type = payload.get("message_type")
    is_incoming_message = (
        event_type == "message_created"
        and isinstance(raw_message_type, str)
        and raw_message_type.strip().lower() == "incoming"
    )
    if is_incoming_message and conversation_ref is not None:
        # Normalize before storing so the claim query and the send gate share
        # one tenant-scoped conversation key across customer and channel APIs.
        payload["conversation_ref"] = str(conversation_ref)
        payload["message_type"] = "incoming"
        await lock_conversation_inbox(
            session, tenant_id=tenant_id, conversation_ref_id=conversation_ref
        )

    stmt = (
        pg_insert(InboxEvent)
        .values(
            tenant_id=tenant_id,
            delivery_id=delivery_id,
            event_type=event_type,
            payload_hash=payload_hash(raw_body),
            minimized_payload=payload,
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


def _conversation_ref(tenant_id: uuid.UUID, payload: dict[str, Any]) -> uuid.UUID | None:
    """Resolve the same canonical reference the inbox worker will consume."""
    explicit = payload.get("conversation_ref")
    if isinstance(explicit, str) and explicit.strip():
        try:
            return uuid.UUID(explicit)
        except ValueError:
            return None
    external = payload.get("conversation_id")
    if not isinstance(external, str) or not external.strip():
        return None
    try:
        return conversation_ref_for(tenant_id, external)
    except ValueError:
        return None


async def lock_conversation_inbox(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    conversation_ref_id: uuid.UUID,
) -> None:
    """Serialize accepting an inbound message with the agent's pre-send gate."""
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"inbox-conversation:{tenant_id}:{conversation_ref_id}"},
    )


async def has_newer_incoming_message(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    conversation_ref_id: uuid.UUID,
    source_event_id: uuid.UUID,
) -> bool:
    """Whether a later customer message superseded this in-flight result.

    The advisory lock linearizes this read with `persist_inbox_event`: if the
    send gate wins, the next message is accepted after the send; if ingress
    wins, the result sees it and is suppressed before any outbound effect.
    """
    await lock_conversation_inbox(
        session,
        tenant_id=tenant_id,
        conversation_ref_id=conversation_ref_id,
    )
    current = (
        await session.execute(
            select(
                InboxEvent.received_at,
                InboxEvent.status,
                InboxEvent.minimized_payload,
            ).where(
                InboxEvent.tenant_id == tenant_id,
                InboxEvent.id == source_event_id,
            )
        )
    ).one_or_none()
    if current is None or current.status != InboxEventStatus.PROCESSING.value:
        return True

    newer = aliased(InboxEvent)
    same_conversation_conditions = [
        newer.minimized_payload["conversation_ref"].as_string() == str(conversation_ref_id)
    ]
    current_payload = (
        current.minimized_payload if isinstance(current.minimized_payload, dict) else {}
    )
    current_external_ref = current_payload.get("conversation_id")
    if isinstance(current_external_ref, str) and current_external_ref:
        # Compatibility with older channel rows that predate canonical-ref
        # normalization. New rows always carry `conversation_ref`.
        same_conversation_conditions.append(
            and_(
                newer.minimized_payload["conversation_ref"].as_string().is_(None),
                newer.minimized_payload["conversation_id"].as_string() == current_external_ref,
            )
        )
    same_conversation = or_(*same_conversation_conditions)
    newer_id = (
        await session.execute(
            select(newer.id)
            .where(
                newer.tenant_id == tenant_id,
                newer.event_type == "message_created",
                newer.minimized_payload["message_type"].as_string() == "incoming",
                newer.status.in_(
                    (InboxEventStatus.RECEIVED.value, InboxEventStatus.PROCESSING.value)
                ),
                same_conversation,
                or_(
                    newer.received_at > current.received_at,
                    and_(
                        newer.received_at == current.received_at,
                        newer.id > source_event_id,
                    ),
                ),
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    return newer_id is not None
