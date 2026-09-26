"""Unit tests: outbound channel dispatch (ADR 0014).

These cover the **decision**, not the wire. The defect this file exists to
prevent is `_dispatch` returning `""` for a channel message: `""` means
"delivered", so an email or WeChat answer that is never sent would be recorded
as delivered, the run would complete, and nothing anywhere would say the
customer heard nothing. That is the `worker_cannot_send` failure shape.

The orchestrator is a stub rather than a real `AgentOrchestrator`: `_dispatch`
reads `self._deps` and nothing else, so a database would add a prerequisite to
checking a routing decision without adding a single thing it verifies.
"""

import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from observability import TraceContext
from platform_core.agent_runtime.orchestrator import AgentOrchestrator, OrchestratorDeps
from platform_core.channels.outbound import (
    ChannelNotConfigured,
    ChannelSender,
    SendResult,
    build_channel_sender,
)
from platform_core.config import Settings


class _FakeTransport:
    """Records what it was asked to send; can be told to fail."""

    def __init__(self, system: str, *, raises: Exception | None = None, ambiguous: bool = False):
        self.system = system
        self.calls: list[dict[str, Any]] = []
        self._raises = raises
        self._ambiguous = ambiguous

    async def send(self, *, address, conversation_key, content, command_id) -> SendResult:
        self.calls.append(
            {
                "address": address,
                "conversation_key": conversation_key,
                "content": content,
                "command_id": command_id,
            }
        )
        if self._raises is not None:
            raise self._raises
        return SendResult(ambiguous=self._ambiguous)


def _orchestrator(**deps: Any) -> AgentOrchestrator:
    """A real orchestrator with only `_deps` set.

    `__new__` rather than the constructor: `__init__` wants a database session,
    and nothing under test here touches one. Using a `SimpleNamespace` instead
    would break the moment `_dispatch` delegates to a sibling method - which is
    exactly what it does.
    """
    orchestrator = AgentOrchestrator.__new__(AgentOrchestrator)
    orchestrator._deps = OrchestratorDeps(**deps)  # type: ignore[attr-defined]
    return orchestrator


def _run() -> SimpleNamespace:
    return SimpleNamespace(id=uuid.uuid4())


async def _dispatch(orchestrator: AgentOrchestrator, **overrides: Any) -> str:
    kwargs: dict[str, Any] = {
        "run": _run(),
        "tenant_id": uuid.uuid4(),
        "draft_text": "您的订单 SO-9001 已发货",
        "ctx": TraceContext(trace_id="tr-1"),
        "conversation_ref_id": uuid.uuid4(),
    }
    kwargs.update(overrides)
    return await orchestrator._dispatch(**kwargs)


# --- the guard-order bug ---------------------------------------------------


@pytest.mark.asyncio
async def test_a_channel_message_is_dispatched() -> None:
    """A channel conversation has a transport, so the answer leaves.

    There is no longer a second transport to fall back on: the channel branch
    is the only one that sends anything, and a channel message must never be
    reported as delivered without it.
    """
    transport = _FakeTransport("email")
    stub = _orchestrator(channel_sender=ChannelSender({"email": transport}))

    result = await _dispatch(stub, channel_system="email", channel_address="buyer@example.test")

    assert result == ""
    assert len(transport.calls) == 1


@pytest.mark.asyncio
async def test_the_platform_surface_is_unchanged() -> None:
    """No channel: the platform's own surface is the channel, and that is a
    success - the behaviour `/support` depends on. The answer is persisted as
    an agent turn and read back by the page, so there is nothing to send."""
    stub = _orchestrator(channel_sender=None)
    assert await _dispatch(stub) == ""


# --- what the transport receives -------------------------------------------


