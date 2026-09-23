"""Integration: a handoff tells the truth about whether anyone is there.

Feature list 7.5, "不许假装有人". The clock is patched rather than waited on,
so the two branches are testable at any time of day.

The assertion that matters is the negative one: the ordinary notice promises a
colleague, and at 03:00 that promise is false. The reason code is unchanged -
the run stopped for the same reason and the receiving agent needs that - only
what the customer is told differs.
"""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from platform_core.channels.outbound import ChannelSender, SendResult

pytestmark = pytest.mark.integration

ADMIN_URL = os.environ.get(
    "APP_ADMIN_DATABASE_URL",
    "postgresql+psycopg://platform:platform@localhost:5435/platform",
)
APP_URL = "postgresql+psycopg://platform_app:platform_app@localhost:5435/platform"

TENANT = "01900000-0000-7000-8000-0000000000ce"
SLUG = "off-hours"

COMPLAINT = "板子短路了，我要索赔"


def _run(coro):
    return asyncio.run(coro, loop_factory=asyncio.SelectorEventLoop)


def _seed() -> None:
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO tenants (id, slug, name, status) VALUES "
                "(:id, :slug, :slug, 'active') ON CONFLICT (slug) DO NOTHING"
            ),
            {"id": TENANT, "slug": SLUG},
        )
    admin.dispose()


def _clear() -> None:
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        conn.execute(text("DELETE FROM agent_runs WHERE tenant_id = :t"), {"t": TENANT})
        conn.execute(
            text("DELETE FROM conversation_control_leases WHERE tenant_id = :t"), {"t": TENANT}
        )
        conn.execute(text("DELETE FROM tenants WHERE slug = :slug"), {"slug": SLUG})
    admin.dispose()


@pytest.fixture(autouse=True)
def clean() -> None:
    _clear()
    _seed()
    yield
    _clear()


class _RecordingTransport:
    """Channel transport double: records every outbound answer.

    It replaced a Chatwoot-shaped `sender` double. The channel path is the only
    path that still leaves the platform, so it is the only delivery an outside
    observer can see; the platform's own surface delivers by persisting the
    agent turn, which these tests read back from the database.
    """

    system = "email"

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def send(self, *, address, conversation_key, content, command_id):
        self.calls.append(
            {
                "address": address,
                "conversation_key": conversation_key,
                "content": content,
                "command_id": command_id,
            }
        )
        return SendResult()

        class _Result:
            ambiguous = False

        return _Result()


def _handoff(monkeypatch: pytest.MonkeyPatch, *, open_now: bool):
    from platform_core.agent_runtime import orchestrator
    from platform_core.agent_runtime.orchestrator import AgentOrchestrator, OrchestratorDeps
    from platform_core.db import create_engine
    from platform_core.identity import lease_service
    from platform_core.retrieval.hybrid import PrincipalScope

    monkeypatch.setattr(orchestrator, "is_open", lambda: open_now)

    tid = uuid.UUID(TENANT)
    conv = uuid.uuid4()
    sender = _RecordingTransport()
    engine = create_engine(APP_URL)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def go():
        async with factory() as session:
            await session.execute(
                text("SELECT set_config('app.tenant_id', :t, true)"), {"t": TENANT}
            )
            lease = await lease_service.acquire_or_get(
                session, tenant_id=tid, conversation_ref_id=conv
            )
            await session.commit()
        async with factory() as session:
            await session.execute(
                text("SELECT set_config('app.tenant_id', :t, true)"), {"t": TENANT}
            )
            orch = AgentOrchestrator(
                session, OrchestratorDeps(channel_sender=ChannelSender({"email": sender}))
            )
            outcome = await orch.run(
                tenant_id=tid,
                conversation_ref_id=conv,
                question=COMPLAINT,
                principal=PrincipalScope(principal_types=("role",), principal_ids=("ai_agent",)),
                expected_lease_version=int(lease.lease_version),
                channel_system="email",
                channel_address="buyer@example.test",
                channel_conversation_key="1",
            )
            await session.commit()
        return outcome

    outcome = _run(go())
    await_engine = engine
    _run(await_engine.dispose())
    return outcome, [c["content"] for c in sender.calls]


def test_outside_hours_the_customer_is_not_promised_a_person(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outcome, messages = _handoff(monkeypatch, open_now=False)

    # Same reason, same handoff - only the promise changes.
    assert outcome.abstain_reason == "COMPLAINT_REQUIRES_HUMAN"
    assert outcome.handoff is True
    assert messages, "an abstention that says nothing is a red line"
    # The complaint above is Chinese, so the notice must be too (`language.
    # answers_in_chinese`). These assertions were English-only and went stale
    # the moment the copy was localised - the check is the same distinction,
    # read in the language the customer actually wrote in: the offline notice
    # says nobody is there and must not promise a colleague.
    assert "不在线" in messages[0]
    assert "人工同事" not in messages[0]


def test_during_hours_the_ordinary_notice_is_kept(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mutation guard: the offline branch must not swallow every handoff."""
    outcome, messages = _handoff(monkeypatch, open_now=True)

    assert outcome.abstain_reason == "COMPLAINT_REQUIRES_HUMAN"
    assert messages
    assert "人工同事" in messages[0]
    assert "不在线" not in messages[0]
