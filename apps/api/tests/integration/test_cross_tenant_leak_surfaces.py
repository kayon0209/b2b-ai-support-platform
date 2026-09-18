"""App-layer cross-tenant negative suite (ticket 23, docs/testing-and-evaluation.md).

`test_cross_tenant_negative.py` proves the database refuses to cross tenant
boundaries. This file proves the *application* refuses too, which is a
different question: RLS is defence in depth, and every one of these tests
would still fail if a handler forgot a tenant predicate but happened to run
with a session whose `app.tenant_id` was set to the caller's tenant. They
exist because the leak surfaces named in the ticket are HTTP-shaped:

- guessing another tenant's resource ids (404, never 403-with-detail);
- reading another tenant's retrieval evidence, including `source_uri`;
- reading another tenant's audit trail;
- reading another tenant's quality metrics and dashboard data;
- reading another tenant's cases and SLA state;
- reading another tenant's external resource mappings.

The invariant under test is the same everywhere: **tenant B is
indistinguishable from a tenant that has no data at all.** A 404 for a real
but foreign id, and never a body that reveals the row exists. That matters
because "resource exists but you cannot see it" is itself a disclosure - it
lets an attacker enumerate ids (`docs/security.md`).

Deliberately not here: the RLS sweep itself (see the sibling file) and
cross-tenant *writes*, which the tool-gateway confirmation tests cover.
"""

import importlib
import os
import uuid
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from platform_core.identity.middleware import TenantContextMiddleware
from platform_core.identity.tenant_context import TenantContext

pytestmark = pytest.mark.integration

ADMIN_URL = os.environ.get(
    "APP_ADMIN_DATABASE_URL",
    "postgresql+psycopg://platform:platform@localhost:5435/platform",
)

TENANT_A = "01900000-0000-7000-8000-0000000000d1"
TENANT_B = "01900000-0000-7000-8000-0000000000d2"
SLUG_A = "leak-a"
SLUG_B = "leak-b"

# Ids owned by tenant A. Tenant B will guess every one of them.
A = {
    "space": "01900000-0000-7000-8000-0000000000e1",
    "source": "01900000-0000-7000-8000-0000000000e2",
    "doc": "01900000-0000-7000-8000-0000000000e3",
    "ver": "01900000-0000-7000-8000-0000000000e4",
    "chunk": "01900000-0000-7000-8000-0000000000e5",
    "case": "01900000-0000-7000-8000-0000000000e6",
    "run": "01900000-0000-7000-8000-0000000000e7",
}

# A marker that must never appear in a response served to tenant B.
SECRET_MARKER = "tenant-a-confidential-refund-cap-30-percent"


def _run(coro: Any) -> Any:
    import asyncio

    return asyncio.run(coro, loop_factory=asyncio.SelectorEventLoop)


def _cleanup(admin: Any) -> None:
    with admin.begin() as conn:
        conn.execute(
            text(
                "DELETE FROM citations WHERE tenant_id IN (:a, :b) "
                "OR agent_run_id IN (SELECT id FROM agent_runs WHERE tenant_id IN (:a, :b))"
            ),
            {"a": TENANT_A, "b": TENANT_B},
        )
        for table in (
            "audit_events",
            "cases",
            "agent_runs",
            "knowledge_acls",
            "chunks",
            "document_versions",
            "documents",
            "knowledge_sources",
            "knowledge_spaces",
            "external_resource_refs",
        ):
            conn.execute(
                text(f"DELETE FROM {table} WHERE tenant_id IN (:a, :b)"),  # noqa: S608
                {"a": TENANT_A, "b": TENANT_B},
            )
        conn.execute(text("DELETE FROM tenants WHERE slug IN (:a, :b)"), {"a": SLUG_A, "b": SLUG_B})


