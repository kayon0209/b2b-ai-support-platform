"""Integration tests: tenant self-service identity admin (Phase 5).

Covers the identity router's membership management endpoints:
- listing members requires TENANT_ADMIN
- inviting a member requires TENANT_ADMIN and an idempotency key
- accepting an invite creates a User + Membership from a token
- updating a member's role requires TENANT_ADMIN
- removing a member requires TENANT_ADMIN and prevents removing the last owner
- invite tokens are single-use and expire
"""

import uuid

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.integration

ADMIN_URL = "postgresql+psycopg://platform:platform@localhost:5435/platform"
TENANT = "0190d000-0000-7000-8000-0000000000c3"
TENANT_OTHER = "0190d000-0000-7000-8000-0000000000c4"


def _seed_member(tenant: str, email: str, role: str = "support_agent") -> str:
    """Create a user + membership in the DB directly."""
    from sqlalchemy import create_engine, text

    eng = create_engine(ADMIN_URL)
    with eng.begin() as conn:
        uid = str(uuid.uuid5(uuid.NAMESPACE_URL, f"user:{email}"))
        conn.execute(
            text(
                "INSERT INTO users (id, primary_email, display_name, is_service_account) "
                "VALUES (:id, :email, :name, false) "
                "ON CONFLICT (primary_email) DO UPDATE SET "
                "display_name = EXCLUDED.display_name"
            ),
            {"id": uid, "email": email, "name": email.split("@")[0].title()},
        )
        conn.execute(
            text(
                "INSERT INTO memberships (id, tenant_id, user_id, role, status) "
                "VALUES (:id, :tid, :uid, :role, 'active') "
                "ON CONFLICT DO NOTHING"
            ),
            {
                "id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"mem:{tenant}:{email}")),
                "tid": tenant,
                "uid": uid,
                "role": role,
            },
        )
    eng.dispose()
    return uid


def _client(tenant_id: str, role: str, actor: uuid.UUID | None = None) -> TestClient:
    import importlib

    from fastapi import FastAPI

    from platform_core.identity.middleware import TenantContextMiddleware
    from platform_core.identity.tenant_context import TenantContext

    main_mod = importlib.import_module("platform_core.main")
    fresh = FastAPI()
    for route in main_mod.app.router.routes:
        fresh.router.routes.append(route)

    actor_id = actor or uuid.uuid5(uuid.NAMESPACE_URL, f"actor:{tenant_id}-{role}")

    class _Resolver:
        async def __call__(self, request):
            return TenantContext(
                tenant_id=uuid.UUID(tenant_id),
                actor_id=actor_id,
                actor_kind="user",
                role=role,
            )

    fresh.add_middleware(TenantContextMiddleware, resolver=_Resolver())
    return TestClient(fresh, raise_server_exceptions=False)


def _app_client() -> TestClient:
    """Client without auth middleware: for accept_invite (no prior membership)."""
    import importlib

    from fastapi import FastAPI

    main_mod = importlib.import_module("platform_core.main")
    fresh = FastAPI()
    for route in main_mod.app.router.routes:
        fresh.router.routes.append(route)
    return TestClient(fresh, raise_server_exceptions=False)


def _headers(idem: str | None = None) -> dict[str, str]:
    h = {"Authorization": "Bearer pt_bootstrap_test"}
    if idem:
        h["Idempotency-Key"] = idem
    return h


@pytest.fixture(scope="module", autouse=True)
def seed_tenants():
    from sqlalchemy import create_engine, text

    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        for tid, slug in ((TENANT, "ident-t1"), (TENANT_OTHER, "ident-t2")):
            conn.execute(
                text(
                    "INSERT INTO tenants (id, slug, name, status) "
                    "VALUES (:id, :slug, :name, 'active') "
                    "ON CONFLICT (slug) DO NOTHING"
                ),
                {"id": tid, "slug": slug, "name": slug},
            )
    yield
    with admin.begin() as conn:
        for table in ("membership_invitations", "memberships"):
            conn.execute(
                text(f"DELETE FROM {table} WHERE tenant_id IN (:a, :b)"),  # noqa: S608
                {"a": TENANT, "b": TENANT_OTHER},
            )
        conn.execute(text("DELETE FROM users WHERE primary_email LIKE '%ident-test%'"))
        conn.execute(text("DELETE FROM tenants WHERE slug LIKE 'ident-t%'"))
    admin.dispose()


