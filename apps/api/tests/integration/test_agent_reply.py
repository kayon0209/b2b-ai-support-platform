"""Integration: a human agent's reply.

The load-bearing assertions, in order of what would break the product:

1. **The reply reaches the customer, verbatim.** Redaction would corrupt an
   order number, and a corrupted reply is worse than none.
2. **Replying takes the lease.** Without it an AI run mid-generation sends its
   own draft over the human's answer - the duplicate the lease exists to stop.
3. **A channel reply is queued with the thread key.** Without the key every
   reply starts a new email thread, so the conversation never accumulates.
4. **A platform-surface reply is delivered by persistence**, and is *not*
   reported as undelivered for having no transport.
"""

from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import create_engine as _admin_engine
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from platform_core.db import create_engine

pytestmark = pytest.mark.integration

ADMIN_URL = os.environ.get(
    "APP_ADMIN_DATABASE_URL",
    "postgresql+psycopg://platform:platform@localhost:5435/platform",
)
APP_URL = "postgresql+psycopg://platform_app:platform_app@localhost:5435/platform"

_NS = uuid.uuid5(uuid.NAMESPACE_URL, "b2b-ai-support/tests/agent-reply")
TENANT = str(uuid.uuid5(_NS, "tenant"))
TENANT_OTHER = str(uuid.uuid5(_NS, "tenant-other"))

_TEARDOWN = (
    "conversation_turns",
    "conversation_contacts",
    "conversation_control_leases",
    "outbox_events",
)


def _run(coro):
    import asyncio

    return asyncio.run(coro, loop_factory=asyncio.SelectorEventLoop)


def _wipe(where: str, params: dict) -> None:
    admin = _admin_engine(ADMIN_URL)
    with admin.begin() as conn:
        for table in _TEARDOWN:
            conn.execute(text(f"DELETE FROM {table} WHERE {where}"), params)  # noqa: S608
    admin.dispose()


@pytest.fixture(autouse=True)
def _clean():
    _wipe("tenant_id IN (:a, :b)", {"a": TENANT, "b": TENANT_OTHER})
    yield
    _wipe("tenant_id IN (:a, :b)", {"a": TENANT, "b": TENANT_OTHER})


@pytest.fixture(scope="module", autouse=True)
def _tenants():
    admin = _admin_engine(ADMIN_URL)
    with admin.begin() as conn:
        for tid, slug in ((TENANT, "rep-a"), (TENANT_OTHER, "rep-b")):
            conn.execute(
                text(
                    "INSERT INTO tenants (id, slug, name, status) VALUES "
                    "(:id, :slug, :slug, 'active') ON CONFLICT (slug) DO NOTHING"
                ),
                {"id": tid, "slug": slug},
            )
    yield
    with admin.begin() as conn:
        sub = "(SELECT id FROM tenants WHERE slug LIKE 'rep-%')"
        for table in _TEARDOWN:
            conn.execute(text(f"DELETE FROM {table} WHERE tenant_id IN {sub}"))  # noqa: S608
        conn.execute(text("DELETE FROM tenants WHERE slug LIKE 'rep-%'"))
    admin.dispose()


async def _with_session(tenant: str, fn):
    engine = create_engine(APP_URL)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            await session.execute(
                text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant}
            )
            try:
                result = await fn(session)
            except Exception:
                await session.rollback()
                raise
            await session.commit()
            return result
    finally:
        await engine.dispose()


def _reply(tenant: str, ref: uuid.UUID, text_body: str, agent: str = "agent-1"):
    from platform_core.agent_runtime.agent_reply import send_agent_reply

    return _run(
        _with_session(
            tenant,
            lambda s: send_agent_reply(
                s,
                tenant_id=uuid.UUID(tenant),
                conversation_ref_id=ref,
                text=text_body,
                agent_ref=agent,
            ),
        )
    )


def _link(tenant: str, ref: uuid.UUID, *, contact: str, channel: str, key: str) -> None:
    from platform_core.support_bridge.continuity import link_conversation

    _run(
        _with_session(
            tenant,
            lambda s: link_conversation(
                s,
                tenant_id=uuid.UUID(tenant),
                conversation_ref_id=ref,
                external_contact_id=contact,
                channel=channel,
                external_conversation_key=key,
            ),
        )
    )


# --- the reply itself ------------------------------------------------------


