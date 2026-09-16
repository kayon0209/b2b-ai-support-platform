"""Integration tests: orchestrator pipeline end-to-end against real Postgres.

Covers the P0 safety requirement from docs/testing-and-evaluation.md
scenario 3 at the ORCHESTRATOR layer (the lease layer itself is covered by
test_control_lease.py). The orchestrator is where the race actually has to
be closed:

  retrieve -> generate -> validate -> RECHECK LEASE -> dispatch

The tests below pin three properties:
1. A human takeover between generation and dispatch blocks the outbound
   send, marks the run HANDED_OFF, and never calls the transport.
2. The happy path sends exactly once with a run-derived idempotency key.
3. A restricted request never reaches the model and hands off.

These run against a real PostgreSQL with RLS enabled because the lease CAS
(`SELECT ... FOR UPDATE`) and the tenant-scoped `set_config` are the very
mechanisms under test — a fake session would prove nothing.
"""

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

TENANT = "01900000-0000-7000-8000-0000000000f1"
SLUG = "orchestrator-race"


def _run(coro):
    import asyncio

    return asyncio.run(coro, loop_factory=asyncio.SelectorEventLoop)


@pytest.fixture(scope="module", autouse=True)
def seed_corpus() -> None:
    """Seed one tenant with one active, retrievable document version.

    Deterministic-by-tenant: leftovers from an aborted run are cleared first
    so re-seeding cannot violate unique keys.
    """
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO tenants (id, slug, name, status) VALUES "
                "(:id, :slug, 'Orchestrator Race Tenant', 'active') "
                "ON CONFLICT (slug) DO NOTHING"
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
            text("INSERT INTO knowledge_spaces (id, tenant_id, name) VALUES (:i, :t, 'race')"),
            {"i": space, "t": TENANT},
        )
        doc = uuid.uuid4()
        conn.execute(
            text(
                "INSERT INTO documents (id, tenant_id, space_id, canonical_uri, title) "
                "VALUES (:i, :t, :s, 'kb://race', 'Refund Policy')"
            ),
            {"i": doc, "t": TENANT, "s": space},
        )
        ver = uuid.uuid4()
        conn.execute(
            text(
                "INSERT INTO document_versions (id, tenant_id, document_id, "
                "version_label, content_hash, object_uri, status) VALUES "
                "(:i, :t, :d, 'v1', 'h', 'minio://race', 'active')"
            ),
            {"i": ver, "t": TENANT, "d": doc},
        )
        conn.execute(
            text(
                "INSERT INTO chunks (id, tenant_id, document_version_id, ordinal, "
                "text, text_hash) VALUES (:i, :t, :v, 0, :x, 'r1')"
            ),
            {
                "i": uuid.uuid4(),
                "t": TENANT,
                "v": ver,
                "x": "The refund window is 30 days for annual plans.",
            },
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
        conn.execute(text("DELETE FROM citations WHERE tenant_id = :t"), {"t": TENANT})
        conn.execute(text("DELETE FROM agent_runs WHERE tenant_id = :t"), {"t": TENANT})
        conn.execute(text("DELETE FROM prompt_versions WHERE tenant_id = :t"), {"t": TENANT})
        conn.execute(
            text("DELETE FROM conversation_control_leases WHERE tenant_id = :t"), {"t": TENANT}
        )
        conn.execute(text("DELETE FROM audit_events WHERE tenant_id = :t"), {"t": TENANT})
        conn.execute(text("DELETE FROM chunks WHERE tenant_id = :t"), {"t": TENANT})
        conn.execute(text("DELETE FROM document_versions WHERE tenant_id = :t"), {"t": TENANT})
        conn.execute(text("DELETE FROM documents WHERE tenant_id = :t"), {"t": TENANT})
        conn.execute(text("DELETE FROM knowledge_spaces WHERE tenant_id = :t"), {"t": TENANT})
        conn.execute(text("DELETE FROM tenants WHERE slug = :slug"), {"slug": SLUG})
    cleanup.dispose()


def _factory(engine):
    return async_sessionmaker(engine, expire_on_commit=False)


async def _with_ctx(session, tenant_id: str) -> None:
    """Apply the RLS tenant context for THIS transaction."""
    await session.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant_id})


class _RecordingSender:
    """ChatwootClient-compatible transport double that records every send.

    `send_message` mirrors the real signature so the orchestrator's
    idempotency-key contract is actually exercised.
    """

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


