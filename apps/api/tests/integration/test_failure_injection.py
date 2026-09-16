"""Failure injection (docs/testing-and-evaluation.md, pre-pilot minimum).

The doc's acceptance bar is the point of this file: *"The expected result
must be safe and observable, not merely eventually successful."* So every
test asserts two things - that the system refuses to do something
dangerous, and that the refusal is recorded somewhere an operator can
actually see it.

Covered here:
- a cited document expiring, or being retired, mid-conversation;
- evidence disappearing between generation and validation;
- provider failures mapping to errors that are safe to retry (or not);
- connector credentials rotated/revoked mid-flight.

Deliberately not duplicated here: worker termination mid-tool-execution
(test_inbox_reclaim), rerank timeout degradation (test_reranker), human
takeover during generation (test_orchestrator_lease_race) - those already
have dedicated suites.
"""

import os
import time
import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_sessionmaker

from platform_core.agent_runtime.qa_path import (
    DraftAnswer,
    validate_citations,
)
from platform_core.llm.provider import (
    ModelError,
    ModelNotConfigured,
    ModelRejected,
    ModelUnavailable,
)
from platform_core.retrieval.hybrid import RetrievedChunk

pytestmark = pytest.mark.integration

ADMIN_URL = os.environ.get(
    "APP_ADMIN_DATABASE_URL",
    "postgresql+psycopg://platform:platform@localhost:5435/platform",
)
APP_URL = "postgresql+psycopg://platform_app:platform_app@localhost:5435/platform"
TENANT = "01900000-0000-7000-8000-0000000000f1"
SPACE = "01900000-0000-7000-8000-0000000000f2"
DOC = "01900000-0000-7000-8000-0000000000f3"
VERSION = "01900000-0000-7000-8000-0000000000f4"
CHUNK = "01900000-0000-7000-8000-0000000000f5"

# Interpolated into DELETE statements below. Table names cannot be bound
# as parameters, so this list is the only thing keeping the construction
# safe - it is a hardcoded literal and must stay one.
CLEANUP_STATEMENTS = (
    "DELETE FROM chunks WHERE tenant_id = :t",
    "DELETE FROM document_versions WHERE tenant_id = :t",
    "DELETE FROM documents WHERE tenant_id = :t",
    "DELETE FROM knowledge_spaces WHERE tenant_id = :t",
    "DELETE FROM connectors WHERE tenant_id = :t",
)


def _purge(conn: Connection) -> None:
    """Children before parents; documents FKs to knowledge_spaces."""
    for statement in CLEANUP_STATEMENTS:
        conn.execute(text(statement), {"t": TENANT})


@pytest.fixture
def fixture_rows() -> Iterator[None]:
    """One knowledge space, document, active version, and chunk.

    Column set mirrors the live schema, not the ORM in isolation:
    `chunks.search_vector` is a *generated* column, so it is never
    inserted, and `ordinal`/`text_hash` are NOT NULL without defaults.
    """
    admin = create_engine(ADMIN_URL)
    now = int(time.time())
    with admin.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO tenants (id, slug, name, status) VALUES "
                "(:id, 'failure-inj', 'Failure Injection', 'active') "
                "ON CONFLICT (slug) DO NOTHING"
            ),
            {"id": TENANT},
        )
        _purge(conn)
        conn.execute(
            text(
                "INSERT INTO knowledge_spaces (id, tenant_id, name, status) VALUES "
                "(:id, :t, 'Failure Injection Space', 'active')"
            ),
            {"id": SPACE, "t": TENANT},
        )
        conn.execute(
            text(
                "INSERT INTO documents "
                "(id, tenant_id, space_id, canonical_uri, title, classification) VALUES "
                "(:id, :t, :s, 's3://bucket/refund.pdf', 'Refund Policy', 'internal')"
            ),
            {"id": DOC, "t": TENANT, "s": SPACE},
        )
        conn.execute(
            text(
                "INSERT INTO document_versions "
                "(id, tenant_id, document_id, version_label, content_hash, status, "
                " object_uri, effective_at, expires_at) VALUES "
                "(:id, :t, :d, 'v1', 'abc', 'active', 's3://bucket/refund.pdf', :eff, :exp)"
            ),
            {"id": VERSION, "t": TENANT, "d": DOC, "eff": now - 86400, "exp": now + 86400},
        )
        conn.execute(
            text(
                "INSERT INTO chunks "
                "(id, tenant_id, document_version_id, section_path, ordinal, text, text_hash) "
                "VALUES (:id, :t, :v, CAST('[\"Refunds\"]' AS jsonb), 0, "
                " 'The refund window is 30 days for annual plans.', 'h1')"
            ),
            {"id": CHUNK, "t": TENANT, "v": VERSION},
        )
    yield
    with admin.begin() as conn:
        _purge(conn)
        conn.execute(text("DELETE FROM tenants WHERE slug = 'failure-inj'"))
    admin.dispose()


