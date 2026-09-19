"""E2E acceptance tests (ticket 20): the critical journeys from
docs/testing-and-evaluation.md, exercised through the HTTP surface and
real services (Postgres RLS, pgvector):

1. Duplicate webhook -> one InboxEvent row, one reply-worthy event.
2. Cross-tenant -> RLS denies every read/write path.
3. Human takeover during generation -> AI pre-send CAS aborts.
4. Insufficient evidence -> abstention with handoff reason.
5. Full knowledge QA: ingest -> retrieve -> cite -> validate.
"""

import hashlib
import hmac
import json
import os
import time
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from platform_core.agent_runtime.prompts import KNOWLEDGE_QA_PROMPT

pytestmark = pytest.mark.integration

ADMIN_URL = os.environ.get(
    "APP_ADMIN_DATABASE_URL",
    "postgresql+psycopg://platform:platform@localhost:5435/platform",
)
APP_URL = "postgresql+psycopg://platform_app:platform_app@localhost:5435/platform"
SECRET = os.environ.get("E2E_WEBHOOK_SECRET", "e2e-webhook-secret")
CHATWOOT_ACCOUNT = "7001"


def _run(coro):
    import asyncio

    return asyncio.run(coro, loop_factory=asyncio.SelectorEventLoop)


@pytest.fixture(scope="module", autouse=True)
def e2e_env() -> dict:
    from platform_core.config import Settings

    tid = str(uuid.uuid5(uuid.NAMESPACE_URL, "tenant:e2e-main"))
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO tenants (id, slug, name, status) VALUES "
                "(:id, 'e2e-main', 'E2E Tenant', 'active') "
                "ON CONFLICT (slug) DO NOTHING"
            ),
            {"id": tid},
        )
        conn.execute(
            text(
                "DELETE FROM external_resource_refs WHERE system='chatwoot' "
                "AND resource_type='account' AND external_id = :aid"
            ),
            {"aid": CHATWOOT_ACCOUNT},
        )
        conn.execute(
            text(
                "INSERT INTO external_resource_refs "
                "(id, tenant_id, system, resource_type, external_id) VALUES "
                "(:id, :tid, 'chatwoot', 'account', :aid)"
            ),
            {"id": uuid.uuid4(), "tid": tid, "aid": CHATWOOT_ACCOUNT},
        )
    with admin.begin() as conn:
        # Seed is deterministic-by-tenant; clear any leftovers from an
        # aborted previous run so re-seeding cannot violate unique keys.
        conn.execute(text("DELETE FROM chunks WHERE tenant_id = :t"), {"t": tid})
        conn.execute(text("DELETE FROM document_versions WHERE tenant_id = :t"), {"t": tid})
        conn.execute(text("DELETE FROM documents WHERE tenant_id = :t"), {"t": tid})
        conn.execute(text("DELETE FROM knowledge_spaces WHERE tenant_id = :t"), {"t": tid})
        conn.execute(text("DELETE FROM inbox_events WHERE tenant_id = :t"), {"t": tid})
    with admin.begin() as conn:
        # Seed one active document version with two chunks + embeddings
        space = uuid.uuid4()
        conn.execute(
            text("INSERT INTO knowledge_spaces (id, tenant_id, name) VALUES (:i, :t, 'p')"),
            {"i": space, "t": tid},
        )
        doc = uuid.uuid4()
        conn.execute(
            text(
                "INSERT INTO documents (id, tenant_id, space_id, canonical_uri, title) "
                "VALUES (:i, :t, :s, 'kb://e2e', 'E2E Policy')"
            ),
            {"i": doc, "t": tid, "s": space},
        )
        ver = uuid.uuid4()
        conn.execute(
            text(
                "INSERT INTO document_versions (id, tenant_id, document_id, "
                "version_label, content_hash, object_uri, status) VALUES "
                "(:i, :t, :d, 'v1', 'h', 'minio://e2e', 'active')"
            ),
            {"i": ver, "t": tid, "d": doc},
        )
        refund_text = "The refund window is 30 days for annual plans."
        conn.execute(
            text(
                "INSERT INTO chunks (id, tenant_id, document_version_id, ordinal, "
                "text, text_hash) VALUES (:i, :t, :v, 0, :x, 'r1')"
            ),
            {"i": uuid.uuid4(), "t": tid, "v": ver, "x": refund_text},
        )
    with admin.begin() as conn:
        from platform_core.retrieval.hybrid import _vector_literal, embed_deterministic

        rows = conn.execute(
            text("SELECT id, text FROM chunks WHERE tenant_id = :t"), {"t": tid}
        ).all()
        for cid, chunk_text in rows:
            conn.execute(
                text("UPDATE chunks SET embedding = CAST(:v AS vector) WHERE id = :i"),
                {"v": _vector_literal(embed_deterministic(chunk_text)), "i": cid},
            )
    admin.dispose()

    from platform_core.main import app
    from platform_core.support_bridge import router as bridge_router

    fake = Settings(environment="local", chatwoot_webhook_secret=SECRET)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(bridge_router, "get_settings", lambda: fake)
        yield {"tenant_id": tid, "app": app}
        return

    yield {"tenant_id": tid, "app": app}

    cleanup = create_engine(ADMIN_URL)
    with cleanup.begin() as conn:
        conn.execute(text("DELETE FROM chunks WHERE tenant_id = :t"), {"t": tid})
        conn.execute(text("DELETE FROM document_versions WHERE tenant_id = :t"), {"t": tid})
        conn.execute(text("DELETE FROM documents WHERE tenant_id = :t"), {"t": tid})
        conn.execute(text("DELETE FROM knowledge_spaces WHERE tenant_id = :t"), {"t": tid})
        conn.execute(text("DELETE FROM inbox_events WHERE tenant_id = :t"), {"t": tid})
        conn.execute(
            text("DELETE FROM external_resource_refs WHERE tenant_id = :t AND system='chatwoot'"),
            {"t": tid},
        )
        conn.execute(
            text("DELETE FROM conversation_control_leases WHERE tenant_id = :t"),
            {"t": tid},
        )
        conn.execute(text("DELETE FROM agent_runs WHERE tenant_id = :t"), {"t": tid})
        conn.execute(text("DELETE FROM tenants WHERE slug = 'e2e-main'"))
    cleanup.dispose()


