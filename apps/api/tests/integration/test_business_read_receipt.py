"""End-to-end read path: an order question is answered from tool data.

This test exists because the read path had none. Receipt publishing (4A.3) was
wired into `_attempt_business_read` and tested only through the publisher
directly, which cannot notice if the read path never calls it - the exact
defect this repository keeps finding.

It drives a real run with a fake `business_api` adapter: the answer must be
grounded in the tool receipt rather than in the corpus, and the receipt must
reach the conversation as a TOOL turn so the customer can see the data and not
only a sentence about it.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import async_sessionmaker

pytestmark = pytest.mark.integration

ADMIN_URL = os.environ.get(
    "APP_ADMIN_DATABASE_URL",
    "postgresql+psycopg://platform:platform@localhost:5435/platform",
)
APP_URL = "postgresql+psycopg://platform_app:platform_app@localhost:5435/platform"

TENANT = "01900000-0000-7000-8000-0000000000d0"
SLUG = "business-read-receipt"

QUESTION = "What is the status of order SO-9001?"
RECEIPT_NODES = [
    {"label": "下单", "status": "done"},
    {"label": "生产", "status": "active"},
]


def _run(coro):
    return asyncio.run(coro, loop_factory=asyncio.SelectorEventLoop)


class _FakeReadExecutor:
    """Returns the receipt the ERP would have returned.

    Shaped like the real one - nodes plus `fetched_at` - because the customer
    sees this, and a card built from a receipt without a fetch time cannot
    say how old the promise is.
    """

    async def execute(self, tool_name: str, parameters: dict, idempotency_key: str) -> dict | None:
        record_id = str(parameters.get("order_id") or "")
        return {
            "found": True,
            "resource": "orders",
            "order_id": record_id,
            "status": "in_production",
            "nodes": RECEIPT_NODES,
            "fetched_at": "2026-09-21T02:00:00Z",
        }

    async def verify_postcondition(self, tool_name: str, parameters: dict, output: object) -> bool:
        # The gateway refuses to report a read as done until its
        # postcondition is verified, so a receipt without this is never
        # published at all.
        return bool(output)


class _FakeBusinessApiFactory:
    """Provider -> executor, the shape `ConnectorExecutorResolver` expects.

    It calls `factory.build(context)` and expects a ToolExecutor back; handing
    it an adapter instead is why the first attempt resolved to
    TOOL_UNAVAILABLE.
    """

    @staticmethod
    def build(context: object) -> _FakeReadExecutor:
        return _FakeReadExecutor()


class _ReceiptCitingGenerator:
    """Answers from the receipt, citing it.

    The generator is what the platform would call; it is stubbed here so the
    test covers the read path and the receipt publishing, not the model.
    """

    def __init__(self) -> None:
        from platform_core.agent_runtime.prompts import KNOWLEDGE_QA_PROMPT

        self.template = KNOWLEDGE_QA_PROMPT

    async def generate(self, question, evidence, **kwargs):
        from platform_core.agent_runtime.qa_path import DraftAnswer

        return DraftAnswer(
            text="Your order SO-9001 is in production.",
            claims={0: [evidence[0].chunk_id]} if evidence else {},
        )


class _FailingExecutor:
    """What an unreachable ERP looks like from here."""

    async def execute(self, tool_name: str, parameters: dict, idempotency_key: str) -> dict | None:
        raise RuntimeError("CONNECTOR_UNAVAILABLE")

    async def verify_postcondition(self, tool_name: str, parameters: dict, output: object) -> bool:
        return False


class _FailingBusinessApiFactory:
    @staticmethod
    def build(context: object) -> _FailingExecutor:
        return _FailingExecutor()


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


def _seed_connector() -> None:
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO connectors (id, tenant_id, provider, name, status, "
                "capabilities, configuration, credential_ref) VALUES "
                "(:id, :t, 'business_api', 'ERP', 'active', CAST(:caps AS jsonb), "
                "CAST(:cfg AS jsonb), NULL)"
            ),
            {
                "id": str(uuid.uuid4()),
                "t": TENANT,
                "caps": json.dumps(["orders_read"]),
                "cfg": json.dumps({"base_url": "https://erp.test"}),
            },
        )
        conn.execute(
            text(
                "INSERT INTO feature_flags (id, tenant_id, key, description, enabled, "
                "rollout_percent, created_at) VALUES (:id, :t, :k, '', true, 100, 0) "
                "ON CONFLICT (tenant_id, key) DO UPDATE SET enabled = true, rollout_percent = 100"
            ),
            {"id": str(uuid.uuid4()), "t": TENANT, "k": "agent.business_read_enabled"},
        )
    admin.dispose()


def _clear() -> None:
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        for statement in (
            "DELETE FROM conversation_turns WHERE tenant_id = :t",
            "DELETE FROM citations WHERE tenant_id = :t",
            "DELETE FROM agent_runs WHERE tenant_id = :t",
            "DELETE FROM conversation_control_leases WHERE tenant_id = :t",
            "DELETE FROM audit_events WHERE tenant_id = :t",
            "DELETE FROM tool_executions WHERE tenant_id = :t",
            "DELETE FROM tool_proposals WHERE tenant_id = :t",
            "DELETE FROM tool_definitions WHERE tenant_id = :t",
            "DELETE FROM connectors WHERE tenant_id = :t",
            "DELETE FROM feature_flags WHERE tenant_id = :t",
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
    _seed_connector()
    yield
    _clear()


async def _execute(
    *, failing: bool = False, use_default_provider: bool = False
) -> tuple[object, list[dict]]:
    from platform_core.agent_runtime.orchestrator import AgentOrchestrator, OrchestratorDeps
    from platform_core.db import create_engine
    from platform_core.identity import lease_service
    from platform_core.retrieval.hybrid import PrincipalScope

    tid = uuid.UUID(TENANT)
    conv = uuid.uuid4()
    sender = _RecordingSender()
    engine = create_engine(APP_URL)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async with factory() as session:
        await session.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": TENANT})
        lease = await lease_service.acquire_or_get(session, tenant_id=tid, conversation_ref_id=conv)
        await session.commit()

    async with factory() as session:
        await session.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": TENANT})
        orch = AgentOrchestrator(
            session,
            OrchestratorDeps(
                sender=sender,
                tool_factories=(
                    None
                    if use_default_provider
                    else {
                        "business_api": (
                            _FailingBusinessApiFactory if failing else _FakeBusinessApiFactory
                        )
                    }
                ),
                generator=_ReceiptCitingGenerator(),
            ),
        )
        outcome = await orch.run(
            tenant_id=tid,
            conversation_ref_id=conv,
            question=QUESTION,
            principal=PrincipalScope(principal_types=("role",), principal_ids=("ai_agent",)),
            expected_lease_version=int(lease.lease_version),
            chatwoot_account_id="1",
            chatwoot_conversation_id="1",
        )
        await session.commit()
    await engine.dispose()
    return outcome, sender.calls


def test_an_order_question_is_answered_from_the_tool_receipt() -> None:
    outcome, _sent = _run(_execute())

    # Not handed off, not abstained: the tool had the answer and it is live
    # data, so the corpus was never consulted (ADR 0006).
    assert outcome.status.value == "completed", outcome.abstain_reason
    assert outcome.route == "business_read"
    assert outcome.citation_count >= 1


def test_the_receipt_reaches_the_conversation_as_data() -> None:
    """4A.3/4A.4, end to end.

    The customer sees the nodes and when the data was fetched, rather than a
    sentence describing them. Asserted here because the publisher alone cannot
    show that the read path actually calls it.
    """
    _outcome, _sent = _run(_execute())

    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        rows = conn.execute(
            sa.text(
                "SELECT role, text_redacted FROM conversation_turns "
                "WHERE tenant_id = :t AND role = 'tool'"
            ),
            {"t": TENANT},
        ).all()
    admin.dispose()

    assert rows, "no tool receipt was published to the timeline"
    body = rows[0][1]
    payload = json.loads(body)
    assert payload["order_id"] == "SO-9001"
    assert payload["status"] == "in_production"


def test_an_unreachable_erp_tells_the_customer_what_is_wrong() -> None:
    """10.2: graceful degradation, end to end.

    The read path is the one that talks to systems we do not own, so it is
    where an outage surfaces. The customer should be told it is an outage -
    not the generic "couldn't verify", which is true but leaves them waiting
    on us for something that is not ours.
    """
    outcome, sent = _run(_execute(failing=True))

    assert outcome.status.value == "abstained"
    # The gateway's name for "the external call failed". Arguments are
    # schema-validated before execution, so this is the other system.
    assert outcome.abstain_reason == "TOOL_EXECUTION_FAILED", outcome.abstain_reason

    customer_visible = [c["content"] for c in sent if not c["private"]]
    assert customer_visible, "an outage that says nothing leaves them waiting"
    joined = " ".join(customer_visible).lower()
    assert "not responding" in joined
    # And it still does not claim a ticket was filed.
    assert "ticket" not in joined


def test_the_shipped_demo_provider_answers_a_real_question() -> None:
    """10.1 with no external system: the read path runs on the shipped demo
    provider, not on a test double.

    The deployment has no ERP to call, which previously meant the path that
    answers "where is my order" was exercised only by fakes. This resolves the
    provider the way production does and asserts a real receipt comes back -
    carrying `source: demo` and `fetched_at`, so the card can say what it is
    and how old it is.
    """
    outcome, _sent = _run(_execute(use_default_provider=True))

    assert outcome.status.value == "completed", outcome.abstain_reason
    assert outcome.route == "business_read"

    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        rows = conn.execute(
            sa.text(
                "SELECT text_redacted FROM conversation_turns "
                "WHERE tenant_id = :t AND role = 'tool'"
            ),
            {"t": TENANT},
        ).all()
    admin.dispose()

    assert rows, "no receipt was published"
    payload = json.loads(rows[0][0])
    assert payload["order_id"] == "SO-9001"
    assert payload["status"] == "in_production"
    assert payload["source"] == "demo"
    assert payload["fetched_at"]
    assert len(payload["nodes"]) == 4
