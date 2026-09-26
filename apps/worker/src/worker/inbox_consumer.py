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

import asyncio
import contextlib
import os
import socket
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from observability import JsonLogger, new_trace_context
from observability_metrics import get_metrics
from platform_core.agent_runtime.conversation import Turn
from platform_core.agent_runtime.models import RunStatus
from platform_core.agent_runtime.orchestrator import AgentOrchestrator, OrchestratorDeps
from platform_core.agent_runtime.semantic.validator import CapabilityView
from platform_core.db import app_role_url, session_scope_with_url
from platform_core.identity.tenant_context import TenantContext, tenant_session
from platform_core.retrieval.hybrid import PrincipalScope
from platform_core.support_bridge.conversation_ref import conversation_ref_for
from platform_core.support_bridge.models import InboxEvent, InboxEventStatus

logger = JsonLogger("platform.worker")

# How long a claimed row may sit in PROCESSING before another worker may
# take it. Must comfortably exceed the slowest realistic run: a knowledge
# answer with retrieval measured ~30 s end to end, so 10 minutes leaves a
# wide margin while still bounding how long a question can go unanswered
# after a crash.
STALE_PROCESSING_SECONDS = 600

# How often a worker holding a row re-announces that it is still on it. Well
# inside `STALE_PROCESSING_SECONDS`: a run that heartbeats is never reclaimable
# no matter how long it takes, which is what separates "slow" from "dead".
HEARTBEAT_INTERVAL_SECONDS = 60

# Which process holds a row. Written at claim time so a stuck row can be
# attributed to a worker rather than merely counted.
WORKER_ID = f"{socket.gethostname()}:{os.getpid()}"

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