@pytest.mark.asyncio
async def test_the_transport_gets_the_address_the_thread_and_a_run_scoped_key() -> None:
    transport = _FakeTransport("email")
    orchestrator = _orchestrator(channel_sender=ChannelSender({"email": transport}))
    run = _run()

    result = await orchestrator._dispatch(
        run=run,
        tenant_id=uuid.uuid4(),
        draft_text="body",
        ctx=TraceContext(trace_id="tr-1"),
        conversation_ref_id=uuid.uuid4(),
        channel_conversation_key="<root@acme.test>",
        channel_system="email",
        channel_address="buyer@example.test",
    )

    assert result == ""
    call = transport.calls[0]
    assert call["address"] == "buyer@example.test"
    # The thread root, so the customer's reply joins the same conversation
    # instead of starting a new one.
    assert call["conversation_key"] == "<root@acme.test>"
    # One run, one key: a retry of this run cannot deliver twice.
    assert call["command_id"] == f"run:{run.id}"


# --- the failure taxonomy --------------------------------------------------


@pytest.mark.asyncio
async def test_an_unconfigured_channel_is_reported_and_not_sent() -> None:
    """`OUTBOUND_NOT_CONFIGURED`, never "". A receive-only channel is a state
    the operator must be able to see."""
    transport = _FakeTransport("wechat")
    stub = _orchestrator(channel_sender=ChannelSender({"wechat": transport}))

    result = await _dispatch(stub, channel_system="email", channel_address="buyer@example.test")

    assert result == "OUTBOUND_NOT_CONFIGURED"
    assert transport.calls == []


@pytest.mark.asyncio
async def test_no_channel_sender_at_all_is_also_not_a_success() -> None:
    stub = _orchestrator(channel_sender=None)
    result = await _dispatch(stub, channel_system="email", channel_address="a@b.test")
    assert result == "OUTBOUND_NOT_CONFIGURED"


@pytest.mark.asyncio
async def test_a_channel_with_no_address_is_a_misconfiguration() -> None:
    """We know the channel and cannot address it: that fails, rather than being
    recorded as a withheld delivery."""
    transport = _FakeTransport("email")
    stub = _orchestrator(channel_sender=ChannelSender({"email": transport}))

    result = await _dispatch(stub, channel_system="email", channel_address="")

    assert result == "OUTBOUND_TARGET_MISSING"
    assert transport.calls == []


@pytest.mark.asyncio
async def test_a_transport_error_is_a_failure_not_a_silent_success() -> None:
    transport = _FakeTransport("email", raises=RuntimeError("smtp down"))
    stub = _orchestrator(channel_sender=ChannelSender({"email": transport}))

    result = await _dispatch(stub, channel_system="email", channel_address="buyer@example.test")

    assert result == "OUTBOUND_FAILED"


@pytest.mark.asyncio
async def test_an_ambiguous_send_is_never_called_a_success() -> None:
    """Unknown outcome: retryable, but never "delivered"."""
    transport = _FakeTransport("email", ambiguous=True)
    stub = _orchestrator(channel_sender=ChannelSender({"email": transport}))

    result = await _dispatch(stub, channel_system="email", channel_address="buyer@example.test")

    assert result == "OUTBOUND_AMBIGUOUS"


# --- the registry ----------------------------------------------------------


@pytest.mark.asyncio
async def test_an_unknown_system_raises_rather_than_doing_nothing() -> None:
    sender = ChannelSender({"email": _FakeTransport("email")})
    with pytest.raises(ChannelNotConfigured):
        await sender.send_message(
            system="carrier-pigeon",
            address="a@b.test",
            conversation_key="k",
            content="c",
            command_id="run:1",
        )


def test_only_channels_with_credentials_are_registered() -> None:
    """A half-configured channel must be *absent*, not present and broken, so
    `configured()` tells the truth and the orchestrator can distinguish
    "receive-only" from "delivery failed"."""
    bare = build_channel_sender(Settings())
    assert bare.systems == ()
    assert not bare.configured("email")

    # A host with no from-address cannot send: an SMTP relay rejects it, and
    # registering it would turn every answer into OUTBOUND_FAILED.
    partial = build_channel_sender(Settings(email_smtp_host="smtp.test"))
    assert not partial.configured("email")

    full = build_channel_sender(
        Settings(email_smtp_host="smtp.test", email_from_address="support@acme.test")
    )
    assert full.systems == ("email",)
    assert full.configured("email")
