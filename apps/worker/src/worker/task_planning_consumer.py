"""Tenant-isolated worker for deferred conversation task planning."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from observability import JsonLogger
from observability_metrics import get_metrics
from platform_core.agent_runtime.models import ConversationTurn
from platform_core.agent_runtime.tasks.planning_seam import TASK_PLANNING_EVENT_TYPE
from platform_core.identity.tenant_context import TenantContext
from platform_core.outbox import OutboxEvent, OutboxStatus

logger = JsonLogger("platform.worker")

TASK_PLAN_BATCH = 5
TASK_PLAN_MAX_ATTEMPTS = 3
TASK_PLAN_IN_FLIGHT = "processing"
STALE_TASK_PLAN_SECONDS = 600


@dataclass(frozen=True)
class ClaimedTaskPlanning:
    """Queue reference; contains no customer message data."""

    event_id: uuid.UUID
    tenant_id: uuid.UUID
    deadline_at: int | None = None
    attempt: int = 1
    external_attempt_limit: int = TASK_PLAN_MAX_ATTEMPTS


async def claim_task_planning_events(
    session: AsyncSession,
    *,
    batch: int = TASK_PLAN_BATCH,
    tenant_id: uuid.UUID | None = None,
    event_id: uuid.UUID | None = None,
) -> list[ClaimedTaskPlanning]:
    """Claim only outbox metadata before tenant context is known.

    Optional tenant/event filters let a controlled replay or failure-injection
    test claim one known event without touching neighboring tenants' queue
    entries. The normal poller omits them and claims the bounded global batch.
    """
    from platform_core.config import get_settings

    settings = get_settings()
    now = int(time.time())
    await session.execute(
        update(OutboxEvent)
        .where(
            OutboxEvent.event_type == TASK_PLANNING_EVENT_TYPE,
            OutboxEvent.status == OutboxStatus.QUEUED.value,
            OutboxEvent.attempts >= TASK_PLAN_MAX_ATTEMPTS,
        )
        .values(
            status=OutboxStatus.FAILED.value,
            processing_started_at=None,
            last_error="task_planning_attempt_budget_exhausted",
        )
    )
    expired = await session.execute(
        update(OutboxEvent)
        .where(
            OutboxEvent.event_type == TASK_PLANNING_EVENT_TYPE,
            OutboxEvent.status == OutboxStatus.QUEUED.value,
            OutboxEvent.deadline_at <= now,
        )
        .values(status=OutboxStatus.FAILED.value, last_error="task_planning_deadline_exhausted")
    )
    expired_count = int(getattr(expired, "rowcount", 0) or 0)
    if expired_count:
        logger.error("task_planning_deadline_exhausted", count=expired_count)
        get_metrics().inbox_events_total.labels(result="task_plan_deadline_exhausted").inc(
            expired_count
        )
    stmt = select(
        OutboxEvent.id,
        OutboxEvent.event_id,
        OutboxEvent.tenant_id,
        OutboxEvent.attempts,
        OutboxEvent.first_attempt_at,
        OutboxEvent.deadline_at,
        OutboxEvent.external_attempt_limit,
    ).where(
        OutboxEvent.event_type == TASK_PLANNING_EVENT_TYPE,
        OutboxEvent.status == OutboxStatus.QUEUED.value,
        OutboxEvent.attempts < TASK_PLAN_MAX_ATTEMPTS,
        or_(OutboxEvent.deadline_at.is_(None), OutboxEvent.deadline_at > now),
    )
    if tenant_id is not None:
        stmt = stmt.where(OutboxEvent.tenant_id == tenant_id)
    if event_id is not None:
        stmt = stmt.where(OutboxEvent.event_id == event_id)
    rows = (
        await session.execute(
            stmt.order_by(OutboxEvent.created_at, OutboxEvent.id)
            .limit(batch)
            .with_for_update(skip_locked=True)
        )
    ).all()
    claims: list[ClaimedTaskPlanning] = []
    for row in rows:
        first_attempt_at = int(row.first_attempt_at or now)
        deadline_at = int(
            row.deadline_at or first_attempt_at + settings.outbox_job_deadline_seconds
        )
        external_attempt_limit = int(
            row.external_attempt_limit
            or min(settings.outbox_event_max_external_attempts, TASK_PLAN_MAX_ATTEMPTS)
        )
        await session.execute(
            update(OutboxEvent)
            .where(OutboxEvent.id == row.id)
            .values(
                status=TASK_PLAN_IN_FLIGHT,
                processing_started_at=now,
                first_attempt_at=func.coalesce(OutboxEvent.first_attempt_at, now),
                deadline_at=func.coalesce(OutboxEvent.deadline_at, deadline_at),
                external_attempt_limit=func.coalesce(
                    OutboxEvent.external_attempt_limit, external_attempt_limit
                ),
                attempts=OutboxEvent.attempts + 1,
            )
        )
        claims.append(
            ClaimedTaskPlanning(
                event_id=row.event_id,
                tenant_id=row.tenant_id,
                deadline_at=deadline_at,
                attempt=int(row.attempts) + 1,
                external_attempt_limit=external_attempt_limit,
            )
        )
    return claims


async def reclaim_stale_task_planning(session: AsyncSession) -> int:
    """Return claims from a stopped worker to the durable queue."""
    now = int(time.time())
    cutoff = now - STALE_TASK_PLAN_SECONDS
    stale = and_(
        OutboxEvent.event_type == TASK_PLANNING_EVENT_TYPE,
        OutboxEvent.status == TASK_PLAN_IN_FLIGHT,
        OutboxEvent.processing_started_at < cutoff,
    )
    expired_queued = await session.execute(
        update(OutboxEvent)
        .where(
            OutboxEvent.event_type == TASK_PLANNING_EVENT_TYPE,
            OutboxEvent.status == OutboxStatus.QUEUED.value,
            OutboxEvent.deadline_at <= now,
        )
        .values(status=OutboxStatus.FAILED.value, last_error="task_planning_deadline_exhausted")
    )
    deadline_failed = await session.execute(
        update(OutboxEvent)
        .where(stale, OutboxEvent.deadline_at <= now)
        .values(
            status=OutboxStatus.FAILED.value,
            processing_started_at=None,
            last_error="task_planning_deadline_exhausted",
        )
    )
    retryable_stale = and_(
        stale,
        or_(OutboxEvent.deadline_at.is_(None), OutboxEvent.deadline_at > now),
    )
    result = await session.execute(
        update(OutboxEvent)
        .where(retryable_stale, OutboxEvent.attempts < TASK_PLAN_MAX_ATTEMPTS)
        .values(status=OutboxStatus.QUEUED.value, processing_started_at=None)
    )
    exhausted = await session.execute(
        update(OutboxEvent)
        .where(retryable_stale, OutboxEvent.attempts >= TASK_PLAN_MAX_ATTEMPTS)
        .values(
            status=OutboxStatus.FAILED.value,
            processing_started_at=None,
            last_error="task_planning_attempt_budget_exhausted",
        )
    )
    exhausted_count = (
        int(getattr(exhausted, "rowcount", 0) or 0)
        + int(getattr(deadline_failed, "rowcount", 0) or 0)
        + int(getattr(expired_queued, "rowcount", 0) or 0)
    )
    if exhausted_count:
        logger.error("task_planning_attempt_budget_exhausted", count=exhausted_count)
        get_metrics().inbox_events_total.labels(result="task_plan_retry_budget_exhausted").inc(
            exhausted_count
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
                or_(
                    ConversationTurn.ts < turn.ts,
                    and_(
                        ConversationTurn.ts == turn.ts,
                        ConversationTurn.id < turn.id,
                    ),
                ),
            )
            .order_by(ConversationTurn.ts.desc(), ConversationTurn.id.desc())
            .limit(8)
        )
    ).all()
    history = [(str(row.id), row.text_redacted) for row in reversed(history_rows)]

    try:
        from platform_core.config import get_settings
        from platform_core.execution_budget import ExecutionBudget, use_execution_budget
        from worker.inbox_consumer import _plan_conversation_tasks

        remaining = (
            get_settings().outbox_job_deadline_seconds
            if claim.deadline_at is None
            else claim.deadline_at - int(time.time())
        )
        if remaining <= 0:
            await _finish(
                session,
                claim,
                status=OutboxStatus.FAILED.value,
                error="task_planning_deadline_exhausted",
            )
            get_metrics().inbox_events_total.labels(result="task_plan_deadline_exhausted").inc()
            return "failed"
        base, remainder = divmod(claim.external_attempt_limit, TASK_PLAN_MAX_ATTEMPTS)
        model_attempts = base + (1 if claim.attempt <= remainder else 0)
        if model_attempts < 1:
            await _finish(
                session,
                claim,
                status=OutboxStatus.FAILED.value,
                error="task_planning_attempt_budget_exhausted",
            )
            get_metrics().inbox_events_total.labels(result="task_plan_retry_budget_exhausted").inc()
            return "failed"
        budget = ExecutionBudget.for_seconds(
            deadline_seconds=min(3.0, float(remaining)),
            max_attempts=model_attempts,
            operation_limits={"model": model_attempts, "tool": 0, "outbound": 0},
        )
        with use_execution_budget(budget):
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
