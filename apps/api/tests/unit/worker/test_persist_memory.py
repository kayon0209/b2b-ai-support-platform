"""Unit tests: what the worker writes back into a conversation.

The bug this file was born from is fixed one layer up now. A platform-native
conversation used to fail in `_dispatch` (`OUTBOUND_TARGET_MISSING`, because
the event carries a `conversation_id` but no `chatwoot_account_id`), the run
was marked FAILED, and the answer was never persisted - because the agent
turn is only written for a COMPLETED run. `_dispatch` now reads a missing
account id as "no external channel" and lets the run complete, so the answer
arrives here through the ordinary path.

What these tests pin is therefore the policy of that ordinary path, and the
boundary around it: an answer is published when the run completed, an
abstention is published with the reason the customer can act on, and nothing
is published for a run whose send failed or whose outcome is unknown -
showing a customer an answer they never received is worse than showing none.
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
from worker.inbox_consumer import ClaimedEvent, _persist_memory

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


async def test_a_half_configured_target_is_not_published() -> None:
    """A half-configured Chatwoot target must not be published either.

    The account is known but the conversation is not, so we did mean to
    reach Chatwoot and could not. The customer is not looking at our own
    surface, and recording the answer here would claim a delivery that never
    happened.
    """
    await _persist_memory(
        _SESSION,
        event=_event(),
        question="What are your support hours?",
        outcome=_outcome(
            RunStatus.FAILED, answer_text="09:00 to 18:00.", reason="OUTBOUND_TARGET_MISSING"
        ),
    )
    assert _agent_texts() == []


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
        outcome=_outcome(RunStatus.FAILED, answer_text="", reason="OUTBOUND_FAILED"),
    )
    assert _agent_texts() == []


async def test_the_turn_source_names_where_the_question_came_from() -> None:
    """A platform-typed question is not a Chatwoot message.

    Labelling it one credits a system of record that never held it, and the
    timeline then shows the same question twice - once from each "source".
    """
    await _persist_memory(
        _SESSION,
        event=_event(),
        question="What are your support hours?",
        outcome=_outcome(RunStatus.FAILED, answer_text="", reason="OUTBOUND_FAILED"),
    )
    assert [(t[0], t[3]) for t in RECORDED] == [("customer", "platform")]

    RECORDED.clear()
    from_platform = _event()
    from_platform.minimized_payload["chatwoot_account_id"] = "3"
    await _persist_memory(
        _SESSION,
        event=from_platform,
        question="What are your support hours?",
        outcome=_outcome(RunStatus.FAILED, answer_text="", reason="OUTBOUND_FAILED"),
    )
    assert [(t[0], t[3]) for t in RECORDED] == [("customer", "chatwoot")]


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
