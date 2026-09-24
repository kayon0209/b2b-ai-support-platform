"""A human agent's reply: the workbench's missing verb.

Everything else in this platform could show an agent what was happening and
nothing could let them answer. The customer surface reads turns back, the
channel adapters deliver them, the lease model decides who owns the
conversation, canned replies exist to be inserted - and there was no write path
that turned a person's sentence into a customer-visible message. A support desk
where the human cannot speak is a dashboard.

Three decisions, each of which is the difference between a reply that arrives
and one that does not:

1. **Replying is taking over.** The lease is acquired and transferred to the
   human *before* the turn is written, in the same transaction. Without it the
   AI can be mid-generation on the same conversation and both messages reach the
   customer - and the lease is the only mechanism that prevents it. This is
   `AGENTS.md` rule 8 read the other way round: the AI must re-check the lease
   before sending, so the human must *hold* it in order for their own send to be
   the one that survives.

2. **The text is stored verbatim.** See `append_authored_turn` for why redaction
   would corrupt rather than protect. What matters here is the consequence: the
   stored copy is what the customer received, so "what did we tell them" has an
   answer.

3. **Delivery is a queued event, not an HTTP call.** The transports live in the
   worker (`worker.wiring.build_interactive_deps`), because that is where the
   circuit breakers and retry accounting already are. Sending from the API
   process would mean a second set of transports with a second notion of "is
   SMTP up", and a request that fails after the turn is committed would leave a
   message the customer never got with nothing scheduled to retry it.

The payload carries the turn id, not the text: the turn is the record, and a
copy in the outbox would be a second one that can disagree.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from observability import JsonLogger
from platform_core.agent_runtime import conversation_store
from platform_core.agent_runtime.conversation import TurnRole
from platform_core.agent_runtime.models import KNOWN_ORIGINS, ORIGIN_UNKNOWN
from platform_core.cases.service import CaseService, cases_for_conversation
from platform_core.identity import lease_service
from platform_core.outbox_service import enqueue
from platform_core.support_bridge.continuity import delivery_target

logger = JsonLogger("platform.agent_runtime.agent_reply")

# Bound so a paste-bomb cannot become a row and an outbound send. Matches the
# customer path's limit (`MessageIn`), so a conversation cannot hold one side's
# messages at a length the other side is forbidden from using.
MAX_REPLY_CHARS = 4000

# The event the worker's relay handles. Named after the act, not the transport:
# the same event delivers over email or WeChat depending on where the
# conversation came from, and a transport-named event would need a second one
# the day a third channel arrives.
AGENT_REPLY_EVENT = "conversation.agent_reply"

# `source` on the turn. Distinct from "platform" (a customer typing into
# `/support`) and from "chatwoot" (historical). `/conversations` and the
# workbench both display it, and "who said this" is the first thing a reviewer
# asks when a reply is wrong.
AGENT_REPLY_SOURCE = "agent"


class AgentReplyError(ValueError):
    """A refused reply. Mapped to 400/409 by the router."""


@dataclass(frozen=True)
class AgentReplyResult:
    turn_id: uuid.UUID
    lease_version: int
    # "channel" when an outbound event was queued, "platform" when persisting
    # the turn *is* the delivery (`/support` reads it back). Reported rather
    # than inferred: a caller that cannot tell the two apart will eventually
    # report a platform-surface reply as undelivered.
    delivery: str
    channel: str | None
    event_id: uuid.UUID | None


async def send_agent_reply(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    conversation_ref_id: uuid.UUID,
    text: str,
    agent_ref: str,
    trace_id: str | None = None,
    origin: str = ORIGIN_UNKNOWN,
    canned_reply_id: uuid.UUID | None = None,
    turn_id: uuid.UUID | None = None,
) -> AgentReplyResult:
    """Record a human's reply and arrange for it to reach the customer.

    The caller owns the transaction and the RLS binding. Everything below
    commits together, so a failure cannot leave the lease transferred with no
    message, or a message with no delivery scheduled.
    """
    body = (text or "").strip()
    if not body:
        raise AgentReplyError("a reply needs some text")
    if len(body) > MAX_REPLY_CHARS:
        raise AgentReplyError(f"a reply may not exceed {MAX_REPLY_CHARS} characters")
    owner = (agent_ref or "").strip()
    if not owner:
        raise AgentReplyError("a reply must name the agent who sent it")
    if origin not in KNOWN_ORIGINS:
        # Refused rather than stored: an unrecognised value would silently
        # dilute the adoption rate it exists to measure, and a typo is cheaper
        # to fix at the call site than in a dashboard nobody trusts.
        raise AgentReplyError(f"unknown reply origin: {origin!r}")

    # 1. Ownership. Acquire first because `transfer_to_human` refuses a missing
    #    row ("lease row missing; acquire first") - the two calls are one
    #    operation and belong together.
    await lease_service.acquire_or_get(
        session, tenant_id=tenant_id, conversation_ref_id=conversation_ref_id
    )
    current = await lease_service.lease_snapshot(
        session, tenant_id=tenant_id, conversation_ref_id=conversation_ref_id, for_update=True
    )
    if current is not None and current.owner_type == "closed":
        raise AgentReplyError("this conversation has ended")
    if current is not None and current.owner_type == "human" and current.owner_ref != owner:
        raise AgentReplyError("another agent owns this conversation")
    lease_version = await lease_service.transfer_to_human(
        session,
        tenant_id=tenant_id,
        conversation_ref_id=conversation_ref_id,
        human_ref=owner,
        reason="agent reply",
    )

    # 2. The record. Verbatim - see the module docstring.
    turn_id = await conversation_store.append_authored_turn(
        session,
        tenant_id=tenant_id,
        conversation_ref_id=conversation_ref_id,
        text=body,
        role=TurnRole.AGENT,
        source=AGENT_REPLY_SOURCE,
        origin=origin,
        canned_reply_id=canned_reply_id,
        author_ref=owner,
        turn_id=turn_id,
    )
    await lease_service.mark_waiting_for_customer(
        session, tenant_id=tenant_id, conversation_ref_id=conversation_ref_id, actor_ref=owner
    )

    # 2b. The human answered, so the first-response clock is satisfied. Through
    #     the case command rather than a direct UPDATE: the transition rule, the
    #     version bump and the `last_state_changed_at` bookkeeping live in one
    #     place, and a bulk UPDATE that also moved `last_state_changed_at` would
    #     silently drop the SLA time accrued since the previous state change.
    for case_id in await cases_for_conversation(
        session, tenant_id=tenant_id, conversation_ref_id=conversation_ref_id
    ):
        try:
            await CaseService(session).apply_command(
                tenant_id=tenant_id, case_id=case_id, command="record_first_response"
            )
        except LookupError:
            # The link points at a case that is gone. That is an inconsistency
            # worth knowing about, but not a reason to withhold a customer's
            # reply - the message is the customer-facing act.
            logger.warning("agent_reply_case_missing", conversation_ref_id=str(conversation_ref_id))

    # 3. Delivery. Where the customer is, if anywhere.
    target = await delivery_target(
        session, tenant_id=tenant_id, conversation_ref_id=conversation_ref_id
    )
    if target is None:
        return AgentReplyResult(
            turn_id=turn_id,
            lease_version=lease_version,
            delivery="platform",
            channel=None,
            event_id=None,
        )

    channel, address, conversation_key = target
    event_id = await enqueue(
        session,
        tenant_id=tenant_id,
        event_type=AGENT_REPLY_EVENT,
        aggregate_type="conversation",
        aggregate_id=str(conversation_ref_id),
        payload={
            "conversation_ref_id": str(conversation_ref_id),
            # The turn id, not the text: one record, not two that can disagree.
            "turn_id": str(turn_id),
            "channel": channel,
            "address": address,
            # The thread to join. Empty when the row predates the column; the
            # handler sends anyway rather than refusing, because a reply in a
            # new thread is worse than nothing but much better than silence.
            "conversation_key": conversation_key,
            "agent_ref": owner,
        },
        trace_id=trace_id,
    )
    return AgentReplyResult(
        turn_id=turn_id,
        lease_version=lease_version,
        delivery="channel",
        channel=channel or None,
        event_id=event_id,
    )
