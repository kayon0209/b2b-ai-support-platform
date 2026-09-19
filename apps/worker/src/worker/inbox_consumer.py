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

Stuck-claim recovery: a row claimed as PROCESSING by a worker that then
died would otherwise never be retried, because nothing transitions it back.
`reclaim_stale_processing` returns such rows to RECEIVED after a timeout.
This is safe precisely because the outbound command id is derived from the
event id: a re-run of the same event cannot produce a second customer
message.
"""

import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from observability import JsonLogger, new_trace_context
from observability_metrics import get_metrics
from platform_core.agent_runtime.conversation import Turn
from platform_core.agent_runtime.models import RunStatus
from platform_core.agent_runtime.orchestrator import AgentOrchestrator, OrchestratorDeps
from platform_core.identity.tenant_context import TenantContext, apply_rls_tenant
from platform_core.retrieval.hybrid import PrincipalScope
from platform_core.support_bridge.models import InboxEvent, InboxEventStatus

logger = JsonLogger("platform.worker")

# How long a claimed row may sit in PROCESSING before another worker may
# take it. Must comfortably exceed the slowest realistic run: a knowledge
# answer with retrieval measured ~30 s end to end, so 10 minutes leaves a
# wide margin while still bounding how long a question can go unanswered
# after a crash.
STALE_PROCESSING_SECONDS = 600

# Events the AI should act on. Everything else is recorded and acked so the
# inbox does not grow unbounded on conversation-lifecycle chatter.
ACTIONABLE_EVENT_TYPES = {"message_created"}

# Only customer-authored messages may trigger a run.
#
# This is a self-reply guard, and it is load-bearing. Chatwoot fires
# `message_created` for outbound messages too, so without this filter the
# agent's own reply arrives as a new inbox event, the agent answers that,
# and the loop never terminates — one customer question becomes an
# unbounded stream of LLM calls and customer-visible messages. Verified
# against a live Chatwoot: a single question produced replies up to
# message id 17 before the worker was stopped.
#
# `message_type` is Chatwoot's own field: "incoming" is the customer,
# "outgoing" is an agent or bot. Anything else (missing, unknown) is
# treated as non-actionable: for a reply guard the safe default is to
# stay silent, since answering a message we did not author is only
# correct for genuine inbound traffic.
CUSTOMER_MESSAGE_TYPES = {"incoming"}

# System principal for AI-initiated retrieval. Group membership is granted
# explicitly; the AI never inherits a human's scope.
AI_PRINCIPAL = PrincipalScope(principal_types=("role",), principal_ids=("ai_agent",))


@dataclass
class ClaimedEvent:
    event_id: uuid.UUID
    tenant_id: uuid.UUID
    delivery_id: str
    event_type: str
    minimized_payload: dict[str, Any] = field(default_factory=dict)
    # Wall-clock seconds when the row was received. Carried so the consumer
    # can measure queue age (received -> run start), which is the signal
    # docs/deployment-and-operations.md makes a P1 alert. A run can be fast
    # and the customer can still wait minutes, so run latency alone does not
    # describe the experience.
    received_at: int = 0


async def claim_events(
    session: AsyncSession, *, batch: int = 20, priority: bool = False
) -> list[ClaimedEvent]:
    """Atomically claim unprocessed inbox rows.

    SKIP LOCKED + in-place status change means two workers can run safely
    without double-processing; the status transition is the claim token.

    `priority` (plan 5.3) claims events whose conversation has an ESCALATED
    case first: a customer who has already been escalated is waiting on the
    platform's weakest moment, and FIFO alone would let a burst of fresh
    questions keep them waiting. Remaining capacity fills with normal FIFO.
    """
    rows: list[InboxEvent] = []
    if priority:
        escalated = (
            select(InboxEvent)
            .where(
                InboxEvent.status == InboxEventStatus.RECEIVED.value,
                InboxEvent.conversation_ref_id.in_(select(case_conversation_ref())),
            )
            .order_by(InboxEvent.received_at)
            .limit(batch)
            .with_for_update(skip_locked=True)
        )
        rows = list((await session.execute(escalated)).scalars().all())
    remaining = batch - len(rows)
    if remaining > 0:
        claimed_ids = [r.id for r in rows]
        stmt = (
            select(InboxEvent)
            .where(
                InboxEvent.status == InboxEventStatus.RECEIVED.value,
                *([InboxEvent.id.not_in(claimed_ids)] if claimed_ids else []),
            )
            .order_by(InboxEvent.received_at)
            .limit(remaining)
            .with_for_update(skip_locked=True)
        )
        rows = rows + list((await session.execute(stmt)).scalars().all())
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
            received_at=int(row.received_at or 0),
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


async def reclaim_stale_processing(
    session: AsyncSession, *, timeout_seconds: int = STALE_PROCESSING_SECONDS
) -> int:
    """Return rows abandoned in PROCESSING to RECEIVED. Returns the count.

    A worker that claims a row and then dies leaves it PROCESSING forever
    — nothing else ever transitions it. Without this, a crash mid-run
    silently drops the customer's question, which is the failure mode the
    whole inbox design exists to prevent.

    Re-running is safe because the outbound command id is derived from the
    event id, so a duplicate send for the same event is suppressed by
    Chatwoot-side idempotency rather than reaching the customer twice.

    `received_at` is left untouched: it is the FIFO ordering key, and a
    reclaimed question should keep its original place in the queue.
    """
    cutoff = int(time.time()) - timeout_seconds
    result = await session.execute(
        update(InboxEvent)
        .where(
            InboxEvent.status == InboxEventStatus.PROCESSING.value,
            InboxEvent.received_at < cutoff,
        )
        .values(status=InboxEventStatus.RECEIVED.value)
    )
    return result.rowcount or 0  # type: ignore[attr-defined]


def case_conversation_ref() -> Any:
    """Select conversation_ref_id of cases in ESCALATED state.

    The ref lives on the CaseConversation join table, so this is a join, not
    a column read. Expressed as a subquery so the claim stays one statement
    under SKIP LOCKED.
    """
    from sqlalchemy import and_

    from platform_core.cases.models import Case, CaseConversation, CaseEscalation

    return (
        select(CaseConversation.conversation_ref_id)
        .join(
            Case,
            and_(
                Case.id == CaseConversation.case_id,
                Case.tenant_id == CaseConversation.tenant_id,
            ),
        )
        # "Escalated" means the SLA ladder fired at least one rung for the
        # case (escalations are append-only, migration 0029): a breach
        # already happened and the customer is still waiting.
        .where(Case.id.in_(select(CaseEscalation.case_id)))
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
    # Only the message id is universally required. `account_id` and
    # `conversation_id` are Chatwoot coordinates, and a question typed into
    # the platform's own chat surface has no Chatwoot account behind it —
    # requiring them here made every platform-originated run silently skip
    # (event claimed and marked completed, run left queued, no answer).
    # The reader decides which coordinates it actually needs.
    if reader is None or not message_id:
        return None
    body = await reader.fetch_message(  # type: ignore[attr-defined]
        account_id=str(account_id or ""),
        conversation_id=str(conversation_id or ""),
        message_id=str(message_id),
    )
    if not isinstance(body, str) or not body.strip():
        return None
    return body


def is_customer_message(event: ClaimedEvent) -> bool:
    """True only for a message the *customer* authored.

    The agent must never answer its own replies: Chatwoot emits
    `message_created` for outbound messages too, which would otherwise
    feed each answer straight back in as the next question.
    """
    message_type = event.minimized_payload.get("message_type")
    if not isinstance(message_type, str):
        return False
    return message_type.strip().lower() in CUSTOMER_MESSAGE_TYPES


async def load_history(
    session: AsyncSession, event: ClaimedEvent, deps: OrchestratorDeps
) -> list[Turn]:
    """Prior turns of this conversation, oldest first (plan 2.2/2.3).

    Merge policy, in priority order:

    1. Local redacted turns are authoritative for their window - they were
       written by this platform and already redacted.
    2. Live Chatwoot messages fill the OLDER window (before the oldest local
       turn) and the whole history when nothing is stored yet. Chatwoot is
       the system of record for raw content; the local store is a bounded
       redacted cache, not a competitor.

    Failure anywhere degrades to whatever was loaded: multi-turn is an
    enhancement, and a run that cannot fetch history still answers the
    question in front of it.
    """
    from platform_core.agent_runtime import conversation_store
    from platform_core.agent_runtime.conversation import ConversationMemory, TurnRole
    from platform_core.config import get_settings
    from platform_core.evaluation.pii import redact_text

    conversation_ref_id = _conversation_ref(event)
    if conversation_ref_id is None:
        return []
    settings = get_settings()
    memory = ConversationMemory()

    async def _admit(turn: Turn) -> None:
        # Trim through `ConversationMemory.add`, not by slicing: the memory's
        # pin rules are what decide which old turn is safe to drop, and a
        # dropped PIN is the one outcome worth waking someone up about.
        evicted = memory.add(turn)
        if evicted:
            get_metrics().context_pins_evicted.inc(evicted)
            logger.warning("context_pins_evicted", count=evicted)

    local = await conversation_store.load_turns(
        session,
        tenant_id=event.tenant_id,
        conversation_ref_id=conversation_ref_id,
        limit=settings.context_max_turns,
    )
    for turn in local:
        await _admit(turn)

    reader = deps.reader
    fetch = getattr(reader, "list_messages", None)
    if fetch is None:
        return memory.turns
    account_id = str(event.minimized_payload.get("chatwoot_account_id") or "")
    conversation_id = str(event.minimized_payload.get("conversation_id") or "")
    if not (account_id and conversation_id):
        return memory.turns
    try:
        messages = await fetch(
            account_id=account_id,
            conversation_id=conversation_id,
            limit=settings.history_fetch_limit,
        )
    except Exception:  # noqa: BLE001 - degradation is the documented contract
        return memory.turns

    oldest_local_ts = min((t.ts for t in local if t.ts), default=0)
    for message in reversed(messages):
        if not isinstance(message, dict):
            continue
        message_type = str(message.get("message_type") or message.get("sender_type") or "")
        role = TurnRole.CUSTOMER if message_type == "incoming" else TurnRole.AGENT
        text = message.get("content")
        if not isinstance(text, str) or not text.strip():
            continue
        created_at = int(message.get("created_at") or 0)
        # Skip everything the local window already covers (and the current
        # message itself, which the orchestrator treats as the question).
        if local and created_at >= oldest_local_ts:
            continue
        redacted, _count = redact_text(text)
        await _admit(Turn(role=role, text=redacted, ts=created_at))
    return memory.turns


async def _persist_memory(
    session: AsyncSession,
    *,
    event: ClaimedEvent,
    question: str,
    outcome: Any,
) -> None:
    """Persist the redacted turns and any durable facts (plan 2.1/2.5).

    Rides the same transaction as the run: a crash mid-run rolls the turns
    back with the run, so a reclaimed event can never double-persist memory.
    Only CUSTOMER turns feed fact extraction - the agent's own statements
    must never become its memory (model self-feedback).
    """
    from platform_core.agent_runtime import conversation_store
    from platform_core.agent_runtime.conversation import (
        Turn,
        TurnRole,
        extract_durable_facts,
    )
    from platform_core.agent_runtime.qa_path import ABSTAIN_CLARIFICATION

    conversation_ref_id = _conversation_ref(event)
    if conversation_ref_id is None:
        return
    now = int(time.time())
    customer_turn = Turn(role=TurnRole.CUSTOMER, text=question, ts=now)
    turn_id = await conversation_store.append_turn(
        session,
        tenant_id=event.tenant_id,
        conversation_ref_id=conversation_ref_id,
        turn=customer_turn,
        source="chatwoot",
    )
    if outcome.status.value == "completed" and outcome.answer_text:
        await conversation_store.append_turn(
            session,
            tenant_id=event.tenant_id,
            conversation_ref_id=conversation_ref_id,
            turn=Turn(role=TurnRole.AGENT, text=outcome.answer_text, ts=now + 1),
            source="platform",
        )
    elif outcome.status.value == "abstained" and outcome.abstain_reason:
        ref = "clarify:" + outcome.abstain_reason
        if outcome.abstain_reason != ABSTAIN_CLARIFICATION:
            ref = "abstain:" + outcome.abstain_reason
        await conversation_store.append_turn(
            session,
            tenant_id=event.tenant_id,
            conversation_ref_id=conversation_ref_id,
            turn=Turn(role=TurnRole.AGENT, text=outcome.answer_text, ts=now + 1, ref=ref),
            source="platform",
        )

    contact_id = event.minimized_payload.get("contact_id")
    if not contact_id:
        return
    facts = extract_durable_facts([customer_turn])
    await conversation_store.upsert_facts(
        session,
        tenant_id=event.tenant_id,
        contact_ref=conversation_store.contact_ref_from_external(event.tenant_id, str(contact_id)),
        facts=conversation_store.fact_tuples(facts),
        source_turn_id=turn_id,
        now=now,
    )


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
        get_metrics().inbox_events_total.labels(result="ignored").inc()
        return None

    if not is_customer_message(event):
        # Either the agent's own reply looping back, or an event type we do
        # not treat as a question. Recorded and acked, never answered.
        logger.info(
            "event_skipped_not_customer",
            delivery_id=event.delivery_id,
            message_type=str(event.minimized_payload.get("message_type")),
        )
        # A separate label from "ignored": a non-zero count here is the
        # self-reply-loop signal, and it must be alertable on its own rather
        # than hidden inside general lifecycle chatter.
        get_metrics().inbox_events_total.labels(result="skipped_not_customer").inc()
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

    from platform_core.agent_runtime import conversation_store

    ctx = TenantContext(tenant_id=event.tenant_id, actor_id=None, actor_kind="system")
    await apply_rls_tenant(session, ctx)

    trace = new_trace_context(service_name="worker")
    metrics = get_metrics()
    if event.received_at:
        metrics.inbox_claim_age_seconds.observe(max(0.0, time.time() - event.received_at))

    orchestrator = AgentOrchestrator(session, deps)
    history = await load_history(session, event, deps)
    known_facts: list[tuple[str, str]] = []
    contact_id_early = event.minimized_payload.get("contact_id")
    if contact_id_early:
        known_facts = await conversation_store.load_facts(
            session,
            tenant_id=event.tenant_id,
            contact_ref=conversation_store.contact_ref_from_external(
                event.tenant_id, str(contact_id_early)
            ),
        )
    outcome = await orchestrator.run(
        tenant_id=event.tenant_id,
        conversation_ref_id=conversation_ref_id,
        question=question,
        principal=AI_PRINCIPAL,
        trace=trace,
        chatwoot_account_id=str(event.minimized_payload.get("chatwoot_account_id") or ""),
        chatwoot_conversation_id=str(event.minimized_payload.get("conversation_id") or ""),
        history=history,
        known_facts=known_facts,
    )
    await _persist_memory(session, event=event, question=question, outcome=outcome)

    metrics.inbox_events_total.labels(result=outcome.status.value).inc()
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
    reclaim_timeout_seconds: int = STALE_PROCESSING_SECONDS,
) -> int:
    """Claim and process one batch. Returns the number of rows finalised.

    A failure on one event is isolated: it is marked FAILED with the error
    recorded and the batch continues, so one poison payload cannot stall
    the queue.
    """
    reclaimed = await reclaim_stale_processing(session, timeout_seconds=reclaim_timeout_seconds)
    if reclaimed:
        # Worth a log line: a nonzero count means a previous worker died
        # mid-run and real questions went unanswered until now.
        logger.warning("stale_claims_reclaimed", count=reclaimed)
        get_metrics().stale_claims_reclaimed_total.inc(reclaimed)

    from platform_core.config import get_settings

    events = await claim_events(
        session, batch=batch, priority=get_settings().priority_claim_enabled
    )
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
            get_metrics().inbox_events_total.labels(result="failed").inc()
        else:
            await mark_completed(session, event.event_id)
        processed += 1
    return processed