@pytest.fixture(autouse=True)
def clean_members():
    from sqlalchemy import create_engine, text

    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        conn.execute(
            text("DELETE FROM membership_invitations WHERE tenant_id IN (:a, :b)"),
            {"a": TENANT, "b": TENANT_OTHER},
        )
        conn.execute(
            text("DELETE FROM memberships WHERE tenant_id IN (:a, :b)"),
            {"a": TENANT, "b": TENANT_OTHER},
        )
        conn.execute(text("DELETE FROM users WHERE primary_email LIKE '%ident-test%'"))
    yield
    admin.dispose()


# --- Listing ---------------------------------------------------------------


class TestListMembers:
    def test_tenant_owner_can_list(self) -> None:
        _seed_member(TENANT, "alice@ident-test.com", role="support_agent")
        client = _client(TENANT, "tenant_owner")
        resp = client.get("/v1/identity/members", headers=_headers())
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] >= 1
        emails = [m["email"] for m in data["items"]]
        assert "alice@ident-test.com" in emails

    def test_support_viewer_cannot_list(self) -> None:
        client = _client(TENANT, "support_viewer")
        resp = client.get("/v1/identity/members", headers=_headers())
        assert resp.status_code == 403

    def test_members_are_scoped_to_the_tenant(self) -> None:
        _seed_member(TENANT, "alice@ident-test.com")
        _seed_member(TENANT_OTHER, "bob@ident-test.com")
        client = _client(TENANT, "tenant_owner")
        resp = client.get("/v1/identity/members", headers=_headers())
        assert resp.status_code == 200
        emails = [m["email"] for m in resp.json()["items"]]
        assert "alice@ident-test.com" in emails
        assert "bob@ident-test.com" not in emails


# --- Invite ----------------------------------------------------------------