@pytest.fixture(scope="module", autouse=True)
def seed_tenant_a_only() -> Any:
    """Tenant A gets real rows on every surface; tenant B gets nothing.

    Seeding only one side is the point: every assertion below is then
    "B sees what an empty tenant sees", with no ambiguity about whether a
    non-empty result came from B's own data.
    """
    admin = create_engine(ADMIN_URL)
    _cleanup(admin)
    with admin.begin() as conn:
        for tid, slug in ((TENANT_A, SLUG_A), (TENANT_B, SLUG_B)):
            conn.execute(
                text(
                    "INSERT INTO tenants (id, slug, name, status) VALUES "
                    "(:id, :slug, 'Leak', 'active') ON CONFLICT (slug) DO NOTHING"
                ),
                {"id": tid, "slug": slug},
            )

        conn.execute(
            text(
                "INSERT INTO knowledge_spaces (id, tenant_id, name, status) "
                "VALUES (:id, :t, 'A Space', 'active')"
            ),
            {"id": A["space"], "t": TENANT_A},
        )
        conn.execute(
            text(
                "INSERT INTO knowledge_sources (id, tenant_id, space_id, type, name, status) "
                "VALUES (:id, :t, :space, 'upload', 'A Source', 'active')"
            ),
            {"id": A["source"], "t": TENANT_A, "space": A["space"]},
        )
        conn.execute(
            text(
                "INSERT INTO documents (id, tenant_id, space_id, source_id, canonical_uri, "
                "title, classification) VALUES "
                "(:id, :t, :space, :src, 'upload://leak/a-secret.pdf', "
                "'A Document', 'internal')"
            ),
            {"id": A["doc"], "t": TENANT_A, "space": A["space"], "src": A["source"]},
        )
        conn.execute(
            text(
                "INSERT INTO document_versions (id, tenant_id, document_id, version_label, "
                "content_hash, effective_at, expires_at, status, object_uri) VALUES "
                "(:id, :t, :doc, 'v1', 'h1', "
                "extract(epoch from now())::bigint, "
                "(extract(epoch from now())::bigint + 86400), 'active', "
                "'minio://leak/a-secret.pdf')"
            ),
            {"id": A["ver"], "t": TENANT_A, "doc": A["doc"]},
        )
        conn.execute(
            text(
                "INSERT INTO chunks (id, tenant_id, document_version_id, ordinal, "
                "text_hash, text, embedding) VALUES "
                "(:id, :t, :ver, 0, 'h2', :excerpt, NULL)"
            ),
            {"id": A["chunk"], "t": TENANT_A, "ver": A["ver"], "excerpt": SECRET_MARKER},
        )
        conn.execute(
            text(
                "INSERT INTO cases (id, tenant_id, subject, description, category, "
                "status, priority, opened_at, first_response_due_at, resolution_due_at) VALUES "
                "(:id, :t, 'A case', :desc, 'general', 'new', 'p2', 1000, 2000, 3000)"
            ),
            {"id": A["case"], "t": TENANT_A, "desc": SECRET_MARKER},
        )
        conn.execute(
            text(
                "INSERT INTO agent_runs (id, tenant_id, conversation_ref_id, route, "
                "status, input_hash) VALUES "
                "(:id, :t, :conv, 'knowledge_qa', 'completed', :hash)"
            ),
            {"id": A["run"], "t": TENANT_A, "conv": str(uuid.uuid4()), "hash": SECRET_MARKER},
        )
        conn.execute(
            text(
                "INSERT INTO audit_events (id, tenant_id, occurred_at, actor_type, action, "
                "resource_type, resource_id, decision, reason_code, trace_id, after_hash) VALUES "
                "(gen_random_uuid(), :t, 1000, 'user', 'document.published', "
                "'document', :res, 'allow', 'ok', 'trace-leak', :marker)"
            ),
            {"t": TENANT_A, "res": A["doc"], "marker": SECRET_MARKER},
        )
        conn.execute(
            text(
                "INSERT INTO external_resource_refs (id, tenant_id, system, "
                "resource_type, external_id) VALUES "
                "(gen_random_uuid(), :t, 'chatwoot', 'account', 'leak-ref-999')"
            ),
            {"t": TENANT_A},
        )
    yield
    _cleanup(admin)
    admin.dispose()