def test_a_platform_reply_is_delivered_by_persistence() -> None:
    """No channel row: `/support` reads the turn back, so this is success.

    Reporting it as undelivered would refuse to answer the customers the
    platform can most easily reach.
    """
    ref = uuid.uuid4()
    result = _reply(TENANT, ref, "您的订单已发出。")

    assert result.delivery == "platform"
    assert result.event_id is None

    admin = _admin_engine(ADMIN_URL)
    with admin.begin() as conn:
        row = conn.execute(
            text(
                "SELECT role, text_redacted, source FROM conversation_turns "
                "WHERE tenant_id = :t AND conversation_ref_id = :r"
            ),
            {"t": TENANT, "r": str(ref)},
        ).one()
    admin.dispose()
    assert row[0] == "agent"
    assert row[1] == "您的订单已发出。"
    assert row[2] == "agent"


def test_a_reply_is_stored_verbatim() -> None:
    """Redaction masks any 10+ digit run, so it would eat the order number the
    message exists to convey."""
    ref = uuid.uuid4()
    body = "订单 SO-9001 的物流单号 20260930123456 已更新。"
    _reply(TENANT, ref, body)

    admin = _admin_engine(ADMIN_URL)
    with admin.begin() as conn:
        stored = conn.execute(
            text(
                "SELECT text_redacted FROM conversation_turns "
                "WHERE tenant_id = :t AND conversation_ref_id = :r"
            ),
            {"t": TENANT, "r": str(ref)},
        ).scalar()
    admin.dispose()
    assert stored == body, "the stored copy must equal what the customer received"


def test_replying_takes_the_lease_from_the_ai() -> None:
    """`AGENTS.md` rule 8 the other way round: the AI re-checks the lease before
    sending, so the human has to *hold* it for their reply to be the one that
    survives."""
    ref = uuid.uuid4()
    result = _reply(TENANT, ref, "我来处理。", agent="agent-7")

    admin = _admin_engine(ADMIN_URL)
    with admin.begin() as conn:
        row = conn.execute(
            text(
                "SELECT owner_type, owner_ref, mode FROM conversation_control_leases "
                "WHERE tenant_id = :t AND conversation_ref_id = :r"
            ),
            {"t": TENANT, "r": str(ref)},
        ).one()
    admin.dispose()
    assert row[0] == "human"
    assert row[1] == "agent-7"
    assert row[2] == "HUMAN_ACTIVE"
    assert result.lease_version >= 1


def test_a_channel_reply_is_queued_with_the_thread_key() -> None:
    """The key is what makes the reply join the existing thread. Without it
    every answer starts a new email thread."""
    ref = uuid.uuid4()
    _link(TENANT, ref, contact="buyer@example.test", channel="email", key="<root@acme.test>")

    result = _reply(TENANT, ref, "已为您加急。")

    assert result.delivery == "channel"
    assert result.channel == "email"
    assert result.event_id is not None

    admin = _admin_engine(ADMIN_URL)
    with admin.begin() as conn:
        payload = conn.execute(
            text(
                "SELECT payload FROM outbox_events "
                "WHERE tenant_id = :t AND event_type = 'conversation.agent_reply'"
            ),
            {"t": TENANT},
        ).scalar()
    admin.dispose()
    assert payload["conversation_key"] == "<root@acme.test>"
    assert payload["address"] == "buyer@example.test"
    assert payload["turn_id"] == str(result.turn_id)
    # The payload carries the turn id, not the text: one record, not two that
    # can disagree.
    assert "text" not in payload


@pytest.mark.parametrize("body", ["", "   ", "x" * 4001])
def test_a_reply_with_bad_text_is_refused(body: str) -> None:
    from platform_core.agent_runtime.agent_reply import AgentReplyError

    with pytest.raises(AgentReplyError):
        _reply(TENANT, uuid.uuid4(), body)


def test_a_reply_must_name_its_author() -> None:
    from platform_core.agent_runtime.agent_reply import AgentReplyError

    with pytest.raises(AgentReplyError, match="name the agent"):
        _reply(TENANT, uuid.uuid4(), "hello", agent="  ")


# --- the delivery handler --------------------------------------------------


def _make_event(tenant: str, payload: dict) -> object:
    from platform_core.outbox import OutboxEvent

    return OutboxEvent(
        tenant_id=uuid.UUID(tenant),
        event_id=uuid.uuid4(),
        event_type="conversation.agent_reply",
        event_version=1,
        aggregate_type="conversation",
        aggregate_id=str(uuid.uuid4()),
        payload=payload,
        status="claimed",
        attempts=1,
        created_at=0,
    )


def test_the_handler_refuses_an_event_with_no_target() -> None:
    """A malformed event must park, not be retired silently - a message that
    never went out is not a delivered one."""
    from worker.outbox_relay import handle_agent_reply

    async def go(session):
        with pytest.raises(ValueError, match="delivery target"):
            await handle_agent_reply(session, _make_event(TENANT, {"channel": "email"}))

    _run(_with_session(TENANT, go))