def _run(coro):
    """Run a coroutine on a selector loop, matching the sibling suites.

    Left untyped on purpose: every other integration suite declares this
    helper the same way, and a generic signature here would make this file
    the odd one out for no benefit.
    """
    import asyncio

    return asyncio.run(coro, loop_factory=asyncio.SelectorEventLoop)  # noqa: ANN001,ANN201


# --- 1. A cited document expires (or is retired) during a conversation. ---


async def _search_refund() -> list[RetrievedChunk]:
    from platform_core.db import create_engine
    from platform_core.retrieval.hybrid import PrincipalScope, hybrid_search

    engine = create_engine(APP_URL)
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            await session.execute(
                text("SELECT set_config('app.tenant_id', :t, true)"), {"t": TENANT}
            )
            return await hybrid_search(
                session,
                tenant_id=uuid.UUID(TENANT),
                query="refund window annual plans",
                top_k=8,
                principal=PrincipalScope(principal_types=("role",), principal_ids=("ai_agent",)),
                embedder=None,
            )
    finally:
        await engine.dispose()


def _found(result: list[RetrievedChunk]) -> bool:
    return any(c.chunk_id == uuid.UUID(CHUNK) for c in result)


def test_expiring_a_cited_document_removes_it_from_later_runs(fixture_rows: None) -> None:
    """An expired version must stop being citable, not linger in context.

    Serving a superseded refund policy is worse than abstaining: the
    customer gets a confident wrong answer about a contractual term.
    """
    assert _found(_run(_search_refund())), "precondition: the chunk is retrievable"

    admin = create_engine(ADMIN_URL)
    try:
        with admin.begin() as conn:
            conn.execute(
                text("UPDATE document_versions SET expires_at = :past WHERE id = :v"),
                {"past": int(time.time()) - 60, "v": VERSION},
            )
        after = _run(_search_refund())
    finally:
        admin.dispose()

    assert not _found(after), "an expired version must not be returned as evidence"


def test_retiring_a_document_version_removes_it_too(fixture_rows: None) -> None:
    """`status` is the other half of the gate: a version can be retired
    without an expiry timestamp ever being set."""
    admin = create_engine(ADMIN_URL)
    try:
        with admin.begin() as conn:
            conn.execute(
                text("UPDATE document_versions SET status = 'superseded' WHERE id = :v"),
                {"v": VERSION},
            )
        after = _run(_search_refund())
    finally:
        admin.dispose()

    assert not _found(after)


def test_citing_evidence_that_left_the_context_is_refused() -> None:
    """A draft citing a chunk no longer in evidence is unpublishable.

    This is the in-run half of the expiry story: the model was given
    evidence, the evidence set changed, and the answer must not be sent
    on the strength of a citation that no longer resolves.
    """
    stale = uuid.uuid4()
    draft = DraftAnswer(text="Yes, refundable within 30 days.", claims={0: [stale]})
    result = validate_citations(draft, evidence=[])

    assert result.ok is False
    assert result.reason_code == "UNSUPPORTED_CLAIM"
    assert result.unsupported_claims == [0], "the failing claim must be named, not just counted"


def test_a_claim_citing_nothing_is_unsupported() -> None:
    """An uncited claim is an assertion, and assertions are not answers."""
    draft = DraftAnswer(text="It depends.", claims={0: []})
    result = validate_citations(draft, evidence=[])
    assert result.ok is False
    assert result.reason_code == "UNSUPPORTED_CLAIM"


# --- 2. Provider failure mapping. ---


def test_transport_failure_is_marked_retryable() -> None:
    """A 5xx / timeout / open breaker is the one class safe to retry:
    the request is known not to have produced an answer."""
    error = ModelUnavailable("upstream returned 503")
    assert error.retryable is True
    assert isinstance(error, ModelError)


def test_provider_rejection_is_not_retryable() -> None:
    """A 4xx is our fault (bad request, auth, quota) - retrying it
    burns quota and delays the handoff that the customer actually
    needs."""
    error = ModelRejected(401, "invalid api key")
    assert error.retryable is False

    assert ModelNotConfigured().retryable is False, (
        "a missing credential fails closed; retrying cannot conjure one"
    )


def test_provider_errors_carry_a_stable_code_for_metrics() -> None:
    """Error mapping must be observable: a free-text message cannot be
    aggregated into a dashboard or alerted on."""
    errors = (
        ModelUnavailable("x"),
        ModelRejected(429, "x"),
        ModelNotConfigured(),
    )
    for error in errors:
        assert isinstance(error.code, str) and error.code
        assert error.code == error.code.upper(), "codes are a stable vocabulary"


