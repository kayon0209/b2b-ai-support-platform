"""Integration: `internal_draft` mode must not reach the customer.

This is a **defect fix with a safety consequence**, not a new feature.

`POST /v1/conversations/{ref}/agent-runs` has accepted `mode="internal_draft"`
since it was written, validated it against a whitelist, stored it on the run's
`model_config` - and **nothing ever read it**. The worker executed every run as a
customer reply, so an operator who asked for a draft to review got a message sent
to the customer instead. The mode promised the one thing it did not do.

The fix reads the mode from the run row at adoption and routes it through the
same suppression shadow mode already uses. The two differ only in who asked: a
feature flag asks for every conversation, a mode asks for this one.

The assertions that matter: **no send**, and **not FAILED**. A withheld delivery
that marks the run failed would make a draft indistinguishable from an outage,
and the operator would have no draft to review either.
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

_NS = uuid.uuid5(uuid.NAMESPACE_URL, "b2b-ai-support/tests/internal-draft")
TENANT = str(uuid.uuid5(_NS, "tenant"))

QUESTION = "请问你们的标准交期一般是多久？"
CHUNK = "标准交期 7 天，加急 3 天，最终以报价单为准。"
DRAFT = "标准交期以报价单为准。"


def _run(coro):
    return asyncio.run(coro, loop_factory=asyncio.SelectorEventLoop)


@pytest.fixture(scope="module", autouse=True)
def _seed():
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO tenants (id, slug, name, status) VALUES "
                "(:id, 'draft-mode', 'Draft', 'active') ON CONFLICT (slug) DO NOTHING"
            ),
            {"id": TENANT},
        )
    with admin.begin() as conn:
        conn.execute(text("DELETE FROM chunks WHERE tenant_id = :t"), {"t": TENANT})
        conn.execute(text("DELETE FROM document_versions WHERE tenant_id = :t"), {"t": TENANT})
        conn.execute(text("DELETE FROM documents WHERE tenant_id = :t"), {"t": TENANT})
        conn.execute(text("DELETE FROM knowledge_spaces WHERE tenant_id = :t"), {"t": TENANT})
        space, doc, ver = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        conn.execute(
            text("INSERT INTO knowledge_spaces (id, tenant_id, name) VALUES (:i, :t, 'p')"),
            {"i": space, "t": TENANT},
        )
        conn.execute(
            text(
                "INSERT INTO documents (id, tenant_id, space_id, canonical_uri, title) "
                "VALUES (:i, :t, :s, 'kb://draft', 'Policy')"
            ),
            {"i": doc, "t": TENANT, "s": space},
        )
        conn.execute(
            text(
                "INSERT INTO document_versions (id, tenant_id, document_id, version_label, "
                "content_hash, object_uri, status) VALUES "
                "(:i, :t, :d, 'v1', 'h', 'minio://d', 'active')"
            ),
            {"i": ver, "t": TENANT, "d": doc},
        )
        conn.execute(
            text(
                "INSERT INTO chunks (id, tenant_id, document_version_id, ordinal, text, text_hash) "
                "VALUES (:i, :t, :v, 0, :x, 'r1')"
            ),
            {"i": uuid.uuid4(), "t": TENANT, "v": ver, "x": CHUNK},
        )
    with admin.begin() as conn:
        from platform_core.retrieval.hybrid import _vector_literal, embed_deterministic

        rows = conn.execute(
            text("SELECT id, text FROM chunks WHERE tenant_id = :t"), {"t": TENANT}
        ).all()
        for cid, chunk_text in rows:
            conn.execute(
                text("UPDATE chunks SET embedding = CAST(:v AS vector) WHERE id = :i"),
                {"v": _vector_literal(embed_deterministic(chunk_text)), "i": cid},
            )
    admin.dispose()
    yield
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        for table in (
            "conversation_turns",
            "conversation_control_leases",
            "citations",
            "agent_runs",
            "chunks",
            "document_versions",
            "documents",
            "knowledge_spaces",
        ):
            conn.execute(text(f"DELETE FROM {table} WHERE tenant_id = :t"), {"t": TENANT})  # noqa: S608
        conn.execute(text("DELETE FROM tenants WHERE slug = 'draft-mode'"))
    admin.dispose()


class _FixedGenerator:
    """Cites the seeded chunk, so the run completes rather than abstaining."""

    def __init__(self) -> None:
        from platform_core.agent_runtime.prompts import KNOWLEDGE_QA_PROMPT

        self.template = KNOWLEDGE_QA_PROMPT

    async def generate(self, question, evidence, **kwargs):
        from platform_core.agent_runtime.qa_path import DraftAnswer

        return DraftAnswer(text=DRAFT, claims={0: [evidence[0].chunk_id]} if evidence else {})


class _RecordingTransport:
    system = "email"

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def send(self, *, address, conversation_key, content, command_id):
        self.calls.append({"content": content, "command_id": command_id})
        return SendResult()


async def _execute(mode: str) -> tuple[object, list[dict]]:
    from platform_core.agent_runtime import chat_service
    from platform_core.agent_runtime.orchestrator import AgentOrchestrator, OrchestratorDeps
    from platform_core.db import create_engine as async_engine
    from platform_core.identity import lease_service
    from platform_core.identity.tenant_context import TenantContext
    from platform_core.retrieval.hybrid import PrincipalScope

    tid = uuid.UUID(TENANT)
    conv = uuid.uuid4()
    sender = _RecordingTransport()
    engine = async_engine(APP_URL)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async with factory() as session:
        await session.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": TENANT})
        await lease_service.acquire_or_get(session, tenant_id=tid, conversation_ref_id=conv)
        await session.commit()

    # The enqueue path the API uses, so the mode lands on the run row exactly
    # the way a real request puts it there.
    async with factory() as session:
        await session.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": TENANT})
        await chat_service.queue_agent_run(
            session,
            ctx=TenantContext(tenant_id=tid, actor_id=None, actor_kind="user"),
            conversation_ref_id=conv,
            external_ref=str(conv),
            trigger_message_ref="msg-1",
            idem=f"idem-{uuid.uuid4()}",
            mode=mode,
            trace_id="trace-draft",
        )
        await session.commit()

    async with factory() as session:
        await session.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": TENANT})
        orch = AgentOrchestrator(
            session,
            OrchestratorDeps(
                channel_sender=ChannelSender({"email": sender}), generator=_FixedGenerator()
            ),
        )
        outcome = await orch.run(
            tenant_id=tid,
            conversation_ref_id=conv,
            question=QUESTION,
            principal=PrincipalScope(principal_types=("role",), principal_ids=("ai_agent",)),
            # A channel is **required for this test to mean anything**. Without
            # one, `_dispatch` takes the platform-surface branch and sends
            # nothing whatever the mode is - so "no send" would pass for the
            # wrong reason and the defect would survive the test written to
            # catch it.
            channel_system="email",
            channel_address="buyer@example.test",
        )
        await session.commit()

    await engine.dispose()
    return outcome, sender.calls


def test_an_internal_draft_run_does_not_reach_the_customer() -> None:
    """The defect: this mode was accepted, stored, and ignored - so asking for a
    draft sent a message."""
    outcome, sent = _run(_execute("internal_draft"))

    assert sent == [], "an internal draft must not be sent to the customer"
    # Withheld, not failed: the operator still needs the draft to review, and a
    # FAILED run is indistinguishable from an outage.
    assert outcome.status.value == "completed"


def test_a_customer_reply_run_still_sends() -> None:
    """The other half: the fix must not suppress ordinary replies.

    Without this, "no send" would be satisfied by breaking delivery entirely.
    """
    outcome, sent = _run(_execute("customer_reply"))

    assert outcome.status.value == "completed"
    assert len(sent) == 1
    assert sent[0]["content"] == DRAFT
    assert sent[0]["command_id"] == f"run:{outcome.run_id}"
