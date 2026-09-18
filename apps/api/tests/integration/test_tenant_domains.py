"""Integration tests: tenant custom domains and Host -> tenant branding.

The security properties this pins, and why each needs a test rather than a
comment:

- an **unverified** claim does not resolve. A Host header is caller-controlled,
  so serving an unverified claim would let a tenant publish their branding on a
  domain they do not own;
- domain uniqueness is **global**, so two tenants cannot describe the same host
  and make resolution depend on row order;
- the public endpoint returns **no tenant id**, because the caller is anonymous
  and an id is a stable handle for probing the API;
- the tenant comes from the **resolved row**, never from anything the caller
  sent.
"""

import asyncio
import os
import uuid

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

TENANT = "0190d000-0000-7000-8000-0000000000d4"
TENANT_OTHER = "0190d000-0000-7000-8000-0000000000d5"

# `.test` / `.invalid` / `.local` are refused by validate_domain, and no DNS
# lookup happens (verification is an operator attestation), so a normal-looking
# host is what these tests need.
DOMAIN = "support.acme-demo.example.com"

_CLEAN: tuple[str, ...] = (
    "DELETE FROM tenant_domains WHERE tenant_id IN (:a, :b)",
    "DELETE FROM audit_events WHERE tenant_id IN (:a, :b)",
)


def _clean(conn) -> None:
    for stmt in _CLEAN:
        conn.execute(text(stmt), {"a": TENANT, "b": TENANT_OTHER})


class _RoleResolver:
    def __init__(self, tenant_id: str, role: str) -> None:
        self._tenant_id = tenant_id
        self._role = role

    async def __call__(self, request: object) -> TenantContext:
        return TenantContext(
            tenant_id=uuid.UUID(self._tenant_id),
            actor_id=uuid.uuid5(uuid.NAMESPACE_URL, f"actor:{self._tenant_id}-{self._role}"),
            actor_kind="user",
            role=self._role,
        )


def _client(tenant_id: str, role: str) -> TestClient:
    import importlib

    main_mod = importlib.import_module("platform_core.main")
    fresh = FastAPI()
    for route in main_mod.app.router.routes:
        fresh.router.routes.append(route)
    fresh.add_middleware(TenantContextMiddleware, resolver=_RoleResolver(tenant_id, role))
    return TestClient(fresh, raise_server_exceptions=False)


def _real_client() -> TestClient:
    """The assembled app: the public route must be reachable without a token."""
    import importlib

    main_mod = importlib.import_module("platform_core.main")
    return TestClient(main_mod.app, raise_server_exceptions=False)


def _headers() -> dict[str, str]:
    return {
        "Authorization": "Bearer pt_bootstrap_test",
        "Idempotency-Key": str(uuid.uuid4()),
    }


def _run(coro):
    return asyncio.run(coro, loop_factory=asyncio.SelectorEventLoop)


@pytest.fixture(scope="module", autouse=True)
def seed_tenants():
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        for tid, slug in ((TENANT, "dom-t1"), (TENANT_OTHER, "dom-t2")):
            conn.execute(
                text(
                    # DO UPDATE, not DO NOTHING: a tenant left behind by an
                    # earlier run would otherwise keep its old branding, and
                    # the assertion below would compare against a value this
                    # fixture never set - a failure that looks like a bug in
                    # the endpoint.
                    "INSERT INTO tenants (id, slug, name, status, brand_display_name, "
                    "brand_primary_color, support_email) VALUES "
                    "(:id, :slug, :name, 'active', :brand, '#1a2b3c', :email) "
                    "ON CONFLICT (slug) DO UPDATE SET "
                    "brand_display_name = EXCLUDED.brand_display_name, "
                    "brand_primary_color = EXCLUDED.brand_primary_color, "
                    "support_email = EXCLUDED.support_email, "
                    "status = 'active'"
                ),
                {
                    "id": tid,
                    "slug": slug,
                    "name": slug,
                    "brand": f"Brand {slug}",
                    "email": f"help@{slug}.example",
                },
            )
    yield
    with admin.begin() as conn:
        _clean(conn)
        conn.execute(text("DELETE FROM tenants WHERE slug LIKE 'dom-t%'"))
    admin.dispose()


