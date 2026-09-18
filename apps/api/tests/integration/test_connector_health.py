"""Integration tests: connector health, reauthorization and credential rotation.

Covers the Phase 3 acceptance criterion "OAuth reauthorization is visible and
actionable" against real Postgres and the real routers: the inventory is
readable by the roles that need it and no one else, a probe records itself,
a connector cannot be returned to service while its credential is
unresolvable, rotating the reference clears NEEDS_REAUTH, and a connector in
NEEDS_REAUTH yields no executor (so the gateway cannot write through it).

The state machine's rules are proven in the unit suite
(`unit/integrations/test_connector_health.py`); these tests prove the storage,
the authorization split and the wiring.
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
APP_URL = "postgresql+psycopg://platform_app:platform_app@localhost:5435/platform"

TENANT = "0190d000-0000-7000-8000-0000000000e1"
TENANT_OTHER = "0190d000-0000-7000-8000-0000000000e2"

# Port 9 (discard) refuses immediately, so an "unreachable" probe costs no
# test time. A real hostname would add DNS + connect timeout to every run.
UNREACHABLE_BASE = "http://127.0.0.1:9"

_CLEAN: tuple[str, ...] = (
    "DELETE FROM dead_letter_items WHERE tenant_id IN (:a, :b)",
    "DELETE FROM sync_cursors WHERE tenant_id IN (:a, :b)",
    "DELETE FROM connectors WHERE tenant_id IN (:a, :b)",
    "DELETE FROM outbox_events WHERE tenant_id IN (:a, :b)",
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
        for tid, slug in ((TENANT, "conn-t1"), (TENANT_OTHER, "conn-t2")):
            conn.execute(
                text(
                    "INSERT INTO tenants (id, slug, name, status) VALUES "
                    "(:id, :slug, :name, 'active') ON CONFLICT (slug) DO NOTHING"
                ),
                {"id": tid, "slug": slug, "name": slug},
            )
    yield
    with admin.begin() as conn:
        _clean(conn)
        conn.execute(text("DELETE FROM tenants WHERE slug LIKE 'conn-t%'"))
    admin.dispose()


@pytest.fixture(autouse=True)
def clean_connectors():
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        _clean(conn)
    yield
    with admin.begin() as conn:
        _clean(conn)
    admin.dispose()


def _insert_connector(
    *,
    tenant: str = TENANT,
    provider: str = "crm",
    status: str = "active",
    capabilities: str = '["update_account"]',
    credential_ref: str | None = "env://TEST_CRM_TOKEN",
    connector_id: str | None = None,
) -> str:
    """Insert a connector as the superuser role.

    The superuser is used so a connector can be seeded in a chosen status;
    every read under test still goes through the RLS-bound app role.
    """
    cid = connector_id or str(uuid.uuid4())
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO connectors (id, tenant_id, provider, name, status, "
                "capabilities, configuration, credential_ref) VALUES "
                "(:id, :tid, :provider, :name, :status, CAST(:caps AS jsonb), "
                "CAST(:cfg AS jsonb), :ref)"
            ),
            {
                "id": cid,
                "tid": tenant,
                "provider": provider,
                "name": f"{provider}-primary",
                "status": status,
                "caps": capabilities,
                "cfg": f'{{"base_url": "{UNREACHABLE_BASE}"}}',
                "ref": credential_ref,
            },
        )
    admin.dispose()
    return cid


def _status_of(connector_id: str) -> str:
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        row = conn.execute(
            text("SELECT status FROM connectors WHERE id = :id"), {"id": connector_id}
        ).scalar_one()
    admin.dispose()
    return str(row)


def _audit_actions(resource_id: str) -> list[str]:
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        rows = (
            conn.execute(
                text(
                    "SELECT action FROM audit_events WHERE resource_id = :id ORDER BY occurred_at"
                ),
                {"id": resource_id},
            )
            .scalars()
            .all()
        )
    admin.dispose()
    return [str(r) for r in rows]


# --- inventory reads -------------------------------------------------------


def test_inventory_requires_connector_read() -> None:
    """A viewer cannot see the connection inventory."""
    _insert_connector()
    resp = _client(TENANT, "support_viewer").get("/v1/connectors", headers=_headers())
    assert resp.status_code == 403, resp.text
    assert resp.json()["error"]["code"] == "POLICY_DENIED"


def test_support_admin_can_read_the_inventory() -> None:
    """When a connector is parked in NEEDS_REAUTH the support admin is the
    person whose tools stopped working, so the state has to reach them."""
    _insert_connector()
    resp = _client(TENANT, "support_admin").get("/v1/connectors", headers=_headers())
    assert resp.status_code == 200, resp.text
    body = resp.json()["connectors"]
    assert len(body) == 1
    assert body[0]["provider"] == "crm"
    assert body[0]["status"] == "active"
    assert body[0]["executable"] is True


def test_inventory_never_returns_the_credential_reference() -> None:
    """The reference names a path inside the secret manager. The operational
    fact is whether one is configured, not what it is."""
    _insert_connector(credential_ref="env://TEST_CRM_TOKEN")
    resp = _client(TENANT, "support_admin").get("/v1/connectors", headers=_headers())
    payload = resp.json()
    assert "env://TEST_CRM_TOKEN" not in resp.text
    assert payload["connectors"][0]["credential_configured"] is True


def test_inventory_does_not_show_another_tenants_connector() -> None:
    _insert_connector(tenant=TENANT_OTHER)
    resp = _client(TENANT, "support_admin").get("/v1/connectors", headers=_headers())
    assert resp.status_code == 200
    assert resp.json()["connectors"] == []


# --- probes ----------------------------------------------------------------


def test_probe_requires_connector_admin_not_connector_read() -> None:
    """A probe can move an active connector to degraded, which removes it from
    the executor set - so it is a configuration permission, not a read."""
    cid = _insert_connector()
    resp = _client(TENANT, "support_admin").post(
        f"/v1/connectors/{cid}/health-check", headers=_headers()
    )
    assert resp.status_code == 403, resp.text
    assert _status_of(cid) == "active"


def test_probe_requires_an_idempotency_key() -> None:
    cid = _insert_connector()
    resp = _client(TENANT, "tenant_owner").post(
        f"/v1/connectors/{cid}/health-check",
        headers={"Authorization": "Bearer pt_bootstrap_test"},
    )
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"


def test_unreachable_probe_records_time_and_degrades() -> None:
    cid = _insert_connector()
    resp = _client(TENANT, "tenant_owner").post(
        f"/v1/connectors/{cid}/health-check", headers=_headers()
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["reachable"] is False
    assert body["status_changed"] is True
    assert body["previous_status"] == "active"
    assert body["connector"]["status"] == "degraded"
    assert body["connector"]["last_health_at"] is not None
    assert body["connector"]["executable"] is False
    assert _status_of(cid) == "degraded"


def test_probe_reports_unsupported_provider_distinctly() -> None:
    """`servicenow` ships no adapter in this deployment. Reporting that as an
    outage would send the operator to the network to fix a capability gap.

    This test used `linear` as the example until Linear was actually shipped -
    at which point it stopped testing an unsupported provider and started
    asserting that a supported one is not. The provider has to be one the
    registry genuinely has no factory for.
    """
    cid = _insert_connector(provider="servicenow", capabilities='["read_issue"]')
    resp = _client(TENANT, "tenant_owner").post(
        f"/v1/connectors/{cid}/health-check", headers=_headers()
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == "CONNECTOR_PROBE_UNSUPPORTED"


def test_probe_of_another_tenants_connector_is_not_found() -> None:
    cid = _insert_connector(tenant=TENANT_OTHER)
    resp = _client(TENANT, "tenant_owner").post(
        f"/v1/connectors/{cid}/health-check", headers=_headers()
    )
    assert resp.status_code == 404, resp.text
    assert _status_of(cid) == "active"


# --- reactivation ----------------------------------------------------------


def test_reactivate_refuses_when_the_credential_does_not_resolve(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The failure this catches: the operator repointed the reference at a
    variable they forgot to set. Clearing NEEDS_REAUTH here would leave the
    API reporting `active` for a connector that still cannot authenticate."""
    monkeypatch.delenv("TEST_CRM_TOKEN", raising=False)
    cid = _insert_connector(status="needs_reauth", credential_ref="env://TEST_CRM_TOKEN")

    resp = _client(TENANT, "tenant_owner").post(
        f"/v1/connectors/{cid}/reactivate", headers=_headers()
    )

    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "CREDENTIAL_UNRESOLVED"
    # A refused request must not record a transition it did not make.
    assert _status_of(cid) == "needs_reauth"
    assert "connector.status_changed" not in _audit_actions(cid)