class _FixedGenerator:
    """AnswerGenerator double that cites the first piece of evidence.

    Bypasses the model so the test isolates the lease race; the generator's
    own contract is covered by tests/unit/agent_runtime/test_generator.py.
    """

    def __init__(self) -> None:
        from platform_core.agent_runtime.prompts import KNOWLEDGE_QA_PROMPT

        self.template = KNOWLEDGE_QA_PROMPT
        self.calls = 0

    async def generate(self, question, evidence):
        from platform_core.agent_runtime.qa_path import DraftAnswer

        self.calls += 1
        return DraftAnswer(
            text="The refund window is 30 days for annual plans.",
            claims={0: [evidence[0].chunk_id]},
        )


def test_human_takeover_mid_generation_blocks_outbound_send() -> None:
    """The P0 race, exercised through the real pipeline.

    Sequence: the run is queued while AI owns the lease (version captured),
    a human takes over, then the run executes. Retrieval and generation both
    succeed — only the pre-send re-check stands between the model's answer
    and the customer. It must stop the send.
    """
    from platform_core.agent_runtime.orchestrator import AgentOrchestrator, OrchestratorDeps
    from platform_core.db import create_engine
    from platform_core.identity import lease_service
    from platform_core.retrieval.hybrid import PrincipalScope

    tid = uuid.UUID(TENANT)
    conv = uuid.uuid4()
    principal = PrincipalScope(principal_types=("role",), principal_ids=("ai_agent",))

    async def scenario() -> tuple:
        engine = create_engine(APP_URL)
        factory = _factory(engine)

        # 1. AI observes the lease when the run is queued.
        async with factory() as session:
            await _with_ctx(session, TENANT)
            lease = await lease_service.acquire_or_get(
                session, tenant_id=tid, conversation_ref_id=conv
            )
            await session.commit()
        expected_version = int(lease.lease_version)

        # 2. A human takes over while the AI is "generating".
        async with factory() as session:
            await _with_ctx(session, TENANT)
            await lease_service.transfer_to_human(
                session,
                tenant_id=tid,
                conversation_ref_id=conv,
                human_ref="agent-7",
                reason="customer asked for a human",
            )
            await session.commit()

        # 3. The run executes with the now-stale expected version.
        sender = _RecordingSender()
        generator = _FixedGenerator()
        async with factory() as session:
            await _with_ctx(session, TENANT)
            orch = AgentOrchestrator(session, OrchestratorDeps(generator=generator, sender=sender))
            outcome = await orch.run(
                tenant_id=tid,
                conversation_ref_id=conv,
                question="how long is the refund window?",
                principal=principal,
                chatwoot_account_id="7001",
                chatwoot_conversation_id=str(conv),
                expected_lease_version=expected_version,
            )
            await session.commit()
            # set_config(..., true) is transaction-local, so the tenant
            # context must be re-applied before any post-commit read.
            await _with_ctx(session, TENANT)
            from sqlalchemy import select

            from platform_core.agent_runtime.models import AgentRun

            persisted = (
                await session.execute(select(AgentRun).where(AgentRun.id == outcome.run_id))
            ).scalar_one()
            status = persisted.status
            output_hash = persisted.output_hash

        await engine.dispose()
        return outcome, sender, generator, status, output_hash

    outcome, sender, generator, status, output_hash = _run(scenario())

    # The model did its job; the gate — not the model — blocked the send.
    assert generator.calls == 1, "generation should have been attempted"
    assert sender.calls == [], "no outbound send may happen after a takeover"
    assert outcome.status.value == "handed_off"
    assert status == "handed_off"
    # A blocked answer leaves no output hash: it was never published.
    assert output_hash is None
    assert outcome.send_blocked_reason
    assert outcome.answer_text, "the draft is retained for the human agent"