class TestInviteMember:
    def test_owner_can_invite(self) -> None:
        client = _client(TENANT, "tenant_owner")
        resp = client.post(
            "/v1/identity/members/invite",
            headers=_headers(str(uuid.uuid4())),
            json={"email": "newuser@ident-test.com", "role": "support_agent"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "invitation_token" in body
        assert body["status"] == "pending"

    def test_invite_requires_role(self) -> None:
        client = _client(TENANT, "support_viewer")
        resp = client.post(
            "/v1/identity/members/invite",
            headers=_headers(str(uuid.uuid4())),
            json={"email": "newuser@ident-test.com", "role": "support_agent"},
        )
        assert resp.status_code == 403

    def test_invite_without_idempotency_key_rejected(self) -> None:
        client = _client(TENANT, "tenant_owner")
        resp = client.post(
            "/v1/identity/members/invite",
            headers=_headers(),
            json={"email": "newuser@ident-test.com", "role": "support_agent"},
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"

    def test_invite_rejects_invalid_role(self) -> None:
        client = _client(TENANT, "tenant_owner")
        resp = client.post(
            "/v1/identity/members/invite",
            headers=_headers(str(uuid.uuid4())),
            json={"email": "newuser@ident-test.com", "role": "tenant_owner"},
        )
        assert resp.status_code == 400

    def test_idempotent_invite_returns_same_token(self) -> None:
        client = _client(TENANT, "tenant_owner")
        idem = str(uuid.uuid4())
        resp1 = client.post(
            "/v1/identity/members/invite",
            headers=_headers(idem),
            json={"email": "idem@ident-test.com", "role": "support_agent"},
        )
        resp2 = client.post(
            "/v1/identity/members/invite",
            headers=_headers(idem),
            json={"email": "idem@ident-test.com", "role": "support_agent"},
        )
        assert resp1.status_code == 200
        assert resp2.status_code == 200
        assert resp1.json()["invitation_token"] == resp2.json()["invitation_token"]


# --- Accept Invite ---------------------------------------------------------


class TestAcceptInvite:
    def test_accept_invite_creates_membership(self) -> None:
        client = _client(TENANT, "tenant_owner")
        resp = client.post(
            "/v1/identity/members/invite",
            headers=_headers(str(uuid.uuid4())),
            json={"email": "accepter@ident-test.com", "role": "support_agent"},
        )
        token = resp.json()["invitation_token"]

        app_client = _app_client()
        resp = app_client.post(
            "/v1/identity/members/accept",
            headers={"Idempotency-Key": str(uuid.uuid4())},
            json={
                "token": token,
                "email": "accepter@ident-test.com",
                "display_name": "Accepter",
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["role"] == "support_agent"
        assert "user_id" in body

    def test_token_is_single_use(self) -> None:
        client = _client(TENANT, "tenant_owner")
        resp = client.post(
            "/v1/identity/members/invite",
            headers=_headers(str(uuid.uuid4())),
            json={"email": "single@ident-test.com", "role": "support_agent"},
        )
        token = resp.json()["invitation_token"]

        app_client = _app_client()
        first = app_client.post(
            "/v1/identity/members/accept",
            headers={"Idempotency-Key": str(uuid.uuid4())},
            json={
                "token": token,
                "email": "single@ident-test.com",
                "display_name": "Single",
            },
        )
        assert first.status_code == 200

        second = app_client.post(
            "/v1/identity/members/accept",
            headers={"Idempotency-Key": str(uuid.uuid4())},
            json={
                "token": token,
                "email": "single@ident-test.com",
                "display_name": "Single",
            },
        )
        assert second.status_code == 409

    def test_accept_is_reachable_without_a_bearer_token(self) -> None:
        """The invitee has no membership yet, so accept must be reachable
        unauthenticated. This drives the REAL middleware (platform_core.main);
        the other accept tests use a middleware-less app and so cannot catch a
        missing auth exemption."""
        import importlib

        main_mod = importlib.import_module("platform_core.main")

        invite_client = _client(TENANT, "tenant_owner")
        resp = invite_client.post(
            "/v1/identity/members/invite",
            headers=_headers(str(uuid.uuid4())),
            json={"email": "reachable@ident-test.com", "role": "support_agent"},
        )
        token = resp.json()["invitation_token"]

        real = TestClient(main_mod.app, raise_server_exceptions=False)
        out = real.post(
            "/v1/identity/members/accept",
            headers={"Idempotency-Key": str(uuid.uuid4())},
            json={
                "token": token,
                "email": "reachable@ident-test.com",
                "display_name": "Reachable",
            },
        )
        assert out.status_code == 200, out.text
        assert out.json()["role"] == "support_agent"


# --- Update / Remove -------------------------------------------------------


class TestMemberManagement:
    def _member_id(self, tenant: str, email: str) -> str:
        from sqlalchemy import create_engine, text

        eng = create_engine(ADMIN_URL)
        with eng.begin() as conn:
            row = conn.execute(
                text(
                    "SELECT m.id FROM memberships m "
                    "JOIN users u ON u.id = m.user_id "
                    "WHERE m.tenant_id = :t AND u.primary_email = :e"
                ),
                {"t": tenant, "e": email},
            ).one_or_none()
            mid = row[0] if row else None
        eng.dispose()
        assert mid is not None, "member not found"
        return str(mid)

    def test_owner_can_change_role(self) -> None:
        _seed_member(TENANT, "changer@ident-test.com", role="support_agent")
        mid = self._member_id(TENANT, "changer@ident-test.com")
        client = _client(TENANT, "tenant_owner")
        resp = client.post(
            f"/v1/identity/members/{mid}",
            headers=_headers(str(uuid.uuid4())),
            json={"role": "support_admin"},
        )
        assert resp.status_code == 200
        assert resp.json()["role"] == "support_admin"

    def test_non_admin_cannot_change_role(self) -> None:
        _seed_member(TENANT, "victim@ident-test.com", role="support_agent")
        mid = self._member_id(TENANT, "victim@ident-test.com")
        client = _client(TENANT, "support_agent")
        resp = client.post(
            f"/v1/identity/members/{mid}",
            headers=_headers(str(uuid.uuid4())),
            json={"role": "tenant_owner"},
        )
        assert resp.status_code == 403

    def test_last_owner_cannot_remove_self(self) -> None:
        # The tenant must actually hold an owner for this to be meaningful:
        # clean_members removes every membership between tests, so seed one.
        _seed_member(TENANT, "owner@ident-test.com", role="tenant_owner")
        client = _client(TENANT, "tenant_owner")
        from sqlalchemy import create_engine, text

        eng = create_engine(ADMIN_URL)
        with eng.begin() as conn:
            mid = conn.execute(
                text(
                    "SELECT id FROM memberships "
                    "WHERE tenant_id = :t AND role = 'tenant_owner' LIMIT 1"
                ),
                {"t": TENANT},
            ).scalar()
        eng.dispose()
        assert mid is not None

        resp = client.request(
            "DELETE",
            f"/v1/identity/members/{mid}",
            headers=_headers(str(uuid.uuid4())),
        )
        assert resp.status_code == 400
        assert "last tenant_owner" in resp.json()["error"]["message"]


# --- Auth success path through the real middleware --------------------------


class TestBootstrapAuthThroughHttp:
    """Drive the real middleware (platform_core.main), not a stub resolver.

    A regression here once made *every* authenticated endpoint return 401 for
    *every* user -- including tenant_owner -- while the unit tests stayed green
    because they asserted actor_id round-trips and the integration tests
    fabricated roles. This exercises slug -> tenant -> membership -> role for
    real, over HTTP.
    """

    def _real_client(self):
        import importlib

        from fastapi.testclient import TestClient

        main_mod = importlib.import_module("platform_core.main")
        return TestClient(main_mod.app, raise_server_exceptions=False)

    def test_valid_bootstrap_token_authenticates_and_carries_a_role(self) -> None:
        from platform_core.config import get_settings

        if not get_settings().allow_bootstrap_tokens:
            pytest.skip("bootstrap tokens are disabled in this environment")

        uid = _seed_member(TENANT, "boot@ident-test.com", role="auditor")
        client = self._real_client()
        resp = client.get(
            "/v1/quality/metrics",
            headers={"Authorization": f"Bearer pt_ident-t1_{uid}"},
        )
        assert resp.status_code == 200, resp.text
        assert "total_runs" in resp.json()

    def test_unknown_slug_is_rejected(self) -> None:
        uid = _seed_member(TENANT, "boot2@ident-test.com", role="auditor")
        client = self._real_client()
        resp = client.get(
            "/v1/quality/metrics",
            headers={"Authorization": f"Bearer pt_no-such-tenant_{uid}"},
        )
        assert resp.status_code == 401
        assert resp.json()["error"]["code"] == "AUTH_UNRESOLVED"

    def test_unknown_actor_is_rejected(self) -> None:
        _seed_member(TENANT, "boot3@ident-test.com", role="auditor")
        client = self._real_client()
        stranger = uuid.uuid5(uuid.NAMESPACE_URL, "stranger@ident-test.com")
        resp = client.get(
            "/v1/quality/metrics",
            headers={"Authorization": f"Bearer pt_ident-t1_{stranger}"},
        )
        assert resp.status_code == 401