def test_reactivate_refuses_when_the_provider_is_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_CRM_TOKEN", "token-value")
    cid = _insert_connector(status="needs_reauth", credential_ref="env://TEST_CRM_TOKEN")

    resp = _client(TENANT, "tenant_owner").post(
        f"/v1/connectors/{cid}/reactivate", headers=_headers()
    )

    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "CONNECTOR_UNREACHABLE"
    assert _status_of(cid) == "needs_reauth"


def test_reactivate_succeeds_and_is_audited(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_CRM_TOKEN", "token-value")
    cid = _insert_connector(status="needs_reauth", credential_ref="env://TEST_CRM_TOKEN")
    # The reachability half of the gate is stubbed: standing up an HTTP server
    # to answer /health would test httpx, not this rule. The credential half
    # runs for real against the environment.
    monkeypatch.setattr("platform_core.integrations.router.probe_connector", _reachable)

    resp = _client(TENANT, "tenant_owner").post(
        f"/v1/connectors/{cid}/reactivate", headers=_headers()
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["connector"]["status"] == "active"
    assert resp.json()["connector"]["executable"] is True
    assert _status_of(cid) == "active"
    assert "connector.status_changed" in _audit_actions(cid)


async def _reachable(*args: object, **kwargs: object) -> bool:
    return True


# --- credential rotation ---------------------------------------------------


def test_rotation_rejects_a_value_that_is_not_a_reference() -> None:
    """A reference without a scheme is almost always a pasted secret. Rejecting
    it keeps credentials out of a column read by every request and copied into
    every backup."""
    cid = _insert_connector()
    resp = _client(TENANT, "tenant_owner").put(
        f"/v1/connectors/{cid}/credential-ref",
        headers=_headers(),
        json={"credential_ref": "sk-live-abc123"},
    )
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == "VALIDATION_FAILED"


def test_rotation_clears_needs_reauth_and_is_audited() -> None:
    cid = _insert_connector(status="needs_reauth", credential_ref="env://OLD_TOKEN")
    resp = _client(TENANT, "tenant_owner").put(
        f"/v1/connectors/{cid}/credential-ref",
        headers=_headers(),
        json={"credential_ref": "env://NEW_TOKEN"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["previous_status"] == "needs_reauth"
    assert body["connector"]["status"] == "active"
    assert _status_of(cid) == "active"
    assert "connector.credential_rotated" in _audit_actions(cid)


def test_rotation_requires_connector_admin() -> None:
    cid = _insert_connector()
    resp = _client(TENANT, "support_admin").put(
        f"/v1/connectors/{cid}/credential-ref",
        headers=_headers(),
        json={"credential_ref": "env://NEW_TOKEN"},
    )
    assert resp.status_code == 403, resp.text


# --- the executor gate -----------------------------------------------------


def test_needs_reauth_connector_yields_no_executor() -> None:
    """The reason the status matters: a parked connector must not be able to
    execute a write. This is the wiring the registry comment claimed."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from platform_core.db import create_engine as app_engine
    from platform_core.identity.tenant_context import apply_rls_tenant
    from platform_core.tool_gateway.registry import resolve_executors

    _insert_connector(status="needs_reauth")

    async def _probe() -> dict[str, object]:
        engine = app_engine(APP_URL)
        try:
            factory = async_sessionmaker(engine, expire_on_commit=False)
            async with factory() as session:
                await apply_rls_tenant(
                    session,
                    TenantContext(tenant_id=uuid.UUID(TENANT), actor_id=None, actor_kind="service"),
                )
                return await resolve_executors(
                    session,
                    tenant_id=uuid.UUID(TENANT),
                    tool_names=["crm.update_account"],
                )
        finally:
            await engine.dispose()

    assert _run(_probe()) == {}


def test_active_connector_yields_an_executor() -> None:
    """The mirror of the previous test: proves the gate is not simply refusing
    everything, which a one-sided assertion would not catch."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from platform_core.db import create_engine as app_engine
    from platform_core.identity.tenant_context import apply_rls_tenant
    from platform_core.tool_gateway.registry import resolve_executors

    _insert_connector(status="active")

    async def _probe() -> list[str]:
        engine = app_engine(APP_URL)
        try:
            factory = async_sessionmaker(engine, expire_on_commit=False)
            async with factory() as session:
                await apply_rls_tenant(
                    session,
                    TenantContext(tenant_id=uuid.UUID(TENANT), actor_id=None, actor_kind="service"),
                )
                resolved = await resolve_executors(
                    session,
                    tenant_id=uuid.UUID(TENANT),
                    tool_names=["crm.update_account"],
                )
                return sorted(resolved)
        finally:
            await engine.dispose()

    assert _run(_probe()) == ["crm.update_account"]