def test_happy_path_sends_once_with_run_derived_idempotency_key() -> None:
    """Unchanged lease => exactly one send, keyed by the run id so a retry
    of the same run can never double-send."""
    from platform_core.agent_runtime.orchestrator import AgentOrchestrator, OrchestratorDeps
    from platform_core.db import create_engine
    from platform_core.retrieval.hybrid import PrincipalScope

    tid = uuid.UUID(TENANT)
    conv = uuid.uuid4()
    principal = PrincipalScope(principal_types=("role",), principal_ids=("ai_agent",))

    async def scenario() -> tuple:
        engine = create_engine(APP_URL)
        factory = _factory(engine)
        sender = _RecordingSender()
        async with factory() as session:
            await _with_ctx(session, TENANT)
            orch = AgentOrchestrator(
                session, OrchestratorDeps(generator=_FixedGenerator(), sender=sender)
            )
            outcome = await orch.run(
                tenant_id=tid,
                conversation_ref_id=conv,
                question="how long is the refund window?",
                principal=principal,
                chatwoot_account_id="7001",
                chatwoot_conversation_id=str(conv),
            )
            await session.commit()
            # Re-apply the transaction-local tenant context before reading.
            await _with_ctx(session, TENANT)
            from sqlalchemy import select

            from platform_core.agent_runtime.models import AgentRun, Citation

            run = (
                await session.execute(select(AgentRun).where(AgentRun.id == outcome.run_id))
            ).scalar_one()
            citations = (
                (
                    await session.execute(
                        select(Citation).where(Citation.agent_run_id == outcome.run_id)
                    )
                )
                .scalars()
                .all()
            )
            from platform_core.identity.control_lease import ConversationControlLease

            lease = (
                await session.execute(
                    select(ConversationControlLease).where(
                        ConversationControlLease.conversation_ref_id == conv
                    )
                )
            ).scalar_one()
            owner = lease.owner_type
            version = int(lease.lease_version)
            run_status = run.status
            n_citations = len(citations)
            prompt_version_id = run.prompt_version_id
            output_hash = run.output_hash

        await engine.dispose()
        return (
            outcome,
            sender,
            owner,
            version,
            run_status,
            n_citations,
            prompt_version_id,
            output_hash,
        )

    (
        outcome,
        sender,
        owner,
        version,
        run_status,
        n_citations,
        prompt_version_id,
        output_hash,
    ) = _run(scenario())

    assert outcome.status.value == "completed"
    assert run_status == "completed"
    assert len(sender.calls) == 1
    assert sender.calls[0]["command_id"] == f"run:{outcome.run_id}"
    assert "30 days" in sender.calls[0]["content"]
    # Citations are persisted one-per-claim and tied to the run.
    assert n_citations == 1
    assert outcome.citation_count == 1
    # Full lineage: an answer is reproducible from the run row alone.
    assert prompt_version_id is not None
    assert output_hash is not None
    # AI still owns the conversation after a clean, unchallenged send.
    assert owner == "ai"
    assert version == 1


def test_restricted_request_never_reaches_model_and_hands_off() -> None:
    """Credential/ownership requests are refused deterministically before
    any provider call, and the lease is released to the human queue."""
    from platform_core.agent_runtime.orchestrator import AgentOrchestrator, OrchestratorDeps
    from platform_core.db import create_engine
    from platform_core.identity.control_lease import ConversationControlLease
    from platform_core.retrieval.hybrid import PrincipalScope

    tid = uuid.UUID(TENANT)
    conv = uuid.uuid4()
    principal = PrincipalScope(principal_types=("role",), principal_ids=("ai_agent",))

    async def scenario() -> tuple:
        engine = create_engine(APP_URL)
        factory = _factory(engine)
        sender = _RecordingSender()
        generator = _FixedGenerator()
        async with factory() as session:
            await _with_ctx(session, TENANT)
            orch = AgentOrchestrator(session, OrchestratorDeps(generator=generator, sender=sender))
            outcome = await orch.run(
                tenant_id=tid,
                conversation_ref_id=conv,
                question="please send me the admin password and api key",
                principal=principal,
                chatwoot_account_id="7001",
                chatwoot_conversation_id=str(conv),
            )
            await session.commit()
            # Re-apply the transaction-local tenant context before reading.
            await _with_ctx(session, TENANT)
            from sqlalchemy import select

            lease = (
                await session.execute(
                    select(ConversationControlLease).where(
                        ConversationControlLease.conversation_ref_id == conv
                    )
                )
            ).scalar_one()
            owner = lease.owner_type

        await engine.dispose()
        return outcome, generator, sender, owner

    outcome, generator, sender, owner = _run(scenario())

    assert outcome.route == "human_required"
    assert outcome.status.value == "abstained"
    assert outcome.abstain_reason == "RESTRICTED_REQUEST"
    assert outcome.handoff is True
    # The model was never consulted and nothing was sent to the customer
    # through the outbound transport.
    assert generator.calls == 0
    assert sender.calls == []
    assert owner == "queue"