def _post_webhook(client: TestClient, body: bytes, delivery_id: str) -> object:
    ts = str(int(time.time()))
    sig = hmac.new(SECRET.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return client.post(
        "/v1/webhooks/chatwoot",
        content=body,
        headers={"X-Signature": sig, "X-Timestamp": ts, "X-Delivery-Id": delivery_id},
    )


@pytest.mark.zero_tolerance("duplicate_replies")
def test_scenario_duplicate_webhook_single_ingest(e2e_env: dict) -> None:
    client = TestClient(e2e_env["app"], raise_server_exceptions=False)
    body = json.dumps(
        {
            "event": "message_created",
            "id": 500,
            "content": "customer asks about refunds",
            "message_type": "incoming",
            "conversation": {"id": 77, "inbox_id": 1},
            "account": {"id": int(CHATWOOT_ACCOUNT)},
        }
    ).encode()
    delivery = str(uuid.uuid4())

    first = _post_webhook(client, body, delivery)
    second = _post_webhook(client, body, delivery)

    assert first.status_code == 202
    assert second.status_code == 200 and second.json()["status"] == "duplicate"

    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        n = conn.execute(
            text("SELECT count(*) FROM inbox_events WHERE delivery_id = :d"),
            {"d": delivery},
        ).scalar()
    admin.dispose()
    assert n == 1


def test_scenario_cross_tenant_denied(e2e_env: dict) -> None:
    other_tenant = str(uuid.uuid5(uuid.NAMESPACE_URL, "tenant:e2e-other"))
    tid = e2e_env["tenant_id"]

    async def scenario() -> tuple[int, int]:
        from platform_core.db import create_engine

        engine = create_engine(APP_URL)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            await session.execute(
                text("SELECT set_config('app.tenant_id', :t, true)"), {"t": other_tenant}
            )
            visible = (
                await session.execute(
                    text("SELECT count(*) FROM chunks WHERE tenant_id = CAST(:t AS uuid)"),
                    {"t": tid},
                )
            ).scalar()
            await session.execute(
                text("SELECT set_config('app.tenant_id', :t, true)"), {"t": other_tenant}
            )
            try:
                await session.execute(
                    text(
                        "INSERT INTO chunks (id, tenant_id, document_version_id, "
                        "ordinal, text, text_hash) VALUES (:i, :t, "
                        "CAST('00000000-0000-0000-0000-000000000000' AS uuid), 0, 'x', 'h')"
                    ),
                    {"i": uuid.uuid4(), "t": tid},
                )
                injected = 1
            except Exception:
                injected = 0
            await session.rollback()
        await engine.dispose()
        return int(visible), injected

    visible, injected = _run(scenario())
    assert visible == 0  # other tenant cannot read e2e corpus
    assert injected == 0  # RLS blocks the write


def test_scenario_human_takeover_aborts_ai_send(e2e_env: dict) -> None:
    from platform_core.db import create_engine
    from platform_core.identity import lease_service
    from platform_core.identity.control_lease import LeaseConflict

    tid = uuid.UUID(e2e_env["tenant_id"])
    conv = uuid.uuid4()

    async def scenario() -> str:
        engine = create_engine(APP_URL)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            await session.execute(
                text("SELECT set_config('app.tenant_id', :t, true)"),
                {"t": str(tid)},
            )
            lease = await lease_service.acquire_or_get(
                session, tenant_id=tid, conversation_ref_id=conv
            )
            await session.commit()
        stale_version = int(lease.lease_version)

        # Human takes over while AI is "generating"
        async with factory() as session:
            await session.execute(
                text("SELECT set_config('app.tenant_id', :t, true)"),
                {"t": str(tid)},
            )
            await lease_service.transfer_to_human(
                session,
                tenant_id=tid,
                conversation_ref_id=conv,
                human_ref="agent-9",
                reason="e2e takeover",
            )
            await session.commit()

        # AI's pre-send check with stale version
        outcome = ""
        async with factory() as session:
            await session.execute(
                text("SELECT set_config('app.tenant_id', :t, true)"),
                {"t": str(tid)},
            )
            try:
                await lease_service.assert_can_send(
                    session,
                    tenant_id=tid,
                    conversation_ref_id=conv,
                    expected_version=stale_version,
                )
                outcome = "sent"
            except LeaseConflict as exc:
                outcome = f"aborted:{exc}"
            await session.rollback()
        await engine.dispose()
        return outcome

    outcome = _run(scenario())
    assert outcome.startswith("aborted:")


def test_scenario_full_knowledge_qa_with_citations(e2e_env: dict) -> None:
    from platform_core.agent_runtime.qa_path import (
        DraftAnswer,
        decide_abstention,
        validate_citations,
    )
    from platform_core.db import create_engine
    from platform_core.retrieval.hybrid import hybrid_search

    tid = uuid.UUID(e2e_env["tenant_id"])
    query = "how long is the refund window?"

    async def scenario() -> tuple:
        engine = create_engine(APP_URL)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            await session.execute(
                text("SELECT set_config('app.tenant_id', :t, true)"),
                {"t": str(tid)},
            )
            evidence = await hybrid_search(session, tenant_id=tid, query=query)
            await session.rollback()
        await engine.dispose()
        return evidence

    evidence = _run(scenario())
    assert evidence, "retrieval must find the seeded refund chunk"

    decision = decide_abstention(query, evidence)
    assert decision.abstain is False

    # Draft cites exactly the retrieved chunk; validator accepts
    draft = DraftAnswer(
        text="The refund window is 30 days for annual plans.",
        claims={0: [evidence[0].chunk_id]},
    )
    result = validate_citations(draft, evidence)
    assert result.ok is True

    # A phantom citation is rejected (never cite out-of-context versions)
    phantom_draft = DraftAnswer(text="x", claims={0: [uuid.uuid4()]})
    assert validate_citations(phantom_draft, evidence).ok is False


class _RecordingSender:
    """ChatwootClient-compatible transport double that records every send."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def send_message(self, *, account_id, conversation_id, content, command_id):
        self.calls.append(
            {
                "account_id": account_id,
                "conversation_id": conversation_id,
                "content": content,
                "command_id": command_id,
            }
        )

        class _Result:
            ambiguous = False

        return _Result()


class _UnusedGenerator:
    """Abstention happens before generation, so reaching this is a failure.

    Carries `template` because the orchestrator records prompt lineage on
    every run, including one that abstains before generating.
    """

    template = KNOWLEDGE_QA_PROMPT

    async def generate(self, question, evidence, **kwargs):  # pragma: no cover
        raise AssertionError("an abstaining run must never reach the model")


def test_scenario_insufficient_evidence_answers_the_customer(e2e_env: dict) -> None:
    """Scenario 4 from this module's docstring: "Insufficient evidence ->
    abstention with handoff reason".

    It was listed and never written, which is how the defect below survived:
    `_finish_abstain` computed `safe_abstention_text` and returned it in the
    RunOutcome **without dispatching it**, so an unanswerable question produced
    silence. The handoff happened internally; the customer was told nothing -
    not that the platform could not verify an answer, and not that a human was
    coming. Abstention is the most common failure mode, so that was the most
    common thing a customer could experience.

    The assertion is on the *transport*, not on the outcome object: the outcome
    carried the right text the whole time. Only a live Chatwoot - or this
    recording double - can tell you whether it was ever sent.
    """
    from platform_core.agent_runtime.orchestrator import AgentOrchestrator, OrchestratorDeps
    from platform_core.db import create_engine
    from platform_core.retrieval.hybrid import PrincipalScope

    tid = uuid.UUID(e2e_env["tenant_id"])
    conv = uuid.uuid4()
    # Nothing in the seeded corpus is about this, so retrieval finds no
    # evidence and the gate must abstain.
    question = "What is the airspeed velocity of an unladen swallow?"

    async def scenario() -> tuple[object, _RecordingSender]:
        engine = create_engine(APP_URL)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        sender = _RecordingSender()
        async with factory() as session:
            await session.execute(
                text("SELECT set_config('app.tenant_id', :t, true)"),
                {"t": str(tid)},
            )
            orchestrator = AgentOrchestrator(
                session,
                OrchestratorDeps(generator=_UnusedGenerator(), sender=sender),
            )
            outcome = await orchestrator.run(
                tenant_id=tid,
                conversation_ref_id=conv,
                question=question,
                principal=PrincipalScope(
                    principal_types=("role",), principal_ids=("support_agent",)
                ),
                chatwoot_account_id=CHATWOOT_ACCOUNT,
                chatwoot_conversation_id=str(conv),
            )
            await session.commit()
        await engine.dispose()
        return outcome, sender

    outcome, sender = _run(scenario())

    assert outcome.abstain_reason, "the run must record why it abstained"
    assert outcome.handoff is True, "no evidence means a human, not a retry"
    assert len(sender.calls) == 1, (
        "the customer-safe notice must actually be sent; an abstention that "
        "reaches nobody leaves the customer waiting on a reply that never comes"
    )
    sent = sender.calls[0]
    assert sent["conversation_id"] == str(conv)
    assert sent["account_id"] == CHATWOOT_ACCOUNT
    assert sent["content"] == outcome.answer_text
    assert "connect you with a human" in sent["content"].lower()
    assert sent["command_id"] == f"run:{outcome.run_id}", (
        "the outbound key is derived from the run id, so a retry cannot double-send"
    )