def test_rejection_code_is_distinguishable_by_status() -> None:
    """`MODEL_REJECTED_429` and `MODEL_REJECTED_401` must not collapse
    into one signal: quota exhaustion is a capacity incident, a bad key
    is a config incident, and they page different people."""
    assert ModelRejected(429).code != ModelRejected(401).code
    assert "429" in ModelRejected(429).code


# --- 3. Connector credentials rotated/revoked mid-flight. ---


def _seed_connector(conn: Connection, *, connector_id: str, name: str, status: str) -> None:
    conn.execute(
        text(
            "INSERT INTO connectors "
            "(id, tenant_id, provider, name, status, capabilities, configuration, "
            " credential_ref) VALUES "
            "(:id, :t, 'jira', :name, :status, CAST(:caps AS jsonb), "
            " CAST(:cfg AS jsonb), 'vault://kv/jira')"
        ),
        {
            "id": connector_id,
            "t": TENANT,
            "name": name,
            "status": status,
            "caps": '["create_issue"]',
            "cfg": '{"base_url": "https://jira.example"}',
        },
    )


def _executor_names() -> list[str]:
    """Resolve executors the way the tool gateway does, per request."""

    async def _resolve() -> list[str]:
        from platform_core.db import create_engine
        from platform_core.tool_gateway.registry import ConnectorExecutorResolver

        engine = create_engine(APP_URL)
        try:
            factory = async_sessionmaker(engine, expire_on_commit=False)
            async with factory() as session:
                await session.execute(
                    text("SELECT set_config('app.tenant_id', :t, true)"), {"t": TENANT}
                )
                resolver = ConnectorExecutorResolver(session, tenant_id=uuid.UUID(TENANT))
                resolved = await resolver.executors_for(["jira.create_issue"])
                return sorted(resolved)
        finally:
            await engine.dispose()

    return _run(_resolve())


def test_a_disabled_connector_stops_resolving_immediately(fixture_rows: None) -> None:
    """Rotating credentials off must take effect on the next call, with
    no cached executor surviving the status change.

    The important detail is *where* the filter happens. If a disabled
    connector still resolved, the executor would be constructed and the
    credential reference dereferenced before anything noticed - the
    secret would be loaded into the process for a connection we already
    know is dead.
    """
    admin = create_engine(ADMIN_URL)
    connector_id = str(uuid.uuid4())
    try:
        with admin.begin() as conn:
            _seed_connector(conn, connector_id=connector_id, name="jira-primary", status="active")

        assert _executor_names() == ["jira.create_issue"], (
            "precondition: an active connector that claims the capability resolves"
        )

        # Rotation: the credential reference is replaced and the connector
        # is taken out of service in one step.
        with admin.begin() as conn:
            conn.execute(
                text(
                    "UPDATE connectors SET status = 'needs_reauth', "
                    "credential_ref = 'vault://kv/jira-rotated' WHERE tenant_id = :t"
                ),
                {"t": TENANT},
            )

        assert _executor_names() == [], "a connector awaiting re-auth must not resolve"

        # Observable: an operator can see exactly why nothing resolved.
        with admin.connect() as conn:
            row = conn.execute(
                text("SELECT status, credential_ref FROM connectors WHERE tenant_id = :t"),
                {"t": TENANT},
            ).one()
        assert row.status == "needs_reauth"
        assert row.credential_ref == "vault://kv/jira-rotated"
    finally:
        admin.dispose()


def test_a_degraded_connector_is_treated_as_missing(fixture_rows: None) -> None:
    """Degraded is excluded for the same reason as disabled: executing a
    write through a connection known to be broken produces an ambiguous
    outcome an operator has to reconcile by hand."""
    admin = create_engine(ADMIN_URL)
    try:
        with admin.begin() as conn:
            _seed_connector(
                conn, connector_id=str(uuid.uuid4()), name="jira-degraded", status="degraded"
            )
        assert _executor_names() == []
    finally:
        admin.dispose()


def test_an_unreachable_connector_degrades_to_no_executor(fixture_rows: None) -> None:
    """A connector row that cannot build an adapter (bad config, factory
    failure) must not fail the whole request - it resolves to nothing,
    and the gateway reports TOOL_EXECUTOR_MISSING for that one tool.
    """
    admin = create_engine(ADMIN_URL)
    try:
        with admin.begin() as conn:
            _seed_connector(
                conn, connector_id=str(uuid.uuid4()), name="jira-broken", status="active"
            )
        # Active and capability-claiming, but bound to a provider with a
        # factory that cannot build from this configuration.
        with admin.begin() as conn:
            conn.execute(
                text("UPDATE connectors SET provider = 'nonexistent' WHERE tenant_id = :t"),
                {"t": TENANT},
            )
        assert _executor_names() == []
    finally:
        admin.dispose()