def test_the_handler_refuses_when_no_transport_is_configured(monkeypatch) -> None:
    """A receive-only deployment. This is exactly the case that must not be
    reported as delivered: `Workbench` shows the reply and the customer has
    heard nothing."""
    from platform_core.channels.outbound import ChannelNotConfigured
    from worker.outbox_relay import handle_agent_reply

    ref = uuid.uuid4()
    _link(TENANT, ref, contact="buyer@example.test", channel="email", key="<root@acme.test>")
    result = _reply(TENANT, ref, "已为您加急。")

    monkeypatch.setattr(
        "platform_core.channels.outbound.build_channel_sender",
        lambda _settings: type("_Empty", (), {"configured": lambda self, _s: False})(),
    )

    async def go(session):
        with pytest.raises(ChannelNotConfigured):
            await handle_agent_reply(
                session,
                _make_event(
                    TENANT,
                    {
                        "channel": "email",
                        "address": "buyer@example.test",
                        "turn_id": str(result.turn_id),
                        "conversation_key": "<root@acme.test>",
                    },
                ),
            )

    _run(_with_session(TENANT, go))


def test_the_handler_sends_verbatim_with_a_turn_scoped_key(monkeypatch) -> None:
    """The retry key is derived from the turn id, so an ambiguous failure
    retries with the same key rather than sending twice."""
    from worker.outbox_relay import handle_agent_reply

    ref = uuid.uuid4()
    _link(TENANT, ref, contact="buyer@example.test", channel="email", key="<root@acme.test>")
    body = "物流单号 20260930123456"
    result = _reply(TENANT, ref, body)

    sent: list[dict] = []

    class _Transport:
        system = "email"

        async def send(self, *, address, conversation_key, content, command_id):
            sent.append(
                {
                    "address": address,
                    "conversation_key": conversation_key,
                    "content": content,
                    "command_id": command_id,
                }
            )
            from platform_core.channels.outbound import SendResult

            return SendResult()

    class _Sender:
        def configured(self, _system: str) -> bool:
            return True

        async def send_message(self, **kwargs):
            # The real `ChannelSender` pops `system` to pick a transport; this
            # stub has one, so it drops the key rather than forwarding it.
            kwargs.pop("system", None)
            return await _Transport().send(**kwargs)

    monkeypatch.setattr(
        "platform_core.channels.outbound.build_channel_sender", lambda _settings: _Sender()
    )

    async def go(session):
        await handle_agent_reply(
            session,
            _make_event(
                TENANT,
                {
                    "channel": "email",
                    "address": "buyer@example.test",
                    "turn_id": str(result.turn_id),
                    "conversation_key": "<root@acme.test>",
                },
            ),
        )

    _run(_with_session(TENANT, go))
    assert len(sent) == 1
    assert sent[0]["content"] == body, "the customer must receive exactly what was written"
    assert sent[0]["conversation_key"] == "<root@acme.test>"
    assert sent[0]["command_id"] == f"agent-reply:{result.turn_id}"


# --- isolation -------------------------------------------------------------


def test_another_tenant_cannot_reply_into_our_conversation() -> None:
    """The lease and the turn are both RLS-scoped, so a foreign ref produces a
    reply in the wrong tenant's conversation - or nothing at all. It must be
    nothing."""
    ref = uuid.uuid4()
    _reply(TENANT, ref, "ours")

    _reply(TENANT_OTHER, ref, "theirs")

    admin = _admin_engine(ADMIN_URL)
    with admin.begin() as conn:
        rows = conn.execute(
            text(
                "SELECT tenant_id, text_redacted FROM conversation_turns "
                "WHERE conversation_ref_id = :r ORDER BY tenant_id"
            ),
            {"r": str(ref)},
        ).all()
    admin.dispose()
    assert len(rows) == 2, "each tenant's turn must land under its own tenant"
    assert {str(r[0]) for r in rows} == {TENANT, TENANT_OTHER}


def test_linking_a_conversation_records_the_contact_and_the_key() -> None:
    """The row the reply path depends on, written by the inbound channel."""
    from platform_core.support_bridge.continuity import delivery_target

    ref = uuid.uuid4()
    _link(TENANT, ref, contact="openid_abc", channel="wechat", key="openid_abc")

    target = _run(
        _with_session(
            TENANT,
            lambda s: delivery_target(s, tenant_id=uuid.UUID(TENANT), conversation_ref_id=ref),
        )
    )
    assert target == ("wechat", "openid_abc", "openid_abc")
