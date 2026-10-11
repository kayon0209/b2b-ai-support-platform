"""Outbox service: enqueue within caller's transaction, relay to broker.

enqueue() joins the CURRENT session/transaction — that is the whole point.
relay_pending() is called by a worker loop with FOR UPDATE SKIP LOCKED so
multiple relay workers never double-send.
"""

import time
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from platform_core.outbox import OutboxEvent, OutboxStatus


@dataclass(frozen=True)
class OutboxReceipt:
    """Minimal application view of an enqueued event."""

    event_id: uuid.UUID
    status: str
    payload: dict[str, Any]


@dataclass(frozen=True)
class OutboxClaim:
    """Queue metadata only; the tenant-bound handler loads the payload later."""

    id: uuid.UUID
    event_id: uuid.UUID
    tenant_id: uuid.UUID
    event_type: str
    processing_token: uuid.UUID
    attempts: int
    max_attempts: int
    deadline_at: int
    external_attempt_limit: int


async def get_receipt(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    event_id: uuid.UUID,
) -> OutboxReceipt | None:
    row = (
        await session.execute(
            select(OutboxEvent).where(
                OutboxEvent.tenant_id == tenant_id,
                OutboxEvent.event_id == event_id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    return OutboxReceipt(event_id=row.event_id, status=str(row.status), payload=row.payload or {})


async def latest_status_for_aggregate(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    event_type: str,
    aggregate_id: str,
) -> str | None:
    status = (
        await session.execute(
            select(OutboxEvent.status)
            .where(
                OutboxEvent.tenant_id == tenant_id,
                OutboxEvent.event_type == event_type,
                OutboxEvent.aggregate_id == aggregate_id,
            )
            .order_by(OutboxEvent.created_at.desc(), OutboxEvent.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return str(status) if status is not None else None


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


async def enqueue_once(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    event_type: str,
    aggregate_type: str,
    aggregate_id: str,
    payload: dict[str, Any],
    event_id: uuid.UUID,
    trace_id: str | None = None,
) -> bool:
    """Insert one event and report whether this call created it."""
    stmt = (
        pg_insert(OutboxEvent)
        .values(
            tenant_id=tenant_id,
            event_id=event_id,
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
    inserted_id = (await session.execute(stmt)).scalar_one_or_none()
    return inserted_id is not None


async def claim_pending(
    session: AsyncSession,
    *,
    batch: int = 50,
    exclude_event_types: tuple[str, ...] = (),
    max_attempts: int | None = None,
) -> list[OutboxClaim]:
    """Claim queued rows with SKIP LOCKED (safe for concurrent relays).

    Claims mark rows 'sent-in-flight' by bumping attempts; actual SENT is
    set by mark_sent after successful publish.
    """
    from platform_core.config import get_settings

    settings = get_settings()
    delivery_limit = max_attempts if max_attempts is not None else 5
    now = int(time.time())
    stmt = select(
        OutboxEvent.id,
        OutboxEvent.event_id,
        OutboxEvent.tenant_id,
        OutboxEvent.event_type,
        OutboxEvent.attempts,
        OutboxEvent.first_attempt_at,
        OutboxEvent.deadline_at,
        OutboxEvent.external_attempt_limit,
    ).where(OutboxEvent.status == OutboxStatus.QUEUED.value)
    if exclude_event_types:
        stmt = stmt.where(OutboxEvent.event_type.not_in(exclude_event_types))
    stmt = stmt.where(
        OutboxEvent.attempts < delivery_limit,
        or_(OutboxEvent.deadline_at.is_(None), OutboxEvent.deadline_at > now),
    )
    stmt = stmt.order_by(OutboxEvent.id).limit(batch).with_for_update(skip_locked=True)
    rows = (await session.execute(stmt)).all()
    claims: list[OutboxClaim] = []
    for row in rows:
        token = uuid.uuid4()
        first_attempt_at = int(row.first_attempt_at or now)
        deadline_at = int(
            row.deadline_at or first_attempt_at + settings.outbox_job_deadline_seconds
        )
        external_attempt_limit = int(
            row.external_attempt_limit or settings.outbox_event_max_external_attempts
        )
        await session.execute(
            update(OutboxEvent)
            .where(
                OutboxEvent.id == row.id,
                OutboxEvent.status == OutboxStatus.QUEUED.value,
            )
            .values(
                status=OutboxStatus.PROCESSING.value,
                processing_started_at=now,
                processing_token=token,
                first_attempt_at=func.coalesce(OutboxEvent.first_attempt_at, now),
                deadline_at=func.coalesce(OutboxEvent.deadline_at, deadline_at),
                external_attempt_limit=func.coalesce(
                    OutboxEvent.external_attempt_limit,
                    settings.outbox_event_max_external_attempts,
                ),
                attempts=OutboxEvent.attempts + 1,
            )
        )
        claims.append(
            OutboxClaim(
                id=row.id,
                event_id=row.event_id,
                tenant_id=row.tenant_id,
                event_type=str(row.event_type),
                processing_token=token,
                attempts=int(row.attempts) + 1,
                max_attempts=delivery_limit,
                deadline_at=deadline_at,
                external_attempt_limit=external_attempt_limit,
            )
        )
    return claims


async def mark_sent(
    session: AsyncSession,
    row_id: uuid.UUID,
    *,
    processing_token: uuid.UUID | None = None,
) -> bool:
    stmt = (
        update(OutboxEvent)
        .where(OutboxEvent.id == row_id)
        .values(
            status=OutboxStatus.SENT.value,
            published_at=int(time.time()),
            processing_started_at=None,
            processing_token=None,
        )
    )
    if processing_token is not None:
        stmt = stmt.where(
            OutboxEvent.status == OutboxStatus.PROCESSING.value,
            OutboxEvent.processing_token == processing_token,
        )
    result = await session.execute(stmt)
    return bool(getattr(result, "rowcount", 0))


async def mark_failed(
    session: AsyncSession,
    row_id: uuid.UUID,
    error: str,
    *,
    processing_token: uuid.UUID | None = None,
    terminal: bool = False,
) -> bool:
    stmt = (
        update(OutboxEvent)
        .where(OutboxEvent.id == row_id)
        .values(
            status=OutboxStatus.FAILED.value if terminal else OutboxStatus.QUEUED.value,
            last_error=error[:2000],
            processing_started_at=None,
            processing_token=None,
        )
    )
    if processing_token is not None:
        stmt = stmt.where(
            OutboxEvent.status == OutboxStatus.PROCESSING.value,
            OutboxEvent.processing_token == processing_token,
        )
    result = await session.execute(stmt)
    return bool(getattr(result, "rowcount", 0))


async def reclaim_stale_claims(
    session: AsyncSession,
    *,
    cutoff: int,
    max_attempts: int,
    retry_safe_event_types: frozenset[str],
    exclude_event_types: tuple[str, ...] = (),
) -> tuple[int, int]:
    """Reclaim safe rows and terminally park ambiguous side-effect rows.

    The queue role reads only metadata. A customer-visible reply is not
    automatically replayed after a crashed send because its provider may have
    accepted the message before the process died.
    """
    now = int(time.time())
    expired_queued = update(OutboxEvent).where(
        OutboxEvent.status == OutboxStatus.QUEUED.value,
        OutboxEvent.deadline_at <= now,
    )
    if exclude_event_types:
        expired_queued = expired_queued.where(OutboxEvent.event_type.not_in(exclude_event_types))
    expired_queued_result = await session.execute(
        expired_queued.values(
            status=OutboxStatus.FAILED.value,
            last_error="outbox_job_deadline_exhausted",
        )
    )
    failed = int(getattr(expired_queued_result, "rowcount", 0) or 0)
    claim_query = (
        select(
            OutboxEvent.id,
            OutboxEvent.event_type,
            OutboxEvent.attempts,
            OutboxEvent.processing_token,
            OutboxEvent.deadline_at,
        )
        .where(
            OutboxEvent.status == OutboxStatus.PROCESSING.value,
            OutboxEvent.processing_started_at <= cutoff,
        )
        .order_by(OutboxEvent.processing_started_at, OutboxEvent.id)
        .with_for_update(skip_locked=True)
    )
    if exclude_event_types:
        claim_query = claim_query.where(OutboxEvent.event_type.not_in(exclude_event_types))
    rows = (await session.execute(claim_query)).all()
    requeued = 0
    for row in rows:
        deadline_exhausted = row.deadline_at is not None and int(row.deadline_at) <= now
        safe_to_replay = (
            not deadline_exhausted
            and str(row.event_type) in retry_safe_event_types
            and int(row.attempts) < max_attempts
        )
        result = await session.execute(
            update(OutboxEvent)
            .where(
                OutboxEvent.id == row.id,
                OutboxEvent.status == OutboxStatus.PROCESSING.value,
                OutboxEvent.processing_token == row.processing_token,
            )
            .values(
                status=(OutboxStatus.QUEUED.value if safe_to_replay else OutboxStatus.FAILED.value),
                last_error=(
                    "outbox_stale_claim_requeued"
                    if safe_to_replay
                    else (
                        "outbox_job_deadline_exhausted"
                        if deadline_exhausted
                        else (
                            "outbox_attempt_budget_exhausted"
                            if int(row.attempts) >= max_attempts
                            else "outbox_delivery_outcome_unknown"
                        )
                    )
                ),
                processing_started_at=None,
                processing_token=None,
            )
        )
        if getattr(result, "rowcount", 0):
            if safe_to_replay:
                requeued += 1
            else:
                failed += 1
    return requeued, failed
