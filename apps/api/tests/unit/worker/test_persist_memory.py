"""Unit tests: an answer that had nowhere to go must still be shown.

Regression tests for "the event completes, the run is marked failed, and the
customer never sees a reply" on platform-originated conversations.

The chain, as observed against the live stack:

1. A customer types into the platform's own chat surface. No Chatwoot
   conversation exists behind it, so the inbox event carries a
   `conversation_id` but no `chatwoot_account_id`.
2. The orchestrator generates an answer, then `_dispatch` refuses to send it
   (`OUTBOUND_TARGET_MISSING`) and marks the run FAILED.
3. `_persist_memory` only ever wrote an agent turn for a COMPLETED run, so
   the answer was produced, never persisted, and the timeline the chat
   surface polls stayed empty forever.

The fix is deliberately narrow. Only a run that produced an answer *and* was
blocked for want of a destination is published, because for a conversation
the platform owns, the platform surface is the delivery channel. A run
blocked because a real send failed, or because the outcome of a send is
unknown, must stay silent: showing a customer an answer they never received
is worse than showing none.
"""

import uuid
from collections.abc import Iterator
from typing import cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from platform_core.agent_runtime import conversation_store
from platform_core.agent_runtime.conversation import Turn
from platform_core.agent_runtime.models import RunStatus
from platform_core.agent_runtime.orchestrator import RunOutcome
from worker.inbox_consumer import OUTBOUND_TARGET_MISSING, ClaimedEvent, _persist_memory

TENANT_ID = uuid.UUID("17ab2c52-7d95-5fba-a06c-b5641393831e")

# `append_turn` is stubbed below, so the session is never dereferenced.
_SESSION = cast(AsyncSession, None)

# (role, text, ref, source) of every turn handed to the store.
RECORDED: list[tuple[str, str, str, str]] = []


async def _fake_append_turn(
    session: object,
    *,
    tenant_id: uuid.UUID,
    conversation_ref_id: uuid.UUID,
    turn: Turn,
    source: str,
) -> uuid.UUID:
    RECORDED.append(
        (
            str(turn.role.value),
            str(turn.text),
            str(getattr(turn, "ref", "") or ""),
            source,
        )
    )
    return uuid.uuid4()


@pytest.fixture(autouse=True)
def _stub_store(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    RECORDED.clear()
    monkeypatch.setattr(conversation_store, "append_turn", _fake_append_turn)
    yield
    RECORDED.clear()


def _event() -> ClaimedEvent:
    """A platform-originated event: no contact, so no fact extraction."""
    return ClaimedEvent(
        event_id=uuid.uuid4(),
        tenant_id=TENANT_ID,
        delivery_id=str(uuid.uuid4()),
        event_type="message_created",
        minimized_payload={
            "conversation_id": "2",
            "message_id": "10",
            "message_type": "incoming",
        },
    )


def _outcome(status: RunStatus, *, answer_text: str, reason: str = "") -> RunOutcome:
    return RunOutcome(
        run_id=uuid.uuid4(),
        status=status,
        route="knowledge_qa",
        answer_text=answer_text,
        send_blocked_reason=reason,
    )


def _agent_texts() -> list[str]:
    return [text for role, text, _ref, _src in RECORDED if role == "agent"]


async def test_completed_run_publishes_the_answer() -> None:
    """The happy path, asserted so the new branch cannot quietly replace it."""
    await _persist_memory(
        _SESSION,
        event=_event(),
        question="What are your support hours?",
        outcome=_outcome(RunStatus.COMPLETED, answer_text="09:00 to 18:00."),
    )
    assert _agent_texts() == ["09:00 to 18:00."]


async def test_answer_with_no_outbound_destination_is_still_published() -> None:
    """The regression: a platform-native conversation has no Chatwoot target.

    The answer exists and the customer is looking at our own surface, so
    refusing to record it loses the answer for no reason at all.
    """
    await _persist_memory(
        _SESSION,
        event=_event(),
        question="What are your support hours?",
        outcome=_outcome(
            RunStatus.FAILED, answer_text="09:00 to 18:00.", reason=OUTBOUND_TARGET_MISSING
        ),
    )
    assert _agent_texts() == ["09:00 to 18:00."]
    # Tagged, so the timeline can tell a delivered answer from one that only
    # ever reached our own surface.
    refs = [ref for role, _text, ref, _src in RECORDED if role == "agent"]
    assert refs == ["outbound:" + OUTBOUND_TARGET_MISSING]


@pytest.mark.parametrize("reason", ["OUTBOUND_FAILED", "OUTBOUND_AMBIGUOUS", ""])
async def test_a_send_that_may_not_have_landed_is_never_published(reason: str) -> None:
    """The boundary that makes the previous test safe.

    OUTBOUND_FAILED means the send errored; OUTBOUND_AMBIGUOUS means we do
    not know whether it landed. Publishing either would tell a customer
    something they may never have received.
    """
    await _persist_memory(
        _SESSION,
        event=_event(),
        question="What are your support hours?",
        outcome=_outcome(RunStatus.FAILED, answer_text="09:00 to 18:00.", reason=reason),
    )
    assert _agent_texts() == []


async def test_no_answer_means_no_agent_turn() -> None:
    """An empty answer is not an answer, however the run ended."""
    await _persist_memory(
        _SESSION,
        event=_event(),
        question="What are your support hours?",
        outcome=_outcome(RunStatus.FAILED, answer_text="", reason=OUTBOUND_TARGET_MISSING),
    )
    assert _agent_texts() == []


async def test_the_customer_turn_is_always_recorded() -> None:
    """The question is persisted even when the agent produces nothing.

    The timeline is the customer's own record of what they asked; losing it
    because the agent failed would erase their half of the conversation.
    """
    await _persist_memory(
        _SESSION,
        event=_event(),
        question="What are your support hours?",
        outcome=_outcome(RunStatus.FAILED, answer_text="", reason="OUTBOUND_FAILED"),
    )
    customers = [text for role, text, _ref, _src in RECORDED if role == "customer"]
    assert customers == ["What are your support hours?"]
