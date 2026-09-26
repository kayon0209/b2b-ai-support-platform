"""Tenant-isolated worker for deferred conversation task planning."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select, tuple_, update
from sqlalchemy.ext.asyncio import AsyncSession

from observability import JsonLogger
from observability_metrics import get_metrics
from platform_core.agent_runtime.models import ConversationTurn
from platform_core.agent_runtime.tasks.planning_seam import TASK_PLANNING_EVENT_TYPE
from platform_core.identity.tenant_context import TenantContext
from platform_core.outbox import OutboxEvent, OutboxStatus

logger = JsonLogger("platform.worker")

TASK_PLAN_BATCH = 5
TASK_PLAN_IN_FLIGHT = "processing"
STALE_TASK_PLAN_SECONDS = 600


@dataclass(frozen=True)
class ClaimedTaskPlanning:
    """Queue reference; contains no customer message data."""

    event_id: uuid.UUID
    tenant_id: uuid.UUID


async def claim_task_planning_events(
    session: AsyncSession, *, batch: int = TASK_PLAN_BATCH
) -> list[ClaimedTaskPlanning]:
    """Claim only outbox metadata before tenant context is known."""
    rows = (
        await session.execute(
            select(OutboxEvent.id, OutboxEvent.event_id, OutboxEvent.tenant_id)
            .where(
                OutboxEvent.event_type == TASK_PLANNING_EVENT_TYPE,
                OutboxEvent.status == OutboxStatus.QUEUED.value,
            )
            .order_by(OutboxEvent.created_at, OutboxEvent.id)
            .limit(batch)
            .with_for_update(skip_locked=True)
        )
    ).all()
    claims: list[ClaimedTaskPlanning] = []
    for row in rows:
        await session.execute(
            update(OutboxEvent)
            .where(OutboxEvent.id == row.id)
            .values(status=TASK_PLAN_IN_FLIGHT, processing_started_at=int(time.time()))
        )
        claims.append(ClaimedTaskPlanning(event_id=row.event_id, tenant_id=row.tenant_id))
    return claims


async def reclaim_stale_task_planning(session: AsyncSession) -> int:
    """Return claims from a stopped worker to the durable queue."""
    cutoff = int(time.time()) - STALE_TASK_PLAN_SECONDS
    result = await session.execute(
        update(OutboxEvent)
        .where(
            OutboxEvent.event_type == TASK_PLANNING_EVENT_TYPE,
            OutboxEvent.status == TASK_PLAN_IN_FLIGHT,
            OutboxEvent.processing_started_at < cutoff,
        )
        .values(status=OutboxStatus.QUEUED.value, processing_started_at=None)
    )
    return int(getattr(result, "rowcount", 0) or 0)


async def process_task_planning_event(
    session: AsyncSession,
    claim: ClaimedTaskPlanning,
    *,
    deps: Any,
) -> str:
    """Load the redacted source turn, plan tasks, then retire the event."""
    event = (
        await session.execute(
            select(OutboxEvent).where(
                OutboxEvent.tenant_id == claim.tenant_id,
                OutboxEvent.event_id == claim.event_id,
            )
        )
    ).scalar_one_or_none()
    if event is None:
        return "missing"

    payload = dict(event.payload or {})
    try:
        conversation_ref_id = uuid.UUID(str(payload.get("conversation_ref")))
        turn_id = uuid.UUID(str(payload.get("turn_id")))
    except (TypeError, ValueError):
        await _finish(
            session,
            claim,
            status=OutboxStatus.FAILED.value,
            error="task_plan_payload_invalid",
        )
        return "failed"

    turn = (
        await session.execute(
            select(ConversationTurn).where(
                ConversationTurn.tenant_id == claim.tenant_id,
                ConversationTurn.id == turn_id,
                ConversationTurn.conversation_ref_id == conversation_ref_id,
                ConversationTurn.role == "customer",
            )
        )
    ).scalar_one_or_none()
    if turn is None:
        await _finish(
            session, claim, status=OutboxStatus.FAILED.value, error="task_plan_source_turn_missing"
        )
        get_metrics().inbox_events_total.labels(result="task_plan_source_missing").inc()
        return "failed"

    history_rows = (
        await session.execute(
            select(ConversationTurn.id, ConversationTurn.text_redacted)
            .where(
                ConversationTurn.tenant_id == claim.tenant_id,
                ConversationTurn.conversation_ref_id == conversation_ref_id,
                tuple_(ConversationTurn.ts, ConversationTurn.id) < tuple_(turn.ts, turn.id),
            )
            .order_by(ConversationTurn.ts.desc(), ConversationTurn.id.desc())
            .limit(8)
        )
    ).all()
    history = [(str(row.id), row.text_redacted) for row in reversed(history_rows)]

    try:
        from worker.inbox_consumer import _plan_conversation_tasks

        await _plan_conversation_tasks(
            session,
            tenant_id=claim.tenant_id,
            conversation_ref_id=conversation_ref_id,
            question=turn.text_redacted,
            history=history,
            lease_owner_type="ai",
            deps=deps,
            turn_created_at=int(turn.ts or time.time()),
            turn_id=str(turn.id),
            raise_errors=True,
        )
    except Exception as exc:  # noqa: BLE001 - record failure without poisoning the worker
        await _finish(
            session,
            claim,
            status=OutboxStatus.FAILED.value,
            error=type(exc).__name__,
        )
        logger.warning("task_planning_job_failed", error_code=type(exc).__name__)
        get_metrics().inbox_events_total.labels(result="task_planning_failed").inc()
        return "failed"

    await _finish(session, claim, status=OutboxStatus.SENT.value, error="")
    return "completed"


async def _finish(
    session: AsyncSession,
    claim: ClaimedTaskPlanning,
    *,
    status: str,
    error: str,
) -> None:
    await session.execute(
        update(OutboxEvent)
        .where(
            OutboxEvent.tenant_id == claim.tenant_id,
            OutboxEvent.event_id == claim.event_id,
        )
        .values(
            status=status,
            published_at=int(time.time()) if status == OutboxStatus.SENT.value else None,
            processing_started_at=None,
            last_error=error[:255] if error else None,
        )
    )


def context_for(claim: ClaimedTaskPlanning) -> TenantContext:
    return TenantContext(
        tenant_id=claim.tenant_id,
        actor_id=None,
        actor_kind="system",
        role="integration_service",
    )


__all__ = [
    "TASK_PLAN_BATCH",
    "TASK_PLAN_IN_FLIGHT",
    "STALE_TASK_PLAN_SECONDS",
    "ClaimedTaskPlanning",
    "claim_task_planning_events",
    "context_for",
    "process_task_planning_event",
    "reclaim_stale_task_planning",
]
