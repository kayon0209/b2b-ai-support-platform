"""Integration: feature 7.1 trigger 7 of 7 - emotion hands off to a person.

Two things have to be true for this feature to count as done, and only one of
them is about the detector:

1. An angry message never reaches retrieval - pinned by asserting no tool
   proposal was ever created, not by asserting a function returned a level.
2. The customer is told something specific. This is the mutation guard that
   matters most, and it exists because of a bug found the same week: an
   earlier gate in this codebase fired correctly and then crashed *before*
   dispatching its notice, so the mechanism worked and the customer still
   heard the generic "I couldn't verify an answer" fallback. A handoff that
   does not tell the customer is not a handoff, so the reply text itself is
   asserted, not just the reason code.

Runs against real Postgres: the claim is about rows that were never written.
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
APP_URL = os.environ.get(
    "APP_TEST_DATABASE_URL",
    "postgresql+psycopg://platform_app:platform_app@localhost:5435/platform",
)

TENANT = "01900000-0000-7000-8000-0000000000d7"
SLUG = "agent-emotion-handoff"

EMOTION_ESCALATION = "EMOTION_ESCALATION"

# ANGRY: about the company, not the question.
ANGRY = "你们的服务太差了，完全是敷衍！"
# ESCALATION_RISK: language of external action.
LEGAL_THREAT = "再不解决我就投诉到12315并找律师"
# FRUSTRATED: impatient, but still about the question - must NOT hand off.
IMPATIENT = "这个单我等了很久了，尽快帮我处理一下"

EXPECTED_NOTICE = "很抱歉给您带来了不好的体验"
GENERIC_FALLBACK = "I couldn't verify an answer"


def _run(coro):
    return asyncio.run(coro, loop_factory=asyncio.SelectorEventLoop)


def _factory(engine):
    return async_sessionmaker(engine, expire_on_commit=False)


async def _with_ctx(session, tenant_id: str) -> None:
    await session.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant_id})


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


def _seed_tenant() -> None:
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO tenants (id, slug, name, status) VALUES "
                "(:id, :slug, 'Agent Emotion Handoff', 'active') "
                "ON CONFLICT (slug) DO NOTHING"
            ),
            {"id": TENANT, "slug": SLUG},
        )
    admin.dispose()


def _clear() -> None:
    statements = (
        "DELETE FROM tool_executions WHERE tenant_id = :t",
        "DELETE FROM action_confirmations WHERE tenant_id = :t",
        "DELETE FROM tool_proposals WHERE tenant_id = :t",
        "DELETE FROM tool_definitions WHERE tenant_id = :t",
        "DELETE FROM connectors WHERE tenant_id = :t",
        "DELETE FROM feature_flags WHERE tenant_id = :t",
        "DELETE FROM citations WHERE tenant_id = :t",
        "DELETE FROM agent_runs WHERE tenant_id = :t",
        "DELETE FROM audit_events WHERE tenant_id = :t",
        "DELETE FROM conversation_control_leases WHERE tenant_id = :t",
        "DELETE FROM case_conversations WHERE tenant_id = :t",
        "DELETE FROM cases WHERE tenant_id = :t",
    )
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        for statement in statements:
            conn.execute(text(statement), {"t": TENANT})
        conn.execute(text("DELETE FROM tenants WHERE slug = :slug"), {"slug": SLUG})
    admin.dispose()


async def _execute(question: str) -> tuple[list[dict], str | None, int]:
    """One run. Returns (messages sent, the run's abstain reason, proposals)."""
    from platform_core.agent_runtime.orchestrator import AgentOrchestrator, OrchestratorDeps
    from platform_core.db import create_engine
    from platform_core.identity import lease_service
    from platform_core.retrieval.hybrid import PrincipalScope

    tid = uuid.UUID(TENANT)
    conv = uuid.uuid4()
    principal = PrincipalScope(principal_types=("role",), principal_ids=("ai_agent",))
    sender = _RecordingTransport()
    engine = create_engine(APP_URL)
    factory = _factory(engine)

    async with factory() as session:
        await _with_ctx(session, TENANT)
        lease = await lease_service.acquire_or_get(session, tenant_id=tid, conversation_ref_id=conv)
        await session.commit()
    expected_version = int(lease.lease_version)

    async with factory() as session:
        await _with_ctx(session, TENANT)
        orch = AgentOrchestrator(
            session, OrchestratorDeps(channel_sender=ChannelSender({"email": sender}))
        )
        await orch.run(
            tenant_id=tid,
            conversation_ref_id=conv,
            question=question,
            principal=principal,
            expected_lease_version=expected_version,
            channel_system="email",
            channel_address="buyer@example.test",
            channel_conversation_key="1",
        )
        await session.commit()

    async with factory() as session:
        await _with_ctx(session, TENANT)
        reason = (
            await session.execute(
                text(
                    "SELECT abstain_reason FROM agent_runs WHERE tenant_id = :t "
                    "ORDER BY started_at DESC LIMIT 1"
                ),
                {"t": TENANT},
            )
        ).scalar_one_or_none()
        proposals = int(
            (
                await session.execute(
                    text("SELECT count(*) FROM tool_proposals WHERE tenant_id = :t"),
                    {"t": TENANT},
                )
            ).scalar_one()
        )
    return sender.calls, reason, proposals


@pytest.fixture(autouse=True)
def tenant() -> None:
    _clear()
    _seed_tenant()
    yield
    _clear()


def test_anger_hands_off_with_the_emotion_reason() -> None:
    calls, reason, _ = _run(_execute(ANGRY))
    assert reason == EMOTION_ESCALATION


def test_legal_threat_hands_off_with_the_emotion_reason() -> None:
    calls, reason, _ = _run(_execute(LEGAL_THREAT))
    assert reason == EMOTION_ESCALATION


def test_the_customer_is_told_something_specific_not_the_generic_fallback() -> None:
    """The mutation guard: a handoff that says nothing is not a handoff."""
    calls, _reason, _ = _run(_execute(ANGRY))
    assert calls, "no notice was dispatched - the gate fired and then went silent"
    assert any(EXPECTED_NOTICE in call["content"] for call in calls)
    assert not any(GENERIC_FALLBACK in call["content"] for call in calls)


def test_an_escalation_never_reaches_a_tool_call() -> None:
    """Pre-retrieval: no embedding, no proposal, nothing to undo."""
    _calls, _reason, proposals = _run(_execute(LEGAL_THREAT))
    assert proposals == 0


def test_impatience_alone_does_not_use_the_emotion_trigger() -> None:
    """FRUSTRATED is answerable - escalating it is how a queue fills up."""
    _calls, reason, _ = _run(_execute(IMPATIENT))
    assert reason != EMOTION_ESCALATION