@pytest.fixture(autouse=True)
def clean_domains():
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        _clean(conn)
    yield
    with admin.begin() as conn:
        _clean(conn)
    admin.dispose()


def _claim(domain: str = DOMAIN, tenant: str = TENANT, role: str = "tenant_owner"):
    return _client(tenant, role).post(
        "/v1/tenant/domains", headers=_headers(), json={"domain": domain}
    )


def _verify(domain_id: str, tenant: str = TENANT) -> object:
    return _client(tenant, "tenant_owner").post(
        f"/v1/tenant/domains/{domain_id}/verify", headers=_headers()
    )


def _public_branding(host: str):
    return _real_client().get("/v1/public/branding", headers={"host": host})


# --- claiming -------------------------------------------------------------


def test_claiming_requires_tenant_admin() -> None:
    resp = _claim(role="support_admin")
    assert resp.status_code == 403, resp.text
    assert resp.json()["error"]["code"] == "POLICY_DENIED"


def test_claiming_requires_an_idempotency_key() -> None:
    resp = _client(TENANT, "tenant_owner").post(
        "/v1/tenant/domains",
        headers={"Authorization": "Bearer pt_bootstrap_test"},
        json={"domain": DOMAIN},
    )
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"


def test_a_claim_starts_unverified() -> None:
    """Claiming must not publish anything - the same reason `define_flag`
    starts closed."""
    resp = _claim()
    assert resp.status_code == 200, resp.text
    body = resp.json()["domain"]
    assert body["domain"] == DOMAIN
    assert body["verified"] is False
    assert body["verification_token"]


def test_a_pasted_url_is_normalised_to_a_host() -> None:
    resp = _claim(f"https://{DOMAIN.upper()}:8443/some/path")
    assert resp.status_code == 200, resp.text
    assert resp.json()["domain"]["domain"] == DOMAIN


# An empty string is not here: `DomainIn.domain` has `min_length=1`, so
# Pydantic answers 422 before the handler runs. That is the correct layer for
# it and asserting 400 would have been testing the wrong one.
@pytest.mark.parametrize(
    "bad",
    ["localhost", "acme", "evil.local", "bad_host.example", "http://"],
)
def test_unclaimable_hosts_are_rejected(bad: str) -> None:
    resp = _claim(bad)
    assert resp.status_code == 400, f"{bad!r} should be refused: {resp.text}"


def test_the_same_tenant_cannot_claim_twice() -> None:
    _claim()
    resp = _claim()
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "DOMAIN_ALREADY_CLAIMED"


def test_another_tenant_cannot_claim_the_same_host() -> None:
    """Global uniqueness is the security property: if two tenants could own one
    host, resolution would depend on row order and one tenant's branding could
    be served for the other's domain."""
    _claim()
    resp = _claim(tenant=TENANT_OTHER)

    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "DOMAIN_UNAVAILABLE"
    # And it does not say who owns it: the message must not reveal the other
    # tenant, or this becomes a way to discover which tenant owns which domain.
    assert "dom-t1" not in resp.text
    assert TENANT not in resp.text


# --- verification and the public page -------------------------------------


def test_an_unverified_domain_does_not_resolve() -> None:
    """The load-bearing one. If this passed, any tenant could claim a domain
    they do not own and have the platform serve their branding on it."""
    _claim()
    resp = _public_branding(DOMAIN)
    assert resp.status_code == 404, resp.text
    assert resp.json()["error"]["code"] == "UNKNOWN_HOST"


def test_verifying_makes_the_host_resolve_to_that_tenants_branding() -> None:
    domain_id = _claim().json()["domain"]["id"]
    assert _verify(domain_id).status_code == 200

    resp = _public_branding(DOMAIN)

    assert resp.status_code == 200, resp.text
    branding = resp.json()["branding"]
    assert branding["display_name"] == "Brand dom-t1"
    assert branding["primary_color"] == "#1a2b3c"
    assert branding["support_email"] == "help@dom-t1.example"


