"""Does the pricing band actually reach a real handoff?

`quote_label` is unit-tested, and the call into it from `_finish_abstain` is
obvious to read - which is exactly why it needs this test. This repository
keeps finding the same defect: a function that works, is tested, and is never
called by the path it was written for. A band nobody sees is no better than no
engine.

So this drives a whole run with a pricing question and reads the note an agent
would read.
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

TENANT = "01900000-0000-7000-8000-0000000000a8"
SLUG = "pricing-handoff"

QUESTION = "4层板 100x100mm 板厚1.6mm 沉金 500片多少钱？"


def _run(coro):
    return asyncio.run(coro, loop_factory=asyncio.SelectorEventLoop)


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


def _clear() -> None:
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        for statement in (
            "DELETE FROM conversation_turns WHERE tenant_id = :t",
            "DELETE FROM citations WHERE tenant_id = :t",
            "DELETE FROM agent_runs WHERE tenant_id = :t",
            "DELETE FROM conversation_control_leases WHERE tenant_id = :t",
            "DELETE FROM audit_events WHERE tenant_id = :t",
        ):
            conn.execute(text(statement), {"t": TENANT})
        conn.execute(text("DELETE FROM tenants WHERE slug = :slug"), {"slug": SLUG})
    admin.dispose()


@pytest.fixture(autouse=True)
def clean() -> None:
    _clear()
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
    yield
    _clear()


async def _execute() -> tuple[object, list[dict]]:
    from platform_core.agent_runtime.orchestrator import AgentOrchestrator, OrchestratorDeps
    from platform_core.db import create_engine
    from platform_core.identity import lease_service
    from platform_core.retrieval.hybrid import PrincipalScope

    tid = uuid.UUID(TENANT)
    conv = uuid.uuid4()
    sender = _RecordingTransport()
    engine = create_engine(APP_URL)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async with factory() as session:
        await session.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": TENANT})
        lease = await lease_service.acquire_or_get(session, tenant_id=tid, conversation_ref_id=conv)
        await session.commit()

    async with factory() as session:
        await session.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": TENANT})
        orch = AgentOrchestrator(
            session, OrchestratorDeps(channel_sender=ChannelSender({"email": sender}))
        )
        outcome = await orch.run(
            tenant_id=tid,
            conversation_ref_id=conv,
            question=QUESTION,
            principal=PrincipalScope(principal_types=("role",), principal_ids=("ai_agent",)),
            expected_lease_version=int(lease.lease_version),
            channel_system="email",
            channel_address="buyer@example.test",
            channel_conversation_key="1",
        )
        await session.commit()
    await engine.dispose()
    return outcome, sender.calls


def test_a_pricing_question_hands_off_with_a_band(monkeypatch: pytest.MonkeyPatch) -> None:
    """4B end to end through the path that would actually use it.

    The engine and the parser are unit-tested; this asserts the run reaches
    them, and that what the agent receives is a band it can act on - not a
    figure it might mistake for a quote.
    """
    from platform_core.config import get_settings

    monkeypatch.setenv("APP_HANDOFF_EVIDENCE_ENABLED", "true")
    get_settings.cache_clear()
    try:
        outcome, calls = _run(_execute())
    finally:
        get_settings.cache_clear()

    assert outcome.route == "human_required", outcome.route
    assert outcome.handoff is True

    # The band used to travel in a private Chatwoot note. The handoff's audit
    # metadata is where it lives now (ADR 0012), and every field below still
    # has to be there: a figure that arrives without its label is worse than no
    # figure at all.
    joined = _handoff_metadata(outcome.run_id).get("context", "")
    assert "quote_band=" in joined, joined
    # The figure must arrive labelled: a public reference number that reads as
    # this company's price is worse than no number at all.
    assert "NON-CONTRACTUAL" in joined
    assert "version=public-reference-2026.08" in joined
    assert "qty=500" in joined
    assert "total_for_order" in joined


def _handoff_metadata(run_id: object) -> dict:
    """The handoff's audit metadata: what the receiving side actually reads.

    `after=` on an audit event is hashed and unreadable by design; `metadata`
    is the documented narrow exception for an event's own parameters, which is
    why the handoff's routing facts live there.
    """
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        row = conn.execute(
            text(
                "SELECT metadata FROM audit_events "
                "WHERE resource_id = :r AND action = 'agent_run.abstained'"
            ),
            {"r": str(run_id)},
        ).first()
    admin.dispose()
    return dict(row[0]) if row and row[0] else {}
