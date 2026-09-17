"""Integration tests: tenant branding (Phase 5: custom domains and branding).

- any member can read the tenant's branding
- only TENANT_ADMIN can write it, and only with an Idempotency-Key
- validation refuses a non-hex colour, a non-http(s) logo URL and a bad email
- branding is tenant-scoped: one tenant never sees another's
- a write is audited
"""

import uuid

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.integration

ADMIN_URL = "postgresql+psycopg://platform:platform@localhost:5435/platform"
TENANT = "0190d000-0000-7000-8000-0000000000f1"
TENANT_OTHER = "0190d000-0000-7000-8000-0000000000f2"


def _client(tenant_id: str, role: str) -> TestClient:
    import importlib

    from fastapi import FastAPI

    from platform_core.identity.middleware import TenantContextMiddleware
    from platform_core.identity.tenant_context import TenantContext

    main_mod = importlib.import_module("platform_core.main")
    fresh = FastAPI()
    for route in main_mod.app.router.routes:
        fresh.router.routes.append(route)

    actor_id = uuid.uuid5(uuid.NAMESPACE_URL, f"actor:{tenant_id}-{role}")

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
        for tid, slug in ((TENANT, "brand-t1"), (TENANT_OTHER, "brand-t2")):
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
        conn.execute(
            text("DELETE FROM audit_events WHERE tenant_id IN (:a, :b)"),
            {"a": TENANT, "b": TENANT_OTHER},
        )
        conn.execute(text("DELETE FROM tenants WHERE slug LIKE 'brand-t%'"))
    admin.dispose()


@pytest.fixture(autouse=True)
def reset_branding():
    """Clear branding and audit rows so each test starts from the defaults."""
    from sqlalchemy import create_engine, text

    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        conn.execute(
            text(
                "UPDATE tenants SET brand_display_name = NULL, brand_logo_url = NULL, "
                "brand_primary_color = NULL, support_email = NULL WHERE id IN (:a, :b)"
            ),
            {"a": TENANT, "b": TENANT_OTHER},
        )
        conn.execute(
            text("DELETE FROM audit_events WHERE tenant_id IN (:a, :b)"),
            {"a": TENANT, "b": TENANT_OTHER},
        )
    yield
    admin.dispose()


class TestReadBranding:
    def test_any_member_can_read(self) -> None:
        resp = _client(TENANT, "support_agent").get("/v1/tenant/branding", headers=_headers())
        assert resp.status_code == 200, resp.text
        assert resp.json()["branding"]["slug"] == "brand-t1"

    def test_defaults_are_null(self) -> None:
        resp = _client(TENANT, "support_viewer").get("/v1/tenant/branding", headers=_headers())
        body = resp.json()["branding"]
        assert body["display_name"] is None
        assert body["primary_color"] is None


class TestWriteBranding:
    def test_owner_can_set_branding(self) -> None:
        client = _client(TENANT, "tenant_owner")
        resp = client.put(
            "/v1/tenant/branding",
            headers=_headers(str(uuid.uuid4())),
            json={
                "display_name": "Acme Support",
                "logo_url": "https://cdn.example.com/logo.png",
                "primary_color": "#1a2b3c",
                "support_email": "help@acme.example",
            },
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()["branding"]
        assert body["display_name"] == "Acme Support"
        assert body["primary_color"] == "#1a2b3c"

        got = client.get("/v1/tenant/branding", headers=_headers()).json()["branding"]
        assert got["logo_url"] == "https://cdn.example.com/logo.png"

    def test_non_admin_cannot_write(self) -> None:
        resp = _client(TENANT, "support_agent").put(
            "/v1/tenant/branding",
            headers=_headers(str(uuid.uuid4())),
            json={"display_name": "nope"},
        )
        assert resp.status_code == 403

    def test_write_requires_idempotency_key(self) -> None:
        resp = _client(TENANT, "tenant_owner").put(
            "/v1/tenant/branding", headers=_headers(), json={"display_name": "x"}
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"

    def test_invalid_colour_is_refused(self) -> None:
        resp = _client(TENANT, "tenant_owner").put(
            "/v1/tenant/branding",
            headers=_headers(str(uuid.uuid4())),
            json={"primary_color": "red; background:url(x)"},
        )
        assert resp.status_code == 400
        assert "primary_color" in resp.json()["error"]["message"]

    def test_javascript_logo_url_is_refused(self) -> None:
        resp = _client(TENANT, "tenant_owner").put(
            "/v1/tenant/branding",
            headers=_headers(str(uuid.uuid4())),
            json={"logo_url": "javascript:alert(1)"},
        )
        assert resp.status_code == 400
        assert "logo_url" in resp.json()["error"]["message"]

    def test_invalid_email_is_refused(self) -> None:
        resp = _client(TENANT, "tenant_owner").put(
            "/v1/tenant/branding",
            headers=_headers(str(uuid.uuid4())),
            json={"support_email": "not-an-email"},
        )
        assert resp.status_code == 400

    def test_omitted_field_clears_it(self) -> None:
        client = _client(TENANT, "tenant_owner")
        client.put(
            "/v1/tenant/branding",
            headers=_headers(str(uuid.uuid4())),
            json={"display_name": "Temp", "support_email": "a@b.example"},
        )
        resp = client.put(
            "/v1/tenant/branding",
            headers=_headers(str(uuid.uuid4())),
            json={"support_email": "a@b.example"},
        )
        assert resp.status_code == 200
        assert resp.json()["branding"]["display_name"] is None

    def test_write_is_audited(self) -> None:
        from sqlalchemy import create_engine, text

        _client(TENANT, "tenant_owner").put(
            "/v1/tenant/branding",
            headers=_headers(str(uuid.uuid4())),
            json={"display_name": "Audited"},
        )
        admin = create_engine(ADMIN_URL)
        with admin.begin() as conn:
            count = conn.execute(
                text(
                    "SELECT count(*) FROM audit_events WHERE tenant_id = :t "
                    "AND action = 'tenant.branding.updated'"
                ),
                {"t": TENANT},
            ).scalar()
        admin.dispose()
        assert count == 1


class TestIsolation:
    def test_branding_is_tenant_scoped(self) -> None:
        _client(TENANT, "tenant_owner").put(
            "/v1/tenant/branding",
            headers=_headers(str(uuid.uuid4())),
            json={"display_name": "Tenant A brand"},
        )
        other = (
            _client(TENANT_OTHER, "tenant_owner")
            .get("/v1/tenant/branding", headers=_headers())
            .json()["branding"]
        )
        assert other["display_name"] is None
        assert other["slug"] == "brand-t2"
