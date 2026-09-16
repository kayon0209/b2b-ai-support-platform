"""Inbox event consumer: InboxEvent -> orchestrator run (tickets 6, 7).

This is the missing link that makes the documented pipeline live. The
webhook path persists an InboxEvent and returns in under 300 ms; this
consumer turns that row into an agent run.

Idempotency contract (docs/architecture.md consistency model):
- Inbound events are at-least-once. Duplicate deliveries must never create
  a duplicate customer reply.
- Claiming is done with SELECT ... FOR UPDATE SKIP LOCKED so concurrent
  workers cannot process the same row.
- The run's outbound command id is derived from the InboxEvent id, so even
  a reprocessed row maps to the same Chatwoot send.
"""

import time
import uuid
from dataclasses import dataclass, field

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from observability import JsonLogger, new_trace_context
from platform_core.agent_runtime.models import RunStatus
from platform_core.agent_runtime.orchestrator import AgentOrchestrator, OrchestratorDeps
from platform_core.identity.tenant_context import TenantContext, apply_rls_tenant
from platform_core.retrieval.hybrid import PrincipalScope
from platform_core.support_bridge.models import InboxEvent, InboxEventStatus

logger = JsonLogger("platform.worker")

# Events the AI should act on. Everything else is recorded and acked so the
# inbox does not grow unbounded on conversation-lifecycle chatter.
ACTIONABLE_EVENT_TYPES = {"message_created"}

# System principal for AI-initiated retrieval. Group membership is granted
# explicitly; the AI never inherits a human's scope.
AI_PRINCIPAL = PrincipalScope(principal_types=("role",), principal_ids=("ai_agent",))


@dataclass
class ClaimedEvent:
    event_id: uuid.UUID
    tenant_id: uuid.UUID
    delivery_id: str
    event_type: str
    minimized_payload: dict = field(default_factory=dict)


async def claim_events(session: AsyncSession, *, batch: int = 20) -> list[ClaimedEvent]:
    """Atomically claim unprocessed inbox rows.

    SKIP LOCKED + in-place status change means two workers can run safely
    without double-processing; the status transition is the claim token.
    """
    stmt = (
        select(InboxEvent)
        .where(InboxEvent.status == InboxEventStatus.RECEIVED.value)
        .order_by(InboxEvent.received_at)
        .limit(batch)
        .with_for_update(skip_locked=True)
    )
    rows = (await session.execute(stmt)).scalars().all()
    if not rows:
        return []
    await session.execute(
        update(InboxEvent)
        .where(InboxEvent.id.in_([r.id for r in rows]))
        .values(status=InboxEventStatus.PROCESSING.value)
    )
    return [
        ClaimedEvent(
            event_id=row.id,
            tenant_id=row.tenant_id,
            delivery_id=row.delivery_id,
            event_type=row.event_type,
            minimized_payload=row.minimized_payload or {},
        )
        for row in rows
    ]


async def mark_completed(session: AsyncSession, event_id: uuid.UUID) -> None:
    await session.execute(
        update(InboxEvent)
        .where(InboxEvent.id == event_id)
        .values(status=InboxEventStatus.COMPLETED.value, processed_at=int(time.time()))
    )


async def mark_failed(session: AsyncSession, event_id: uuid.UUID, error: str) -> None:
    await session.execute(
        update(InboxEvent)
        .where(InboxEvent.id == event_id)
        .values(
            status=InboxEventStatus.FAILED.value,
            processed_at=int(time.time()),
            last_error=error[:2000],
        )
    )


def _conversation_ref(event: ClaimedEvent) -> uuid.UUID | None:
    """Resolve the platform conversation ref from the minimized payload.

    The webhook stores the Chatwoot conversation id, not our internal ref.
    We derive a stable UUIDv5 from (tenant, chatwoot conversation id) so the
    same conversation always maps to the same control-lease row without a
    schema change to the inbox table.
    """
    external = event.minimized_payload.get("conversation_id")
    if not external:
        return None
    return uuid.uuid5(event.tenant_id, f"chatwoot:conversation:{external}")


async def resolve_question(event: ClaimedEvent, deps: OrchestratorDeps) -> str | None:
    """Obtain the customer question text for this event.

    The inbox stores minimized metadata only, so the body is fetched from
    Chatwoot on demand (docs/security.md: no raw customer content at rest).
    A payload that already carries content (tests, future connectors) is
    used directly.
    """
    content = event.minimized_payload.get("content")
    if isinstance(content, str) and content.strip():
        return content

    message_id = event.minimized_payload.get("message_id")
    account_id = event.minimized_payload.get("chatwoot_account_id")
    conversation_id = event.minimized_payload.get("conversation_id")
    reader = deps.reader
    if reader is None or not (message_id and account_id and conversation_id):
        return None
    body = await reader.fetch_message(  # type: ignore[attr-defined]
        account_id=str(account_id),
        conversation_id=str(conversation_id),
        message_id=str(message_id),
    )
    if not isinstance(body, str) or not body.strip():
        return None
    return body


async def process_event(
    session: AsyncSession,
    event: ClaimedEvent,
    *,
    deps: OrchestratorDeps,
) -> RunStatus | None:
    """Run one claimed event through the orchestrator.

    Returns the resulting run status, or None when the event is recorded
    but deliberately not actioned.
    """
    if event.event_type not in ACTIONABLE_EVENT_TYPES:
        return None

    # The inbox row may legitimately lack routing fields (e.g. non-message
    # events). Such rows are acked without an agent run.
    conversation_ref_id = _conversation_ref(event)
    if conversation_ref_id is None:
        return None

    question = await resolve_question(event, deps)
    if question is None:
        # No readable body: record the row and leave it. Answering without
        # the customer's question is never acceptable.
        return None

    ctx = TenantContext(tenant_id=event.tenant_id, actor_id=None, actor_kind="system")
    await apply_rls_tenant(session, ctx)

    trace = new_trace_context()
    orchestrator = AgentOrchestrator(session, deps)
    outcome = await orchestrator.run(
        tenant_id=event.tenant_id,
        conversation_ref_id=conversation_ref_id,
        question=question,
        principal=AI_PRINCIPAL,
        trace=trace,
        chatwoot_account_id=str(event.minimized_payload.get("chatwoot_account_id") or ""),
        chatwoot_conversation_id=str(event.minimized_payload.get("conversation_id") or ""),
    )
    logger.info(
        "event_processed",
        trace,
        delivery_id=event.delivery_id,
        run_id=str(outcome.run_id),
        status=outcome.status.value,
        route=outcome.route,
        latency_ms=outcome.latency_ms,
    )
    return outcome.status


async def drain_once(
    session: AsyncSession,
    *,
    deps: OrchestratorDeps,
    batch: int = 20,
) -> int:
    """Claim and process one batch. Returns the number of rows finalised.

    A failure on one event is isolated: it is marked FAILED with the error
    recorded and the batch continues, so one poison payload cannot stall
    the queue.
    """
    events = await claim_events(session, batch=batch)
    processed = 0
    for event in events:
        try:
            await process_event(session, event, deps=deps)
        except Exception as exc:  # noqa: BLE001 - per-event isolation
            await mark_failed(session, event.event_id, f"{type(exc).__name__}: {exc}")
            logger.error(
                "event_failed",
                error_code=type(exc).__name__,
                delivery_id=event.delivery_id,
            )
        else:
            await mark_completed(session, event.event_id)
        processed += 1
    return processed
