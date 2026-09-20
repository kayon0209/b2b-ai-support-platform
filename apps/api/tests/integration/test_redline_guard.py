"""Integration: a draft that commits the company never reaches the customer.

Feature list 6.1/6.2 (P0, "出事一次，项目就死了") and the standing constraint
AI 不定价. The scan existed but was behind a default-off flag and had no
tests, so in practice nothing blocked anything: a control that is switched off
and never exercised is not a control.

These tests run the real pipeline with a generator that returns the offending
draft, and assert two things: the run abstains with
`REDLINE_COMMERCIAL_COMMITMENT`, and the commitment was **not sent** - the
abstention is about what the customer never sees.
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

TENANT = "01900000-0000-7000-8000-0000000000c9"
SLUG = "redline-guard"

# Long enough to clear the clarification gate, which runs before generation -
# a short question would clarify and never produce a draft, making both tests
# below pass for the wrong reason.
QUESTION = "请问你们的标准交期一般是多久？"
CHUNK = "标准交期 7 天，加急 3 天，最终以报价单为准。"

# Commitment verb (保证) + commercial object (交期) in one sentence.
COMMITTING = "我们保证交期 7 天。"
# The same topic, stated as policy: no promise, must be allowed through.
POLICY_ONLY = "标准交期以报价单为准。"


def _run(coro):
    return asyncio.run(coro, loop_factory=asyncio.SelectorEventLoop)


@pytest.fixture(scope="module", autouse=True)
def seed_corpus() -> None:
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO tenants (id, slug, name, status) VALUES "
                "(:id, :slug, 'Redline Guard', 'active') ON CONFLICT (slug) DO NOTHING"
            ),
            {"id": TENANT, "slug": SLUG},
        )
    with admin.begin() as conn:
        conn.execute(text("DELETE FROM chunks WHERE tenant_id = :t"), {"t": TENANT})
        conn.execute(text("DELETE FROM document_versions WHERE tenant_id = :t"), {"t": TENANT})
        conn.execute(text("DELETE FROM documents WHERE tenant_id = :t"), {"t": TENANT})
        conn.execute(text("DELETE FROM knowledge_spaces WHERE tenant_id = :t"), {"t": TENANT})
    with admin.begin() as conn:
        space = uuid.uuid4()
        conn.execute(
            text("INSERT INTO knowledge_spaces (id, tenant_id, name) VALUES (:i, :t, 'rl')"),
            {"i": space, "t": TENANT},
        )
        doc = uuid.uuid4()
        conn.execute(
            text(
                "INSERT INTO documents (id, tenant_id, space_id, canonical_uri, title) "
                "VALUES (:i, :t, :s, 'kb://rl', 'Lead Time')"
            ),
            {"i": doc, "t": TENANT, "s": space},
        )
        ver = uuid.uuid4()
        conn.execute(
            text(
                "INSERT INTO document_versions (id, tenant_id, document_id, version_label, "
                "content_hash, object_uri, status) VALUES "
                "(:i, :t, :d, 'v1', 'h', 'minio://rl', 'active')"
            ),
            {"i": ver, "t": TENANT, "d": doc},
        )
        conn.execute(
            text(
                "INSERT INTO chunks (id, tenant_id, document_version_id, ordinal, text, "
                "text_hash) VALUES (:i, :t, :v, 0, :x, 'rl1')"
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

    cleanup = create_engine(ADMIN_URL)
    with cleanup.begin() as conn:
        for statement in (
            "DELETE FROM citations WHERE tenant_id = :t",
            "DELETE FROM agent_runs WHERE tenant_id = :t",
            "DELETE FROM prompt_versions WHERE tenant_id = :t",
            "DELETE FROM conversation_control_leases WHERE tenant_id = :t",
            "DELETE FROM audit_events WHERE tenant_id = :t",
            "DELETE FROM chunks WHERE tenant_id = :t",
            "DELETE FROM document_versions WHERE tenant_id = :t",
            "DELETE FROM documents WHERE tenant_id = :t",
            "DELETE FROM knowledge_spaces WHERE tenant_id = :t",
        ):
            conn.execute(text(statement), {"t": TENANT})
        conn.execute(text("DELETE FROM tenants WHERE slug = :slug"), {"slug": SLUG})
    cleanup.dispose()


class _FixedGenerator:
    """Returns a chosen draft, citing the first evidence chunk.

    Bypasses the model: what is under test is what happens to a draft that
    already contains the violation, not whether a model would produce one.
    """

    def __init__(self, text_to_return: str) -> None:
        from platform_core.agent_runtime.prompts import KNOWLEDGE_QA_PROMPT

        self.template = KNOWLEDGE_QA_PROMPT
        self._text = text_to_return

    async def generate(self, question, evidence, **kwargs):
        from platform_core.agent_runtime.qa_path import DraftAnswer

        cited = {0: [evidence[0].chunk_id]} if evidence else {}
        return DraftAnswer(text=self._text, claims=cited)


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


async def _execute(*, draft_text: str):
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
            session, OrchestratorDeps(sender=sender, generator=_FixedGenerator(draft_text))
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


def test_a_committing_draft_is_blocked_with_the_flag_off() -> None:
    """The whole point: no tenant had to enable anything for this to hold."""
    outcome, sent = _run(_execute(draft_text=COMMITTING))

    assert outcome.abstain_reason == "REDLINE_COMMERCIAL_COMMITMENT"
    assert outcome.handoff is True
    # What the customer never sees. An abstention that still delivered the
    # sentence is not an abstention.
    customer_visible = [c["content"] for c in sent if not c["private"]]
    assert not any("保证交期" in text for text in customer_visible)


def test_the_same_topic_stated_as_policy_is_allowed_through() -> None:
    """Mutation guard against over-blocking.

    If the guard ever fires on a mere mention, the platform refuses to answer
    the L1 questions the knowledge base exists to answer - which would show up
    as a mysterious rise in handoffs rather than as a bug.
    """
    outcome, sent = _run(_execute(draft_text=POLICY_ONLY))

    assert outcome.abstain_reason != "REDLINE_COMMERCIAL_COMMITMENT"
    # Non-vacuous: the run must actually have answered. Without this the
    # assertion above would also hold for a run that clarified or found no
    # evidence, and the guard could block everything without anyone noticing.
    customer_visible = [c["content"] for c in sent if not c["private"]]
    assert any(POLICY_ONLY in content for content in customer_visible), customer_visible
