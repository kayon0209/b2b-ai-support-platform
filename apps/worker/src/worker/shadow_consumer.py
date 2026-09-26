"""Shadow classification consumer: outbox row -> assessment row.

This is the module the acceptance review was right to insist on. The previous
wiring awaited the classification inside `inbox_consumer.process_event` and
passed `deps.generator` to it - an answer generator, which has `generate` and
not `complete`. Every shadow run therefore failed with
`SEMANTIC_MODEL_UNAVAILABLE` while still reporting `recorded`, and the
comparison dataset was built from failures.

Four properties, each of which is a test in `test_shadow_consumer.py`:

1. **The provider is the chat provider.** Taken from `deps.extra["chat"]`,
   which is where `wiring.py` puts the bundle's `ChatProvider`. `None` is a
   recorded failure, not a skip.
2. **A model failure is a failure.** `record_shadow` returns `recorded=False`
   with a reason, and this consumer writes an assessment whose
   `validation_status` is `rejected`. It never writes an assessment that
   claims a comparison happened when none did.
3. **Its own transaction, its own session.** The claim is committed before the
   model call, so a killed consumer releases the row rather than holding it,
   and a model timeout cannot roll back the claim.
4. **Bounded.** A short deadline, no retry, and the quota and expiry checks in
   `shadow.py` applied before the provider is touched.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from observability import JsonLogger
from observability_metrics import get_metrics
from platform_core.agent_runtime.semantic.shadow import (
    SHADOW_EVENT_TYPE,
    ShadowRequest,
    capabilities_for_shadow,
    record_shadow,
)
from platform_core.identity.tenant_context import TenantContext
from platform_core.outbox import OutboxEvent, OutboxStatus

logger = JsonLogger("platform.worker")

# A shadow classification is worth far less than a fast customer answer, so it
# gets a shorter deadline than the interactive path and no retry at all.
SHADOW_DEADLINE_SECONDS = 2.0
SHADOW_MAX_RETRIES = 0

# A claimed row nobody finishes within this window returns to the queue. Well
# beyond the deadline, so a slow provider is never mistaken for a dead worker.
STALE_SHADOW_SECONDS = 300

# The claimed state. `OutboxStatus` has no in-flight member, and adding one
# would change a shared vocabulary the outbox relay also reads; a literal
# keeps the claim visible without widening the enum for one consumer.
SHADOW_IN_FLIGHT = "processing"

# How many rows one drain claims. Small: this competes with the interactive
# worker for the same database, and a shadow backlog must never be the reason
# a customer waits.
SHADOW_BATCH = 10


@dataclass(frozen=True)
class ClaimedShadow:
    event_id: uuid.UUID
    tenant_id: uuid.UUID
    conversation_ref_id: uuid.UUID
    turn_id: str
    question: str
    history: list[tuple[str, str]]
    turn_created_at: int


def chat_provider(deps: Any) -> Any | None:
    """The `ChatProvider` from the worker's dependency bundle.

    `wiring.py` puts the model bundle's chat client in `extra["chat"]` and the
    answer generator in `generator`; they are different objects with different
    methods, and confusing them is what made every shadow run fail silently.
    Returning `None` for either case lets the caller record a failure.
    """
    extra = getattr(deps, "extra", None) or {}
    provider = extra.get("chat")
    if provider is not None and hasattr(provider, "complete"):
        return provider
    return None


async def claim_shadow_events(
    session: AsyncSession, *, batch: int = SHADOW_BATCH
) -> list[ClaimedShadow]:
    """Claim queued shadow requests with SKIP LOCKED.

    The same claim discipline as `inbox_consumer`: two workers must not analyse
    the same turn, and a duplicate analysis would write two assessments for one
    input.
    """
    rows = (
        (
            await session.execute(
                select(OutboxEvent)
                .where(
                    OutboxEvent.event_type == SHADOW_EVENT_TYPE,
                    OutboxEvent.status == OutboxStatus.QUEUED.value,
                )
                .order_by(OutboxEvent.created_at, OutboxEvent.id)
                .limit(batch)
                .with_for_update(skip_locked=True)
            )
        )
        .scalars()
        .all()
    )
    claimed: list[ClaimedShadow] = []
    for row in rows:
        payload = dict(row.payload or {})
        await session.execute(
            update(OutboxEvent).where(OutboxEvent.id == row.id).values(status=SHADOW_IN_FLIGHT)
        )
        try:
            conversation_ref = uuid.UUID(str(payload.get("conversation_ref")))
        except (TypeError, ValueError):
            # A row whose aggregate cannot be read is terminal, not retryable:
            # the same payload will fail identically forever.
            await session.execute(
                update(OutboxEvent)
                .where(OutboxEvent.id == row.id)
                .values(
                    status=OutboxStatus.FAILED.value,
                    last_error="shadow_conversation_ref_unreadable",
                )
            )
            continue
        claimed.append(
            ClaimedShadow(
                event_id=row.event_id,
                tenant_id=row.tenant_id,
                conversation_ref_id=conversation_ref,
                turn_id=str(payload.get("turn_id") or ""),
                question=str(payload.get("question") or ""),
                history=[(str(h[0]), str(h[1])) for h in (payload.get("history") or [])],
                turn_created_at=int(payload.get("turn_created_at") or 0),
            )
        )
    return claimed


async def reclaim_stale_shadow(session: AsyncSession) -> int:
    """Return long-claimed rows to the queue.

    Without this a consumer killed mid-classification leaves the row in flight
    forever and the comparison silently stops. The assessment write is
    idempotent per turn, so re-running an interrupted claim is safe.
    """
    cutoff = int(time.time()) - STALE_SHADOW_SECONDS
    result = await session.execute(
        update(OutboxEvent)
        .where(
            OutboxEvent.event_type == SHADOW_EVENT_TYPE,
            OutboxEvent.status == SHADOW_IN_FLIGHT,
            OutboxEvent.created_at < cutoff,
        )
        .values(status=OutboxStatus.QUEUED.value)
    )
    return int(getattr(result, "rowcount", 0) or 0)


async def process_shadow_event(
    session: AsyncSession,
    claimed: ClaimedShadow,
    *,
    provider: Any | None,
) -> str:
    """Analyse one claimed turn. Returns a closed-vocabulary outcome code.

    Never raises: a failure here is recorded on the row and returned, because
    an exception would leave the outbox row in flight until the stale sweep and
    would hide the reason.
    """
    from platform_core.agent_runtime.semantic.service import SemanticBudget
    from platform_core.tool_gateway import registry

    metrics = get_metrics()

    if provider is None:
        # B1-01: this used to be reported as `recorded` while the model had
        # never been called. A missing provider is a failure, and the metric
        # says so.
        await _finish(session, claimed, status=OutboxStatus.FAILED.value, error="no_chat_provider")
        metrics.inbox_events_total.labels(result="shadow_no_provider").inc()
        return "failed"

    try:
        await registry.ensure_tool_definitions(session, tenant_id=claimed.tenant_id)
        available = capabilities_for_shadow(await _tenant_tool_names(session, claimed.tenant_id))

        outcome = await record_shadow(
            session,
            ShadowRequest(
                tenant_id=claimed.tenant_id,
                conversation_ref_id=claimed.conversation_ref_id,
                turn_id=claimed.turn_id,
                turn_text=claimed.question,
                history=claimed.history,
                lease_owner_type="ai",
                capabilities=available,
                turn_created_at=claimed.turn_created_at or int(time.time()),
            ),
            provider=provider,
            budget=SemanticBudget(
                deadline_seconds=SHADOW_DEADLINE_SECONDS,
                max_retries=SHADOW_MAX_RETRIES,
            ),
        )
    except Exception as exc:  # noqa: BLE001 - recorded, never propagated
        await _finish(session, claimed, status=OutboxStatus.FAILED.value, error=type(exc).__name__)
        metrics.inbox_events_total.labels(result="shadow_error").inc()
        logger.warning(
            "shadow_process_failed",
            conversation_ref_id=str(claimed.conversation_ref_id),
            error_code=type(exc).__name__,
        )
        return "failed"

    if not outcome.recorded:
        # Expired or over quota: not an error, and not a comparison either.
        # Marked failed so it is not retried, and the reason is the reason.
        await _finish(session, claimed, status=OutboxStatus.FAILED.value, error=outcome.reason)
        metrics.inbox_events_total.labels(result=f"shadow_{outcome.reason.lower()}").inc()
        return outcome.reason.lower()

    await _finish(session, claimed, status=OutboxStatus.SENT.value, error="")
    metrics.inbox_events_total.labels(result="shadow_recorded").inc()
    return "recorded"


async def _finish(
    session: AsyncSession, claimed: ClaimedShadow, *, status: str, error: str
) -> None:
    await session.execute(
        update(OutboxEvent)
        .where(OutboxEvent.tenant_id == claimed.tenant_id, OutboxEvent.event_id == claimed.event_id)
        .values(
            status=status,
            published_at=int(time.time()) if status == OutboxStatus.SENT.value else None,
            last_error=error[:255] if error else None,
        )
    )


async def _tenant_tool_names(session: AsyncSession, tenant_id: uuid.UUID) -> dict[str, Any]:
    from platform_core.agent_runtime.semantic.validator import CapabilityView
    from platform_core.tool_gateway.models import ToolDefinition

    rows = (
        await session.execute(
            select(ToolDefinition).where(
                (ToolDefinition.tenant_id == tenant_id) | ToolDefinition.tenant_id.is_(None)
            )
        )
    ).scalars()
    return {row.name: CapabilityView(tool_name=row.name, risk_class=row.risk) for row in rows}


def context_for(claimed: ClaimedShadow) -> TenantContext:
    """The tenant binding a consumer session must be opened with.

    The outbox row is read through the owner connection when claiming, so the
    analysis transaction needs the RLS context re-applied explicitly.
    """
    return TenantContext(
        tenant_id=claimed.tenant_id,
        actor_id=None,
        actor_kind="system",
        role="integration_service",
    )


__all__ = [
    "SHADOW_BATCH",
    "SHADOW_DEADLINE_SECONDS",
    "SHADOW_MAX_RETRIES",
    "SHADOW_IN_FLIGHT",
    "STALE_SHADOW_SECONDS",
    "ClaimedShadow",
    "chat_provider",
    "claim_shadow_events",
    "context_for",
    "process_shadow_event",
    "reclaim_stale_shadow",
]
