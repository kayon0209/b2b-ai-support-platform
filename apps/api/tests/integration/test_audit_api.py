"""Integration tests: audit management API (ticket 26).

Verifies the two-layer access model:
- Policy gate: only auditor/security_admin/tenant_owner roles read audit.
- RLS gate: results are bounded to the caller's tenant regardless of role.
"""

import os

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from platform_core.identity.tenant_context import TenantContext

pytestmark = pytest.mark.integration

ADMIN_URL = os.environ.get(
    "APP_ADMIN_DATABASE_URL",
    "postgresql+psycopg://platform:platform@localhost:5435/platform",
)
TENANT_A = "01900000-0000-7000-8000-000000000001"
TENANT_B = "01900000-0000-7000-8000-000000000002"

# Real users + memberships for the tests that go through the bootstrap token
# resolver. The role lives in the database, not in the token, so a test that
# wants "a viewer" has to authenticate as a user whose row says viewer.
_SEEDED = (
    ("aud-a", "viewer", "01900000-0000-7000-8000-0000000a0010", "support_viewer"),
    ("aud-a", "auditor", "01900000-0000-7000-8000-0000000a0011", "auditor"),
    ("aud-a", "agent", "01900000-0000-7000-8000-0000000a0012", "support_agent"),
    ("aud-a", "security", "01900000-0000-7000-8000-0000000a0013", "security_admin"),
    ("aud-b", "auditor", "01900000-0000-7000-8000-0000000b0011", "auditor"),
)
SEEDED_USERS: dict[tuple[str, str], str] = {(s, h): u for s, h, u, _ in _SEEDED}


@pytest.fixture(scope="module", autouse=True)
def seed_audits() -> None:
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        for tid, slug in ((TENANT_A, "aud-a"), (TENANT_B, "aud-b")):
            conn.execute(
                text(
                    "INSERT INTO tenants (id, slug, name, status) VALUES "
                    "(:id, :slug, 'Aud', 'active') ON CONFLICT (slug) DO NOTHING"
                ),
                {"id": tid, "slug": slug},
            )
            for i in range(3):
                conn.execute(
                    text(
                        "INSERT INTO audit_events (id, tenant_id, occurred_at, actor_type, "
                        "action, resource_type, decision, reason_code, trace_id) VALUES "
                        "(gen_random_uuid(), :tid, 1000 + :i, 'user', 'case.create', "
                        "'case', 'completed', 'OK', 'tr-aud')"
                    ),
                    {"tid": tid, "i": i},
                )
        for slug, hint, uid, role in _SEEDED:
            tid = TENANT_A if slug == "aud-a" else TENANT_B
            conn.execute(
                text(
                    "INSERT INTO users (id, primary_email, display_name, is_service_account) "
                    "VALUES (:id, :email, 'Aud User', false) "
                    "ON CONFLICT (primary_email) DO NOTHING"
                ),
                {"id": uid, "email": f"{slug}-{hint}@example.com"},
            )
            conn.execute(
                text(
                    "INSERT INTO memberships (id, tenant_id, user_id, role, status) "
                    "VALUES (gen_random_uuid(), :tid, :uid, :role, 'active') "
                    "ON CONFLICT (tenant_id, user_id) DO NOTHING"
                ),
                {"tid": tid, "uid": uid, "role": role},
            )
    yield
    with admin.begin() as conn:
        for tid in (TENANT_A, TENANT_B):
            conn.execute(text("DELETE FROM audit_events WHERE tenant_id = :t"), {"t": tid})
            conn.execute(text("DELETE FROM memberships WHERE tenant_id = :t"), {"t": tid})
        for slug, hint, _uid, _role in _SEEDED:
            conn.execute(
                text("DELETE FROM users WHERE primary_email = :e"),
                {"e": f"{slug}-{hint}@example.com"},
            )
        conn.execute(text("DELETE FROM tenants WHERE slug IN ('aud-a','aud-b')"))
    admin.dispose()


