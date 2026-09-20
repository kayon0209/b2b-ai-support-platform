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


class _RecordingSender:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def send_message(
        self, *, account_id, conversation_id, content, command_id, private: bool = False
    ):
        self.calls.append({"content": content, "private": private})

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
    sender = _RecordingSender()
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
            orch = AgentOrchestrator(session, OrchestratorDeps(sender=sender))
            outcome = await orch.run(
                tenant_id=tid,
                conversation_ref_id=conv,
                question=COMPLAINT,
                principal=PrincipalScope(principal_types=("role",), principal_ids=("ai_agent",)),
                expected_lease_version=int(lease.lease_version),
                chatwoot_account_id="1",
                chatwoot_conversation_id="1",
            )
            await session.commit()
        return outcome

    outcome = _run(go())
    await_engine = engine
    _run(await_engine.dispose())
    return outcome, [c["content"] for c in sender.calls if not c["private"]]


def test_outside_hours_the_customer_is_not_promised_a_person(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outcome, messages = _handoff(monkeypatch, open_now=False)

    # Same reason, same handoff - only the promise changes.
    assert outcome.abstain_reason == "COMPLAINT_REQUIRES_HUMAN"
    assert outcome.handoff is True
    assert messages, "an abstention that says nothing is a red line"
    assert "offline" in messages[0].lower()
    assert "human colleague" not in messages[0].lower()


def test_during_hours_the_ordinary_notice_is_kept(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mutation guard: the offline branch must not swallow every handoff."""
    outcome, messages = _handoff(monkeypatch, open_now=True)

    assert outcome.abstain_reason == "COMPLAINT_REQUIRES_HUMAN"
    assert messages
    assert "human colleague" in messages[0].lower()
    assert "offline" not in messages[0].lower()
