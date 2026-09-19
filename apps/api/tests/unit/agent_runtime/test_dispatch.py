"""Unit tests: deciding whether an answer has somewhere to go.

`_dispatch` is where a generated answer becomes a customer-visible message,
and it is the one place that decides whether failing to deliver is a failure.

It used to require both a Chatwoot account id and a conversation id, so a
conversation the platform owns - the customer typed into our own chat
surface, which has an external conversation id but no Chatwoot account -
was treated as a botched delivery. The run was marked FAILED, and because the
agent turn is only persisted for a COMPLETED run, the answer was generated
and then thrown away. Nothing errored; the customer simply saw no reply.

The distinction that matters is not "are both coordinates present" but "is
this conversation Chatwoot's at all". The account id answers that: real
Chatwoot events carry one, platform-native ones do not. The conversation id
does not, because it is the external correlation id the conversation ref is
derived from, and it is present on both kinds of event.
"""

import uuid
from typing import Any, cast

from sqlalchemy.ext.asyncio import AsyncSession

from observability import new_trace_context
from platform_core.agent_runtime.models import AgentRun
from platform_core.agent_runtime.orchestrator import AgentOrchestrator, OrchestratorDeps

TENANT_ID = uuid.UUID("17ab2c52-7d95-5fba-a06c-b5641393831e")


class _Sender:
    """Records what it was asked to send, and can be told to fail."""

    def __init__(self, *, raise_error: bool = False, ambiguous: bool = False) -> None:
        self.raise_error = raise_error
        self.ambiguous = ambiguous
        self.sent: list[tuple[str, str, str]] = []

    async def send_message(
        self, *, account_id: str, conversation_id: str, content: str, command_id: str
    ) -> Any:
        if self.raise_error:
            raise RuntimeError("chatwoot is down")
        self.sent.append((account_id, conversation_id, content))
        return type("Result", (), {"ambiguous": self.ambiguous})()


def _orchestrator(sender: object) -> AgentOrchestrator:
    return AgentOrchestrator(cast(AsyncSession, None), OrchestratorDeps(sender=sender))


def _run() -> AgentRun:
    return AgentRun(
        id=uuid.uuid4(),
        tenant_id=TENANT_ID,
        conversation_ref_id=uuid.uuid4(),
        route="knowledge_qa",
    )


async def _dispatch(sender: object, *, account_id: str | None, conversation_id: str | None) -> str:
    orch = _orchestrator(sender)
    return await orch._dispatch(
        run=_run(),
        tenant_id=TENANT_ID,
        draft_text="09:00 to 18:00.",
        ctx=new_trace_context(service_name="test"),
        chatwoot_account_id=account_id,
        chatwoot_conversation_id=conversation_id,
        conversation_ref_id=uuid.uuid4(),
    )


async def test_a_conversation_with_no_chatwoot_account_is_not_a_failed_send() -> None:
    """The regression: a platform-native conversation must still complete.

    There is no Chatwoot account to send to, so there is nothing to fail at -
    the platform's own surface is the delivery channel. Marking this FAILED
    is what made every such answer disappear.
    """
    sender = _Sender()
    reason = await _dispatch(sender, account_id="", conversation_id="some-external-id")
    assert reason == ""
    # Nothing was sent, and nothing was claimed to have been.
    assert sender.sent == []


async def test_no_transport_wired_is_un_sent_rather_than_failed() -> None:
    """Local/unit wiring: an answer that was never meant to leave is fine."""
    assert await _dispatch(None, account_id="3", conversation_id="77") == ""


async def test_a_chatwoot_conversation_is_sent() -> None:
    sender = _Sender()
    reason = await _dispatch(sender, account_id="3", conversation_id="77")
    assert reason == ""
    assert sender.sent == [("3", "77", "09:00 to 18:00.")]


async def test_an_account_with_no_conversation_is_a_misconfiguration() -> None:
    """We did mean to reach Chatwoot and cannot - that has to be a failure.

    Unlike the platform-native case there is a customer waiting somewhere we
    cannot reach, so reporting success would be a lie.
    """
    sender = _Sender()
    reason = await _dispatch(sender, account_id="3", conversation_id="")
    assert reason == "OUTBOUND_TARGET_MISSING"
    assert sender.sent == []


async def test_a_send_that_raised_is_reported_as_failed() -> None:
    sender = _Sender(raise_error=True)
    assert await _dispatch(sender, account_id="3", conversation_id="77") == "OUTBOUND_FAILED"


async def test_an_unknown_send_outcome_is_never_claimed_as_success() -> None:
    """Ambiguous means we do not know whether the customer received it."""
    sender = _Sender(ambiguous=True)
    reason = await _dispatch(sender, account_id="3", conversation_id="77")
    assert reason == "OUTBOUND_AMBIGUOUS"
