"""Integration: shadow mode produces the answer and withholds it.

Feature list 9.2, and the step that makes the leak analysis safe to act on:
a category is marked automatable, someone writes the document, and before the
platform starts answering customers on its own you watch what it *would* have
said.

The assertion that matters is that a shadow run is **not** a failed run. If
withholding the send marked the run FAILED, a shadow window would be
indistinguishable from an outage - the exact opposite of what the observation
is for.
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

TENANT = "01900000-0000-7000-8000-0000000000cf"
SLUG = "shadow-mode"

QUESTION = "请问你们的标准交期一般是多久？"
CHUNK = "标准交期 7 天，加急 3 天，最终以报价单为准。"
DRAFT = "标准交期以报价单为准。"


def _run(coro):
    return asyncio.run(coro, loop_factory=asyncio.SelectorEventLoop)


def _set_shadow(enabled: bool) -> None:
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO feature_flags (id, tenant_id, key, description, enabled, "
                "rollout_percent, created_at) VALUES (:id, :t, :k, '', :e, 100, 0) "
                "ON CONFLICT (tenant_id, key) DO UPDATE SET enabled = :e, rollout_percent = 100"
            ),
            {"id": str(uuid.uuid4()), "t": TENANT, "k": "agent.shadow_mode_enabled", "e": enabled},
        )
    admin.dispose()


@pytest.fixture(scope="module", autouse=True)
def seed_corpus() -> None:
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO tenants (id, slug, name, status) VALUES "
                "(:id, :slug, 'Shadow Mode', 'active') ON CONFLICT (slug) DO NOTHING"
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
            text("INSERT INTO knowledge_spaces (id, tenant_id, name) VALUES (:i, :t, 'sh')"),
            {"i": space, "t": TENANT},
        )
        doc = uuid.uuid4()
        conn.execute(
            text(
                "INSERT INTO documents (id, tenant_id, space_id, canonical_uri, title) "
                "VALUES (:i, :t, :s, 'kb://sh', 'Lead Time')"
            ),
            {"i": doc, "t": TENANT, "s": space},
        )
        ver = uuid.uuid4()
        conn.execute(
            text(
                "INSERT INTO document_versions (id, tenant_id, document_id, version_label, "
                "content_hash, object_uri, status) VALUES "
                "(:i, :t, :d, 'v1', 'h', 'minio://sh', 'active')"
            ),
            {"i": ver, "t": TENANT, "d": doc},
        )
        conn.execute(
            text(
                "INSERT INTO chunks (id, tenant_id, document_version_id, ordinal, text, "
                "text_hash) VALUES (:i, :t, :v, 0, :x, 'sh1')"
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
            "DELETE FROM feature_flags WHERE tenant_id = :t",
            "DELETE FROM chunks WHERE tenant_id = :t",
            "DELETE FROM document_versions WHERE tenant_id = :t",
            "DELETE FROM documents WHERE tenant_id = :t",
            "DELETE FROM knowledge_spaces WHERE tenant_id = :t",
        ):
            conn.execute(text(statement), {"t": TENANT})
        conn.execute(text("DELETE FROM tenants WHERE slug = :slug"), {"slug": SLUG})
    cleanup.dispose()


class _FixedGenerator:
    def __init__(self) -> None:
        from platform_core.agent_runtime.prompts import KNOWLEDGE_QA_PROMPT

        self.template = KNOWLEDGE_QA_PROMPT

    async def generate(self, question, evidence, **kwargs):
        from platform_core.agent_runtime.qa_path import DraftAnswer

        return DraftAnswer(text=DRAFT, claims={0: [evidence[0].chunk_id]} if evidence else {})


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


async def _execute() -> tuple[object, list[str]]:
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
            expected_lease_version=int(lease.lease_version),
            channel_system="email",
            channel_address="buyer@example.test",
            channel_conversation_key="1",
        )
        await session.commit()
    await engine.dispose()
    return outcome, [c["content"] for c in sender.calls]


def test_shadow_mode_answers_but_does_not_send() -> None:
    _set_shadow(True)

    outcome, sent = _run(_execute())

    # Completed, not failed: the answer exists and cleared every gate.
    assert outcome.status.value == "completed", outcome.send_blocked_reason
    assert sent == []
    # The reason is recorded rather than silently dropped - "no message" has
    # to be distinguishable from "no answer".
    assert outcome.send_blocked_reason == "SHADOW_MODE"


def test_with_shadow_off_the_answer_is_delivered() -> None:
    """Mutation guard: shadow must not suppress every send by accident."""
    _set_shadow(False)

    outcome, sent = _run(_execute())

    assert outcome.status.value == "completed"
    assert DRAFT in sent
    assert outcome.send_blocked_reason != "SHADOW_MODE"