class _RoleResolver:
    """Fabricate a TenantContext for a fixed tenant and role (see m2 suite)."""

    def __init__(self, tenant_id: str, role: str | None) -> None:
        self._tenant_id = tenant_id
        self._role = role
        self._actor = uuid.uuid5(uuid.NAMESPACE_URL, f"actor:{tenant_id}-{role}")

    async def __call__(self, request: object) -> TenantContext:
        return TenantContext(
            tenant_id=uuid.UUID(self._tenant_id),
            actor_id=self._actor,
            actor_kind="user",
            role=self._role,
        )


def _client(tenant_id: str, role: str | None) -> TestClient:
    main_mod = importlib.import_module("platform_core.main")
    fresh = FastAPI()
    for route in main_mod.app.router.routes:
        fresh.router.routes.append(route)
    fresh.add_middleware(TenantContextMiddleware, resolver=_RoleResolver(tenant_id, role))
    return TestClient(fresh, raise_server_exceptions=False)


def _auth() -> dict[str, str]:
    return {"Authorization": "Bearer pt_bootstrap_test"}


def _body_leaks(resp: Any) -> bool:
    """True when a response exposes tenant A's confidential text."""
    return SECRET_MARKER in resp.text


# --- 1. Guessed resource ids -------------------------------------------------


@pytest.mark.zero_tolerance("cross_tenant_violations")
def test_guessed_case_id_is_not_found_not_forbidden() -> None:
    """404, not 403: a 403 would confirm the case exists elsewhere."""
    resp = _client(TENANT_B, "support_agent").get(f"/v1/cases/{A['case']}", headers=_auth())
    assert resp.status_code == 404, f"unexpected {resp.status_code}: {resp.text[:200]}"
    assert not _body_leaks(resp)


@pytest.mark.zero_tolerance("cross_tenant_violations")
def test_guessed_case_id_with_a_privileged_role_is_still_not_found() -> None:
    """A stronger role must not turn 'invisible' into 'visible'.

    `support_admin` and `auditor` hold broader read rights inside their own
    tenant; that is not a licence to read anyone else's.
    """
    for role in ("support_admin", "auditor", "security_admin"):
        resp = _client(TENANT_B, role).get(f"/v1/cases/{A['case']}", headers=_auth())
        assert resp.status_code == 404, f"{role} got {resp.status_code}: {resp.text[:200]}"
        assert not _body_leaks(resp)


@pytest.mark.zero_tolerance("cross_tenant_violations")
def test_case_list_is_empty_for_a_tenant_with_no_cases() -> None:
    resp = _client(TENANT_B, "support_agent").get("/v1/cases", headers=_auth())
    assert resp.status_code == 200
    assert not _body_leaks(resp)
    payload = resp.json()
    items = payload.get("items", payload if isinstance(payload, list) else [])
    assert len(items) == 0, f"tenant B saw {len(items)} of tenant A's cases"


# --- 2. Retrieval evidence and source_uri ------------------------------------


@pytest.mark.zero_tolerance("cross_tenant_violations")
def test_retrieval_never_returns_another_tenants_evidence() -> None:
    """The highest-value leak: a question about A's confidential topic asked
    by B must retrieve nothing, and must never reveal A's document URI."""
    resp = _client(TENANT_B, "support_agent").post(
        "/v1/retrieval/query",
        json={"query": SECRET_MARKER, "knowledge_space_ids": [A["space"]], "limit": 10},
        headers=_auth(),
    )
    assert resp.status_code in (200, 403), f"unexpected {resp.status_code}: {resp.text[:200]}"
    assert not _body_leaks(resp)
    assert "minio://leak" not in resp.text, "tenant A's storage URI leaked"
    if resp.status_code == 200:
        payload = resp.json()
        items = payload.get("items", payload.get("chunks", []))
        assert len(items) == 0, "retrieval returned another tenant's evidence"


