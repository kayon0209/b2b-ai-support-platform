"""Integration tests: tenant usage and quota (Phase 5).

- usage is reported for the current calendar month
- the quota is set by an admin, and only by an admin
- queuing a run is refused with 429 once the quota is exhausted
- clearing the quota restores unlimited use
"""

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

pytestmark = pytest.mark.integration

ADMIN_URL = "postgresql+psycopg://platform:platform@localhost:5435/platform"
TENANT = "0190d000-0000-7000-8000-0000000000f4"


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
def seed_tenant():
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO tenants (id, slug, name, status) "
                "VALUES (:id, 'usage-t1', 'Usage', 'active') "
                "ON CONFLICT (slug) DO NOTHING"
            ),
            {"id": TENANT},
        )
    yield
    with admin.begin() as conn:
        conn.execute(text("DELETE FROM inbox_events WHERE tenant_id = :t"), {"t": TENANT})
        conn.execute(text("DELETE FROM agent_runs WHERE tenant_id = :t"), {"t": TENANT})
        conn.execute(text("DELETE FROM audit_events WHERE tenant_id = :t"), {"t": TENANT})
        conn.execute(text("DELETE FROM tenants WHERE slug = 'usage-t1'"))
    admin.dispose()


@pytest.fixture(autouse=True)
def reset_usage():
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        conn.execute(text("DELETE FROM inbox_events WHERE tenant_id = :t"), {"t": TENANT})
        conn.execute(text("DELETE FROM agent_runs WHERE tenant_id = :t"), {"t": TENANT})
        # Cleared too, so the audit assertion counts only its own write.
        conn.execute(text("DELETE FROM audit_events WHERE tenant_id = :t"), {"t": TENANT})
        conn.execute(
            text("UPDATE tenants SET monthly_run_quota = NULL WHERE id = :t"), {"t": TENANT}
        )
    yield
    admin.dispose()


def _queue_run(client: TestClient) -> object:
    return client.post(
        f"/v1/conversations/{uuid.uuid4()}/agent-runs",
        headers={**_headers(), "Idempotency-Key": str(uuid.uuid4())},
        json={"trigger_message_ref": "m-1"},
    )


class TestUsage:
    def test_fresh_tenant_reports_zero_usage(self) -> None:
        resp = _client(TENANT, "support_agent").get("/v1/tenant/usage", headers=_headers())
        assert resp.status_code == 200, resp.text
        usage = resp.json()["usage"]
        assert usage["runs_used"] == 0
        assert usage["quota"] is None
        assert usage["remaining"] is None
        assert usage["over_quota"] is False
        # The window is a calendar month, not a rolling window.
        assert usage["period_end"] > usage["period_start"]

    def test_usage_counts_queued_runs(self) -> None:
        client = _client(TENANT, "support_admin")
        assert _queue_run(client).status_code == 200

        usage = client.get("/v1/tenant/usage", headers=_headers()).json()["usage"]
        assert usage["runs_used"] == 1


class TestQuota:
    def test_admin_can_set_the_quota(self) -> None:
        client = _client(TENANT, "tenant_owner")
        resp = client.put(
            "/v1/tenant/quota", headers=_headers(str(uuid.uuid4())), json={"monthly_run_quota": 5}
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["usage"]["quota"] == 5
        assert resp.json()["usage"]["remaining"] == 5

    def test_non_admin_cannot_set_the_quota(self) -> None:
        resp = _client(TENANT, "support_agent").put(
            "/v1/tenant/quota", headers=_headers(str(uuid.uuid4())), json={"monthly_run_quota": 5}
        )
        assert resp.status_code == 403

    def test_setting_the_quota_requires_an_idempotency_key(self) -> None:
        resp = _client(TENANT, "tenant_owner").put(
            "/v1/tenant/quota", headers=_headers(), json={"monthly_run_quota": 5}
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"

    def test_negative_quota_is_rejected(self) -> None:
        resp = _client(TENANT, "tenant_owner").put(
            "/v1/tenant/quota", headers=_headers(str(uuid.uuid4())), json={"monthly_run_quota": -1}
        )
        assert resp.status_code == 422  # pydantic ge=0

    def test_exhausted_quota_refuses_a_new_run_with_429(self) -> None:
        owner = _client(TENANT, "tenant_owner")
        owner.put(
            "/v1/tenant/quota", headers=_headers(str(uuid.uuid4())), json={"monthly_run_quota": 0}
        )

        resp = _queue_run(_client(TENANT, "support_admin"))
        assert resp.status_code == 429, resp.text
        assert resp.json()["error"]["code"] == "QUOTA_EXCEEDED"

    def test_clearing_the_quota_restores_unlimited_use(self) -> None:
        owner = _client(TENANT, "tenant_owner")
        owner.put(
            "/v1/tenant/quota", headers=_headers(str(uuid.uuid4())), json={"monthly_run_quota": 0}
        )
        assert _queue_run(_client(TENANT, "support_admin")).status_code == 429

        owner.put(
            "/v1/tenant/quota",
            headers=_headers(str(uuid.uuid4())),
            json={"monthly_run_quota": None},
        )
        assert _queue_run(_client(TENANT, "support_admin")).status_code == 200

    def test_setting_the_quota_is_audited(self) -> None:
        _client(TENANT, "tenant_owner").put(
            "/v1/tenant/quota", headers=_headers(str(uuid.uuid4())), json={"monthly_run_quota": 7}
        )
        admin = create_engine(ADMIN_URL)
        with admin.begin() as conn:
            count = conn.execute(
                text(
                    "SELECT count(*) FROM audit_events WHERE tenant_id = :t "
                    "AND action = 'tenant.quota.updated'"
                ),
                {"t": TENANT},
            ).scalar()
        admin.dispose()
        assert count == 1