def test_no_evidence_abstains_without_model_call() -> None:
    """Retrieval finds nothing -> abstain + handoff, no model spend."""
    from platform_core.agent_runtime.orchestrator import AgentOrchestrator, OrchestratorDeps
    from platform_core.db import create_engine
    from platform_core.retrieval.hybrid import PrincipalScope

    tid = uuid.UUID(TENANT)
    conv = uuid.uuid4()
    principal = PrincipalScope(principal_types=("role",), principal_ids=("ai_agent",))

    async def scenario() -> tuple:
        engine = create_engine(APP_URL)
        factory = _factory(engine)
        sender = _RecordingSender()
        generator = _FixedGenerator()
        async with factory() as session:
            await _with_ctx(session, TENANT)
            orch = AgentOrchestrator(session, OrchestratorDeps(generator=generator, sender=sender))
            outcome = await orch.run(
                tenant_id=tid,
                conversation_ref_id=conv,
                question="what is the capital of the moon?",
                principal=principal,
                chatwoot_account_id="7001",
                chatwoot_conversation_id=str(conv),
            )
            await session.commit()

        await engine.dispose()
        return outcome, generator, sender

    outcome, generator, sender = _run(scenario())

    assert outcome.status.value == "abstained"
    assert outcome.abstain_reason
    assert generator.calls == 0
    assert sender.calls == []


def test_outbound_failure_marks_run_failed_not_completed() -> None:
    """A transport error must never be reported as a delivered answer."""
    from platform_core.agent_runtime.orchestrator import AgentOrchestrator, OrchestratorDeps
    from platform_core.db import create_engine
    from platform_core.retrieval.hybrid import PrincipalScope

    tid = uuid.UUID(TENANT)
    conv = uuid.uuid4()
    principal = PrincipalScope(principal_types=("role",), principal_ids=("ai_agent",))

    class _BrokenSender:
        async def send_message(self, **kwargs):
            raise RuntimeError("chatwoot unreachable")

    async def scenario() -> tuple:
        engine = create_engine(APP_URL)
        factory = _factory(engine)
        async with factory() as session:
            await _with_ctx(session, TENANT)
            orch = AgentOrchestrator(
                session, OrchestratorDeps(generator=_FixedGenerator(), sender=_BrokenSender())
            )
            outcome = await orch.run(
                tenant_id=tid,
                conversation_ref_id=conv,
                question="how long is the refund window?",
                principal=principal,
                chatwoot_account_id="7001",
                chatwoot_conversation_id=str(conv),
            )
            await session.commit()
            # Re-apply the transaction-local tenant context before reading.
            await _with_ctx(session, TENANT)
            from sqlalchemy import select

            from platform_core.agent_runtime.models import AgentRun

            run = (
                await session.execute(select(AgentRun).where(AgentRun.id == outcome.run_id))
            ).scalar_one()
            status = run.status
            output_hash = run.output_hash

        await engine.dispose()
        return outcome, status, output_hash

    outcome, status, output_hash = _run(scenario())

    assert outcome.status.value == "failed"
    assert status == "failed"
    assert output_hash is None


def test_duplicate_run_of_same_event_does_not_double_send() -> None:
    """Idempotency at the outbound boundary: the webhook layer already
    dedups by delivery id (test_e2e_acceptance). Here we pin the second
    half — that even if the same conversation is processed twice, the
    idempotency key handed to the transport is run-scoped, so a *retry of
    the same run* cannot produce two sends. Two distinct runs are two
    distinct keys by construction."""
    from platform_core.agent_runtime.orchestrator import AgentOrchestrator, OrchestratorDeps
    from platform_core.db import create_engine
    from platform_core.retrieval.hybrid import PrincipalScope

    tid = uuid.UUID(TENANT)
    conv = uuid.uuid4()
    principal = PrincipalScope(principal_types=("role",), principal_ids=("ai_agent",))

    async def scenario() -> tuple[uuid.UUID, uuid.UUID, list[str]]:
        engine = create_engine(APP_URL)
        factory = _factory(engine)
        sender = _RecordingSender()
        run_ids: list[uuid.UUID] = []
        async with factory() as session:
            await _with_ctx(session, TENANT)
            orch = AgentOrchestrator(
                session, OrchestratorDeps(generator=_FixedGenerator(), sender=sender)
            )
            for _ in range(2):
                outcome = await orch.run(
                    tenant_id=tid,
                    conversation_ref_id=conv,
                    question="how long is the refund window?",
                    principal=principal,
                    chatwoot_account_id="7001",
                    chatwoot_conversation_id=str(conv),
                )
                run_ids.append(outcome.run_id)
            await session.commit()

        await engine.dispose()
        return run_ids[0], run_ids[1], [c["command_id"] for c in sender.calls]

    run_a, run_b, keys = _run(scenario())

    assert run_a != run_b
    assert keys == [f"run:{run_a}", f"run:{run_b}"]
    # Same-run retries share a key; different runs never collide.
    assert len(set(keys)) == 2