# `AI_PRINCIPAL` is declared above; nothing else belongs in this module's
# constants - in particular, do not reintroduce a special case here for a
# run that failed to send. `_dispatch` now treats "no Chatwoot account" as
# "no external channel", so a platform-native conversation completes and is
# persisted by the normal path. A surviving OUTBOUND_TARGET_MISSING means a
# half-configured Chatwoot target, and publishing that answer would claim a
# delivery that never happened.


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
    now = int(time.time())
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
        .values(
            status=InboxEventStatus.PROCESSING.value,
            # The claim timestamp is what separates "this worker died holding
            # it" from "this row has been waiting a long time". Writing only
            # the status made those two indistinguishable, and a backlog was
            # enough to confuse them - see `reclaim_stale_processing`.
            claimed_at=now,
            heartbeat_at=now,
            worker_id=WORKER_ID,
        )
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

    **The predicate is the claim, not the arrival.** It used to compare
    `received_at` alone, which answers "how long did this wait" and says
    nothing about who is working on it now. Under a backlog that is enough to
    rob a live claim: the row waited past the threshold, a worker finally took
    it, and the next poll handed it straight back - two workers on one customer
    message. The outbound id hides the duplicate reply, so nothing looks wrong
    while the model and the tools run twice.

    `received_at` stays in the coalesce as the fallback for rows written before
    this shipped, which have no claim timestamp; it is the last resort, not the
    rule. `received_at` is otherwise untouched and remains the FIFO key.
    """
    cutoff = int(time.time()) - timeout_seconds
    liveness = func.coalesce(InboxEvent.heartbeat_at, InboxEvent.claimed_at, InboxEvent.received_at)
    result = await session.execute(
        update(InboxEvent)
        .where(
            InboxEvent.status == InboxEventStatus.PROCESSING.value,
            liveness < cutoff,
        )
        .values(
            status=InboxEventStatus.RECEIVED.value,
            # The next worker gets a clean claim; leaving the dead worker's
            # timestamp here would make a freshly re-queued row look abandoned
            # on the following poll.
            claimed_at=None,
            heartbeat_at=None,
            worker_id=None,
        )
    )
    return result.rowcount or 0  # type: ignore[attr-defined]


async def heartbeat_claim(event_id: uuid.UUID) -> None:
    """Move the liveness stamp forward for a row this worker still holds.

    Uses its own short-lived session on the administrative connection: the
    caller's session is mid-batch and must not be shared across a concurrent
    write. A failure here is logged and swallowed - the claim is still
    recoverable either way, and a heartbeat outage must not abort the run it
    was protecting.
    """
    from platform_core.db import get_session_factory

    try:
        async with get_session_factory()() as session:
            await session.execute(
                update(InboxEvent)
                .where(
                    InboxEvent.id == event_id,
                    InboxEvent.status == InboxEventStatus.PROCESSING.value,
                )
                .values(heartbeat_at=int(time.time()))
            )
            await session.commit()
    except Exception as exc:  # noqa: BLE001 - liveness aid, never fatal
        logger.warning("claim_heartbeat_failed", error_code=type(exc).__name__)


@contextlib.asynccontextmanager
async def keepalive(event_id: uuid.UUID) -> AsyncIterator[None]:
    """Hold a claim open for as long as the work takes.

    Yields immediately and schedules a periodic heartbeat for the duration.
    Without it, a run that legitimately exceeds the reclaim threshold is
    indistinguishable from a worker that died holding it, and the only two
    possible answers are a duplicate or a stuck row.
    """

    async def beat() -> None:
        while True:
            await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)
            await heartbeat_claim(event_id)

    task = asyncio.create_task(beat())
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


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

    Two encodings, and the payload says which one it used:

    - `conversation_ref` - the platform id, already resolved by the writer.
      Used verbatim. This is how a run queued by an operator, who reached the
      conversation by its platform ref and holds no channel id, names the
      conversation.
    - `conversation_id` - a channel's own id (a Chatwoot conversation id, a
      visitor id). Only this side can turn it into a ref.

    Deriving the first would hash an id that is already a hash, filing the run
    under a conversation no writer ever wrote to: the answer is produced and
    then never found, and nothing raises. Preferring the explicit key is what
    makes the two encodings distinguishable - both are UUIDs, so there is no
    way to tell them apart from the value itself.

    The derivation is shared, not reimplemented here: the API's customer and
    run endpoints answer questions about the same conversation, and a second
    copy of the rule is how they end up disagreeing about which conversation
    they are talking about.
    """
    resolved = event.minimized_payload.get("conversation_ref")
    if isinstance(resolved, str) and resolved.strip():
        try:
            return uuid.UUID(resolved)
        except ValueError:
            # A writer that put a non-UUID here is broken, and the alternative
            # to returning None is crashing the consumer loop. Logged by the
            # caller as `no_conversation_ref`, which is the same outcome as an
            # unusable channel id.
            return None

    external = event.minimized_payload.get("conversation_id")
    if not isinstance(external, str) or not external.strip():
        return None
    # Passed through untrimmed: the shared helper hashes the id exactly as
    # given, and trimming here would move stored conversations.
    return conversation_ref_for(event.tenant_id, external)


async def _local_turn_text(message_id: object) -> str | None:
    """Redacted text of a platform-persisted turn, or None.

    Goes through the `resolve_turn_text` SECURITY DEFINER function because
    this runs outside an HTTP request: there is no `app.tenant_id` binding,
    and under FORCE RLS a direct select would return nothing silently.
    """
    try:
        turn_id = uuid.UUID(str(message_id))
    except (ValueError, AttributeError, TypeError):
        # Not a local turn id — a Chatwoot message id, most likely.
        return None
    try:
        async with session_scope_with_url(app_role_url()) as session:
            row = (
                await session.execute(text("SELECT resolve_turn_text(:m)"), {"m": turn_id})
            ).scalar_one_or_none()
    except Exception as exc:  # noqa: BLE001 - fall through to the Chatwoot read
        # Logged, not swallowed. A bare `return None` here made every
        # platform-originated question indistinguishable from "no body
        # found": the event was acked, the run sat queued, and nothing in
        # the log said why.
        # `error_code`, not `error`: a free-text field is dropped by the
        # allowlist, and forwarding an exception message would also put
        # arbitrary text - possibly a customer's own words, quoted by the
        # exception - into the log stream. The class name is what gets
        # grepped for anyway.
        logger.warning(
            "local_turn_read_failed",
            new_trace_context(service_name="worker"),
            turn_id=str(turn_id),
            error_code=type(exc).__name__,
        )
        return None
    return row if isinstance(row, str) and row.strip() else None