@pytest.fixture
def client() -> TestClient:
    from platform_core.identity.middleware import (
        TenantContextMiddleware,
        bootstrap_token_resolver,
    )
    from platform_core.main import app

    # Audit router is already included; middleware resolves bootstrap tokens.
    app.add_middleware(TenantContextMiddleware, resolver=bootstrap_token_resolver)
    return TestClient(app, raise_server_exceptions=False)


def _token(slug: str, role_hint: str = "") -> dict[str, str]:
    """Build a bootstrap token for a real membership seeded by this module.

    The token names a slug and a user id; the role is *not* in the token -
    the resolver reads it from `memberships`. `role_hint` therefore selects
    which seeded user to authenticate as, it is not a claim the server
    honours. `test_audit_api_denies_without_role` needs a real row to exist,
    so a membership is seeded below for it.
    """
    user = SEEDED_USERS.get((slug, role_hint)) or SEEDED_USERS.get((slug, ""))
    assert user is not None, f"no seeded user for slug={slug!r} hint={role_hint!r}"
    return {"Authorization": f"Bearer pt_{slug}_{user}"}


def test_audit_api_denies_without_role(client: TestClient) -> None:
    """A real, successfully-authenticated viewer is still denied audit read.

    This is the policy gate under test, not the auth path: the token resolves
    to a real membership (role `support_viewer`), so a 403 here proves the
    router's role check is what stops the request. Previously this test used
    a slug/user pair with no membership row at all and asserted 403 - which
    stopped being true once resolution was fixed to actually read the
    database, because an unresolvable token is a 401, before any policy runs.
    """
    resp = client.get("/v1/audit-events", headers=_token("aud-a", "viewer"))
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "AUDIT_ACCESS_DENIED"


class _RoleResolver:
    """Test resolver that fabricates a TenantContext with a fixed role."""

    def __init__(self, slug: str, role: str) -> None:
        # slug maps to the seeded tenant constant; keep real UUIDs.
        self._tenant_id = TENANT_A if slug == "aud-a" else TENANT_B
        self._role = role

    async def __call__(self, request: object) -> TenantContext:
        import uuid as _uuid

        return TenantContext(
            tenant_id=_uuid.UUID(self._tenant_id),
            actor_id=_uuid.uuid5(_uuid.NAMESPACE_URL, f"user:{self._tenant_id}-auditor"),
            actor_kind="user",
            role=self._role,
        )


def _client_with_role(role: str, slug: str) -> TestClient:
    # Building a new FastAPI instance avoids "middleware after startup";
    # import the module and create a fresh app with the same routers.
    import importlib

    from fastapi import FastAPI

    from platform_core.identity.middleware import TenantContextMiddleware

    main_mod = importlib.import_module("platform_core.main")
    fresh = FastAPI()
    for route in main_mod.app.router.routes:
        fresh.router.routes.append(route)
    fresh.add_middleware(TenantContextMiddleware, resolver=_RoleResolver(slug, role))
    return TestClient(fresh, raise_server_exceptions=True)


def test_auditor_reads_own_tenant_only() -> None:
    client = _client_with_role("auditor", "aud-a")
    resp = client.get("/v1/audit-events", headers=_token("aud-a", "auditor"))
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 3  # only tenant A rows, never tenant B
    assert all(item["trace_id"] == "tr-aud" for item in body["items"])


def test_support_agent_denied_audit_read() -> None:
    client = _client_with_role("support_agent", "aud-a")
    resp = client.get("/v1/audit-events", headers=_token("aud-a", "agent"))
    assert resp.json()["error"]["code"] == "AUDIT_ACCESS_DENIED"


def test_action_filter_narrows_results() -> None:
    client = _client_with_role("security_admin", "aud-a")
    resp = client.get(
        "/v1/audit-events",
        headers=_token("aud-a", "security"),
        params={"action": "case.create", "limit": 2},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 3
    assert len(body["items"]) == 2  # limit honored
    assert all(i["action"] == "case.create" for i in body["items"])