@pytest.mark.zero_tolerance("cross_tenant_violations")
def test_retrieval_of_a_named_document_does_not_cross_tenants() -> None:
    resp = _client(TENANT_B, "support_agent").post(
        "/v1/retrieval/query",
        json={"query": "A Document", "knowledge_space_ids": [A["space"]], "limit": 10},
        headers=_auth(),
    )
    assert "minio://leak" not in resp.text
    assert "A Document" not in resp.text


# --- 3. Audit trail ----------------------------------------------------------


@pytest.mark.zero_tolerance("cross_tenant_violations")
def test_audit_events_do_not_expose_another_tenants_records() -> None:
    resp = _client(TENANT_B, "auditor").get("/v1/audit-events", headers=_auth())
    assert resp.status_code == 200, f"unexpected {resp.status_code}: {resp.text[:200]}"
    assert not _body_leaks(resp)
    payload = resp.json()
    items = payload.get("items", payload if isinstance(payload, list) else [])
    assert len(items) == 0, "tenant B read tenant A's audit trail"


def test_audit_access_requires_the_audit_role() -> None:
    """Widening read rights must not come from widening the audience.

    The status code is the assertion that matters here: the router used to
    answer a denial with 200 plus an error body, so any client branching on
    the transport read "denied" as "here is the audit trail".
    """
    resp = _client(TENANT_B, "support_agent").get("/v1/audit-events", headers=_auth())
    assert resp.status_code == 403, f"unexpected {resp.status_code}: {resp.text[:200]}"
    assert resp.json()["error"]["code"] == "AUDIT_ACCESS_DENIED"


# --- 4. Quality metrics and dashboards --------------------------------------


@pytest.mark.zero_tolerance("cross_tenant_violations")
def test_quality_metrics_are_scoped_to_the_calling_tenant() -> None:
    resp = _client(TENANT_B, "security_admin").get("/v1/quality/metrics", headers=_auth())
    assert resp.status_code in (200, 403), f"unexpected {resp.status_code}: {resp.text[:200]}"
    assert not _body_leaks(resp)


@pytest.mark.zero_tolerance("cross_tenant_violations")
def test_quality_routes_are_scoped_to_the_calling_tenant() -> None:
    resp = _client(TENANT_B, "security_admin").get("/v1/quality/routes", headers=_auth())
    assert resp.status_code in (200, 403), f"unexpected {resp.status_code}: {resp.text[:200]}"
    assert not _body_leaks(resp)


# --- 5. External resource mappings ------------------------------------------


@pytest.mark.zero_tolerance("cross_tenant_violations")
def test_another_tenants_external_id_cannot_be_resolved() -> None:
    """External ids are attacker-guessable (sequential Chatwoot ids), so the
    mapping lookup must be tenant-filtered, not merely authenticated.

    `resolve_tenant_from_account` is the inbound webhook's tenant resolver:
    a guessed account id that belongs to tenant A must not resolve while the
    session is scoped to tenant B, or a forged webhook could be attributed
    to the wrong tenant.
    """
    from platform_core.support_bridge.mapping import resolve_tenant_from_account

    async def probe() -> Any:
        from sqlalchemy.ext.asyncio import async_sessionmaker

        from platform_core.db import create_engine

        engine = create_engine(
            "postgresql+psycopg://platform_app:platform_app@localhost:5435/platform"
        )
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            await session.execute(
                text("SELECT set_config('app.tenant_id', :t, true)"), {"t": TENANT_B}
            )
            found = await resolve_tenant_from_account(session, "leak-ref-999")
            await session.rollback()
        await engine.dispose()
        return found

    assert _run(probe()) is None, "tenant B resolved tenant A's external account reference"