async def resolve_question(event: ClaimedEvent, deps: OrchestratorDeps) -> str | None:
    """Obtain the customer question text for this event.

    The inbox row stores minimized metadata only (docs/security.md: no raw
    customer content at rest), so the body is read from the platform's own
    `conversation_turns` by turn id — the path both `/support` and every
    channel adapter write through. A payload that already carries `content`
    (tests, connectors) is used directly.

    This used to fall back to fetching the body from Chatwoot's API when the
    local copy was missing. Chatwoot is gone (ADR 0012), and with it the only
    producer of turns that were not persisted locally, so the fallback had no
    case left to serve.
    """
    del deps
    content = event.minimized_payload.get("content")
    if isinstance(content, str) and content.strip():
        return content

    message_id = event.minimized_payload.get("message_id")
    if not message_id:
        return None
    return await _local_turn_text(message_id)


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

    Local redacted turns are the whole history now: they are written by this
    platform, already redacted, and — since ADR 0012 — the only store there
    is. There is no second source to merge with.

    Failure anywhere degrades to whatever was loaded: multi-turn is an
    enhancement, and a run that cannot read history still answers the
    question in front of it.
    """
    from platform_core.agent_runtime import conversation_store
    from platform_core.agent_runtime.conversation import ConversationMemory
    from platform_core.config import get_settings

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

    del deps
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
    # `source` names where the turn actually came from. Every turn is the
    # platform's now: `/support` and the channel adapters all persist through
    # `append_customer_turn`. Labelling one "chatwoot" credited a system of
    # record that no longer holds anything, and read as a second, duplicate
    # question on the timeline.
    turn_source = "platform"

    # A question from the platform's own chat surface was already persisted when
    # the message was accepted - `POST /v1/support/messages` and
    # `/v1/customer/.../messages` both write the turn before queueing the run,
    # so that the customer's own copy exists even if the run never executes.
    # Appending it here as well put every such question on the timeline twice:
    # measured in the browser, the customer saw their own message duplicated.
    # The turn is addressed by id, so the existing row is reused rather than
    # rewritten, which also keeps fact extraction pointing at the real turn.
    already_stored = await _local_turn_text(event.minimized_payload.get("message_id"))
    if already_stored is not None:
        turn_id = uuid.UUID(str(event.minimized_payload["message_id"]))
    else:
        turn_id = await conversation_store.append_turn(
            session,
            tenant_id=event.tenant_id,
            conversation_ref_id=conversation_ref_id,
            turn=customer_turn,
            source=turn_source,
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
    # No branch here for a run that failed to send. A conversation the
    # platform owns no longer fails at all - `_dispatch` treats "no Chatwoot
    # account" as "no external channel", so it completes and lands in the
    # branch above. What is left of OUTBOUND_TARGET_MISSING is a half-configured
    # target, where we did mean to reach Chatwoot and could not; publishing
    # that answer to our own surface would claim a delivery that never
    # happened for a customer who is not looking here.

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


async def _resolve_contact_id(event: ClaimedEvent, deps: OrchestratorDeps) -> str | None:
    """Who sent this message, for account lookup. None when unknowable.

    The webhook payload is tried first, but it does not carry the contact in
    this deployment - measured over every stored event: `contact_id` 0/29,
    `sender_id` 1/29. So the fallback reads it from the message via the
    Chatwoot API, which is the only place it exists.

    Returning None is normal, not an error: an unbound contact is most
    contacts, and routing simply proceeds without a tier.
    """
    payload_contact = event.minimized_payload.get("contact_id")
    if payload_contact:
        return str(payload_contact)

    # No payload contact and nothing stored: this event carries no contact, and
    # inventing one would attribute the run to the wrong customer. There used to
    # be a fallback that asked Chatwoot which contact sent the message; every
    # producer stores the id on the payload now, so it had no case left.
    del deps
    return None


def _attachment_types(event: object) -> list[str]:
    """Attachment content types the minimiser kept, or [].

    Defensive about the shape because the payload comes from outside: a
    hostile or merely unexpected value must not fail the run, and an
    attachment we cannot classify is simply not reported.
    """
    payload = getattr(event, "minimized_payload", None)
    if not isinstance(payload, dict):
        return []
    types = payload.get("attachment_types")
    if not isinstance(types, list):
        return []
    return [str(item) for item in types if isinstance(item, str)]


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
        # Logged, not silent. A missing or non-string `conversation_id` used
        # to return here with no trace at all, which is indistinguishable
        # from "the worker never ran" - and it cost hours of chasing a
        # reader that was never broken.
        #
        # Both keys, because there are two encodings now and seeing which one
        # a bad row carried is the whole diagnosis: `conversation_ref` present
        # but unparseable means the writer is broken, both absent means the
        # row never named a conversation at all.
        logger.warning(
            "event_skipped_no_conversation_ref",
            delivery_id=event.delivery_id,
            event_type=event.event_type,
            # Named `conversation_ref_id`, not `conversation_ref`: the log schema
            # (`observability.ALLOWED_LOG_FIELDS`) allowlists the former and
            # drops the latter silently - which is the one outcome a diagnostic
            # line exists to avoid. The value is the payload's raw `conversation_ref`
            # as received; a non-UUID here is the whole reason this branch ran.
            conversation_ref_id=str(event.minimized_payload.get("conversation_ref") or ""),
            conversation_id=str(event.minimized_payload.get("conversation_id") or ""),
        )
        get_metrics().inbox_events_total.labels(result="no_conversation_ref").inc()
        return None

    question = await resolve_question(event, deps)
    if question is None:
        # No readable body: record the row and leave it. Answering without
        # the customer's question is never acceptable.
        #
        # Also logged rather than silent: this is the branch that produces
        # "the event completed but no run exists", and without a line here
        # there is nothing to distinguish it from a worker that is simply
        # not running.
        logger.warning(
            "event_skipped_unreadable_question",
            delivery_id=event.delivery_id,
            conversation_ref_id=str(conversation_ref_id),
            # Metadata only - never the customer's text.
            message_id=str(event.minimized_payload.get("message_id") or ""),
        )
        get_metrics().inbox_events_total.labels(result="unreadable_question").inc()
        return None

    from platform_core.agent_runtime import conversation_store

    # The RLS binding is the caller's: `drain_once` hands this function a
    # `tenant_session`, which connects as the non-bypass app role and re-binds
    # the tenant at the start of every transaction (`_event_context` is the
    # context it was opened with). Re-applying it here would not be enough on
    # its own - the run commits (the control lease), and a transaction-scoped
    # `set_config` does not survive a commit.

    trace = new_trace_context(service_name="worker")
    metrics = get_metrics()
    if event.received_at:
        metrics.inbox_claim_age_seconds.observe(max(0.0, time.time() - event.received_at))

    orchestrator = AgentOrchestrator(session, deps)
    history = await load_history(session, event, deps)
    known_facts: list[tuple[str, str]] = []
    contact_id_early = await _resolve_contact_id(event, deps)
    if contact_id_early:
        known_facts = await conversation_store.load_facts(
            session,
            tenant_id=event.tenant_id,
            contact_ref=conversation_store.contact_ref_from_external(
                event.tenant_id, str(contact_id_early)
            ),
        )
    # Feature 2.5: resolve the visitor's ownership proof before the run. "" =
    # anonymous visitor (the gate fires); None = operator run (no gate); a
    # non-empty string = the verified account the run may read. Preserve "":
    # collapsing it to None would let an anonymous visitor read order data, and
    # collapsing a missing key to "" would gate operators out of their own reads.
    _va = event.minimized_payload.get("verified_account")
    verified_account = _va if isinstance(_va, str) else None

    outcome = await orchestrator.run(
        tenant_id=event.tenant_id,
        conversation_ref_id=conversation_ref_id,
        question=question,
        principal=AI_PRINCIPAL,
        trace=trace,
        channel_conversation_key=str(event.minimized_payload.get("conversation_id") or ""),
        history=history,
        known_facts=known_facts,
        contact_id=str(contact_id_early) if contact_id_early else None,
        # 1.3: what the customer attached, as content types only. The
        # minimiser already dropped everything else; this just carries the
        # metadata through so a handoff can say evidence was supplied.
        attachment_types=_attachment_types(event),
        # Feature 2.5: "" = anonymous visitor (gate fires); None = operator run
        # (no gate); a non-empty string = the verified account the run may read.
        verified_account=verified_account,
        # ADR 0014: which channel to answer on. Absent for the platform's own
        # surface, where `_dispatch` already knows what to do - so this is
        # None, not "".
        channel_system=str(event.minimized_payload.get("channel_system") or "") or None,
        # Where to answer: the customer's email address, or the WeChat openid.
        # The channel route stores it as `contact_id`.
        channel_address=str(contact_id_early) if contact_id_early else None,
    )
    await _persist_memory(session, event=event, question=question, outcome=outcome)

    # Shadow classification is *enqueued*, not performed.
    #
    # The previous revision awaited the model call here, inside the per-event
    # session, before returning so the row could be marked COMPLETED. That made
    # the claim in this function's comment false: the customer's event was not
    # complete until a classification finished, so a slow provider delayed
    # event completion and held a connection for the duration.
    #
    # What happens here is now only: resolve the flag, and if shadow is on,
    # write an outbox row in the same transaction as the run. The classification
    # itself runs in `shadow_consumer`, in its own tenant session, with its own
    # deadline, quota and expiry. Nothing on the customer path waits for it.
    shadow_enqueued = await _enqueue_shadow(
        session,
        tenant_id=event.tenant_id,
        conversation_ref_id=conversation_ref_id,
        turn_id=str(event.minimized_payload.get("message_id") or event.delivery_id),
        question=question,
        history=[(t.ref, t.text) for t in history[-8:] if getattr(t, "ref", None)],
        turn_created_at=int(event.received_at or time.time()),
    )

    # Conversation tasks, from a real assessment.
    #
    # B1-02: `plan_tasks` and `create_or_get` had no caller, so a customer
    # message produced no tasks and the workbench panel had nothing to show.
    #
    # This runs *after* the run is persisted and the answer dispatched, and
    # only when `agent.conversation_tasks` is on. With the flag off it reads
    # one flag row and returns; the customer's path is byte-for-byte what it
    # was before R1. Unlike the shadow work it is synchronous, because a task
    # the agent is expected to act on has to exist by the time they open the
    # conversation - deferring it would make the panel empty on first render
    # and the feature would look broken rather than slow.
    tasks_created = await _plan_conversation_tasks(
        session,
        tenant_id=event.tenant_id,
        conversation_ref_id=conversation_ref_id,
        question=question,
        history=[(t.ref, t.text) for t in history[-8:] if getattr(t, "ref", None)],
        lease_owner_type="ai",
        deps=deps,
        turn_created_at=int(event.received_at or time.time()),
    )

    metrics.inbox_events_total.labels(result=outcome.status.value).inc()
    logger.info(
        "event_processed",
        trace,
        delivery_id=event.delivery_id,
        run_id=str(outcome.run_id),
        status=outcome.status.value,
        route=outcome.route,
        latency_ms=outcome.latency_ms,
        # Counts only. The task rows carry the detail, and a log line that
        # quoted a task would be a copy of customer content in a place with
        # weaker access control. `count` is the allowlisted name; a new field
        # would have to be added to the redaction boundary's schema, and this
        # does not justify widening it.
        count=tasks_created,
    )
    if shadow_enqueued:
        get_metrics().inbox_events_total.labels(result="shadow_enqueued").inc()
    return outcome.status


async def _plan_conversation_tasks(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    conversation_ref_id: uuid.UUID,
    question: str,
    history: list[tuple[str, str]],
    lease_owner_type: str,
    deps: OrchestratorDeps,
    turn_created_at: int,
) -> int:
    """Analyse the turn and persist whatever tasks it implies. Returns a count.

    Gated twice: `agent.conversation_tasks` must be on, and the process kill
    switch must be up. With either off this returns before touching a table.

    Every failure is swallowed. The customer's answer is already committed by
    the time this runs, and a planner error must not mark their message failed
    and trigger a retry that re-answers them.
    """
    from platform_core.agent_runtime.semantic.context import build_context
    from platform_core.agent_runtime.semantic.contracts import SemanticMode
    from platform_core.agent_runtime.semantic.modes import (
        FLAG_ASSIST,
        FLAG_TASKS,
        resolve_mode,
    )
    from platform_core.agent_runtime.semantic.service import (
        AnalysisRequest,
        SemanticBudget,
        analyze,
    )
    from platform_core.agent_runtime.semantic.shadow import capabilities_for_shadow
    from platform_core.agent_runtime.tasks.planning_seam import run_task_planning
    from platform_core.config import get_settings
    from platform_core.knowledge import flag_service
    from platform_core.tool_gateway import registry

    try:
        decisions = await flag_service.evaluate_many(
            session,
            flag_keys=[FLAG_TASKS, FLAG_ASSIST],
            tenant_id=tenant_id,
            defaults={FLAG_TASKS: False, FLAG_ASSIST: False},
        )
        if not decisions.get(FLAG_TASKS) or not decisions[FLAG_TASKS].enabled:
            return 0
        # Tasks come from an assist-mode suggestion. A tenant that turned on
        # task persistence but not assist has asked for the storage without the
        # suggestion, and there is nothing to store.
        if not decisions.get(FLAG_ASSIST) or not decisions[FLAG_ASSIST].enabled:
            return 0
        resolution = resolve_mode(get_settings(), {k: d.enabled for k, d in decisions.items()})
        if resolution.mode is SemanticMode.OFF:
            return 0

        from worker import shadow_consumer

        provider = shadow_consumer.chat_provider(deps)
        if provider is None:
            return 0

        await registry.ensure_tool_definitions(session, tenant_id=tenant_id)
        capabilities = capabilities_for_shadow(await _tenant_tool_names(session, tenant_id))

        ctx = build_context(
            current_turn_id=str(turn_created_at),
            current_text=question,
            history=history,
            mode=SemanticMode.ASSIST,
            capabilities=capabilities,
        )
        assessment = await analyze(
            AnalysisRequest(context=ctx, lease_owner_type=lease_owner_type),
            provider=provider,
            capabilities=capabilities,
            # Longer than the shadow budget: this one is on the request path
            # and its result is meant to be visible to the agent immediately.
            budget=SemanticBudget(deadline_seconds=3.0, max_retries=0),
        )
        outcome = await run_task_planning(
            session,
            tenant_id=tenant_id,
            conversation_ref_id=conversation_ref_id,
            assessment=assessment,
            capabilities=capabilities,
        )
        if outcome.created:
            get_metrics().inbox_events_total.labels(result="tasks_created").inc(outcome.created)
        return outcome.created
    except Exception as exc:  # noqa: BLE001 - planning must not fail the message
        logger.warning(
            "task_planning_failed",
            conversation_ref_id=str(conversation_ref_id),
            error_code=type(exc).__name__,
        )
        return 0


async def _enqueue_shadow(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    conversation_ref_id: uuid.UUID,
    turn_id: str,
    question: str,
    history: list[tuple[str, str]],
    turn_created_at: int,
) -> bool:
    """Queue a shadow classification if this tenant has shadow mode on.

    Returns whether a row was written. No model call happens here, and no
    failure propagates: a shadow enqueue that cannot be written must not fail
    an event whose customer answer is already committed.

    The outbox row is the *request*. It carries the turn text because the
    consumer needs it and the consumer is asynchronous; it is written inside
    the tenant transaction, so it is as protected as the run itself, and the
    consumer re-reads the turn from `conversation_turns` rather than trusting a
    long-lived copy in a queue payload.
    """
    from platform_core.agent_runtime.semantic.modes import FLAG_SHADOW, resolve_mode
    from platform_core.agent_runtime.semantic.shadow import SHADOW_EVENT_TYPE
    from platform_core.config import get_settings
    from platform_core.knowledge import flag_service
    from platform_core.outbox_service import enqueue

    try:
        decisions = await flag_service.evaluate_many(
            session,
            flag_keys=[FLAG_SHADOW],
            tenant_id=tenant_id,
            defaults={FLAG_SHADOW: False},
        )
        resolution = resolve_mode(get_settings(), {k: d.enabled for k, d in decisions.items()})
        if resolution.mode.value != "shadow":
            return False

        await enqueue(
            session,
            tenant_id=tenant_id,
            event_type=SHADOW_EVENT_TYPE,
            aggregate_type="conversation",
            aggregate_id=str(conversation_ref_id),
            payload={
                "conversation_ref": str(conversation_ref_id),
                "turn_id": turn_id,
                "turn_created_at": turn_created_at,
                # Carried so the consumer can run without re-reading a
                # minimised inbox payload, and so a turn that has since been
                # pruned is still explainable. Bounded by the same truncation
                # the synchronous path would have applied.
                "question": question[:2000],
                "history": history,
            },
        )
        return True
    except Exception:  # noqa: BLE001 - shadow must never fail the customer path
        logger.warning("shadow_enqueue_failed", conversation_ref_id=str(conversation_ref_id))
        return False


async def _tenant_tool_names(
    session: AsyncSession, tenant_id: uuid.UUID
) -> dict[str, CapabilityView]:
    """The tenant's registered tools, with the registry's own risk class.

    Read through the tenant session, so RLS already restricts this to the
    tenant's rows plus the platform catalog. The risk class is the registry's
    value, never anything a model supplied.
    """
    from platform_core.tool_gateway.models import ToolDefinition

    rows = (
        await session.execute(
            select(ToolDefinition).where(
                (ToolDefinition.tenant_id == tenant_id) | ToolDefinition.tenant_id.is_(None)
            )
        )
    ).scalars()
    return {row.name: CapabilityView(tool_name=row.name, risk_class=row.risk) for row in rows}


async def drain_once(
    session: AsyncSession,
    *,
    deps: OrchestratorDeps,
    batch: int = 20,
    reclaim_timeout_seconds: int = STALE_PROCESSING_SECONDS,
) -> int:
    """Claim and process one batch. Returns the number of rows finalised.

    **Two roles, on purpose.** The session passed in is the *queue's bookkeeping*
    session - the owner role, because claiming must see every tenant's rows
    before any tenant is known (`worker.wiring.queue_bookkeeping_session`). Every
    event is then processed on its own `tenant_session`, which connects as
    `platform_app` and binds that event's tenant.

    Why this matters, measured rather than argued: with the whole batch on the
    owner session, RLS is bypassed for the run, so `flag_service` - which reads
    its row by key and relies on RLS to scope it - returned **another tenant's**
    `agent.business_read_enabled=False`. The run then skipped the read-tool
    branch, abstained, and published no receipt: the customer saw "I couldn't
    verify an answer" and the cause was a different tenant's configuration. The
    same bypass also meant every tenant-data read in the run was scoped by
    application code alone, with no third defence layer.

    The claim is committed before processing starts. It has to be: the claim
    holds `FOR UPDATE` locks on the rows it takes, and a per-event session on a
    different connection would contend for them. Committing first also makes the
    claim durable, which is what lets `reclaim_stale_processing` recover a row
    from a worker that dies mid-run - while the claim lived inside the batch
    transaction, a crash rolled it back and the row was simply back in the queue
    with the lock gone.

    A failure on one event is isolated: it is marked FAILED with the error
    recorded and the batch continues, so one poison payload cannot stall
    the queue. Each mark commits on its own, so a later crash cannot lose the
    accounting for events that already finished.
    """
    reclaimed = await reclaim_stale_processing(session, timeout_seconds=reclaim_timeout_seconds)
    if reclaimed:
        # Worth a log line: a nonzero count means a previous worker died
        # mid-run and real questions went unanswered until now.
        logger.warning("stale_claims_reclaimed", count=reclaimed)
        get_metrics().stale_claims_reclaimed_total.inc(reclaimed)
    await session.commit()

    from platform_core.config import get_settings

    events = await claim_events(
        session, batch=batch, priority=get_settings().priority_claim_enabled
    )
    await session.commit()

    processed = 0
    for event in events:
        try:
            async with keepalive(event.event_id):
                async with tenant_session(_event_context(event)) as event_session:
                    await process_event(event_session, event, deps=deps)
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
        await session.commit()
        processed += 1
    return processed


def _event_context(event: ClaimedEvent) -> TenantContext:
    """The context an event runs under. No actor: this is the platform acting.

    `actor_kind="system"` is honest labelling for the audit trail - the run was
    started by a message, not by a person - and `actor_id=None` is what the
    orchestrator already received as its principal.
    """
    return TenantContext(tenant_id=event.tenant_id, actor_id=None, actor_kind="system")