def test_the_public_page_does_not_leak_a_tenant_id() -> None:
    """The caller is anonymous; a stable tenant id would be a handle for
    probing the API."""
    domain_id = _claim().json()["domain"]["id"]
    _verify(domain_id)

    resp = _public_branding(DOMAIN)

    assert resp.status_code == 200
    assert TENANT not in resp.text
    assert "tenant_id" not in resp.text


def test_the_host_is_matched_case_insensitively() -> None:
    """A browser may send any casing, and the CHECK constraint stores
    lowercase; without the lower() in the resolver the row would never match."""
    domain_id = _claim().json()["domain"]["id"]
    _verify(domain_id)

    assert _public_branding(DOMAIN.upper()).status_code == 200


def test_an_unknown_host_is_not_found() -> None:
    resp = _public_branding("nothing-here.example")
    assert resp.status_code == 404


def test_a_suspended_tenant_stops_serving() -> None:
    domain_id = _claim().json()["domain"]["id"]
    _verify(domain_id)
    assert _public_branding(DOMAIN).status_code == 200

    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        conn.execute(text("UPDATE tenants SET status = 'suspended' WHERE id = :t"), {"t": TENANT})
    try:
        assert _public_branding(DOMAIN).status_code == 404
    finally:
        with admin.begin() as conn:
            conn.execute(text("UPDATE tenants SET status = 'active' WHERE id = :t"), {"t": TENANT})
        admin.dispose()


def test_verifying_twice_writes_one_audit_event() -> None:
    """Re-confirming is not an incident and must not record a transition that
    did not happen."""
    domain_id = _claim().json()["domain"]["id"]

    first = _verify(domain_id)
    second = _verify(domain_id)

    assert first.json()["changed"] is True
    assert second.json()["changed"] is False

    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        n = conn.execute(
            text(
                "SELECT count(*) FROM audit_events WHERE tenant_id = :t "
                "AND action = 'tenant_domain.verified'"
            ),
            {"t": TENANT},
        ).scalar_one()
    admin.dispose()
    assert n == 1


# --- cross-tenant ---------------------------------------------------------


def test_another_tenant_cannot_verify_or_remove_a_foreign_domain() -> None:
    domain_id = _claim().json()["domain"]["id"]

    assert _verify(domain_id, tenant=TENANT_OTHER).status_code == 404
    resp = _client(TENANT_OTHER, "tenant_owner").delete(
        f"/v1/tenant/domains/{domain_id}", headers=_headers()
    )
    assert resp.status_code == 404, resp.text

    # Still the original tenant's, still unverified.
    listing = _client(TENANT, "tenant_owner").get("/v1/tenant/domains", headers=_headers()).json()
    assert listing["domains"][0]["verified"] is False


def test_a_listing_does_not_show_another_tenants_domains() -> None:
    _claim(tenant=TENANT_OTHER)
    resp = _client(TENANT, "tenant_owner").get("/v1/tenant/domains", headers=_headers())
    assert resp.status_code == 200
    assert resp.json()["domains"] == []


# --- releasing ------------------------------------------------------------


def test_releasing_frees_the_host_for_another_tenant() -> None:
    domain_id = _claim().json()["domain"]["id"]
    _verify(domain_id)

    resp = _client(TENANT, "tenant_owner").delete(
        f"/v1/tenant/domains/{domain_id}", headers=_headers()
    )
    assert resp.status_code == 200, resp.text

    assert _public_branding(DOMAIN).status_code == 404
    assert _claim(tenant=TENANT_OTHER).status_code == 200


# --- the bootstrap function ----------------------------------------------


def test_the_domain_resolver_is_not_publicly_executable() -> None:
    """The second place RLS is deliberately stepped around, so the grant is
    the boundary."""
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        proacl = conn.execute(
            text("SELECT proacl FROM pg_proc WHERE proname = 'resolve_domain_tenant'")
        ).scalar_one()
    admin.dispose()

    grants = " ".join(proacl or [])
    assert "platform_app=X" in grants
    assert "=X/" not in grants.replace("platform=X/", "").replace("platform_app=X/", "")
