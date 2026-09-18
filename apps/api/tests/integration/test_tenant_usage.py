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
        # The ledger is append-only for the app role; the admin role is what
        # clears it between tests so a rollup assertion measures one test.
        conn.execute(text("DELETE FROM billing_entries WHERE tenant_id = :t"), {"t": TENANT})
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


class TestBillingRollup:
    """`GET /v1/tenant/billing` — the ledger the invoice is computed from.

    The rollup function itself is covered by `test_billing_ledger.py`. What is
    pinned here is the HTTP contract the admin UI reads: the envelope key, and
    the permission boundary. Both would fail silently in the UI - a wrong key
    renders an empty card, and a missing check renders commercial data to a
    support agent.
    """

    def test_the_envelope_is_billing_not_data(self) -> None:
        """The UI reads `body.billing`. `ok_response` spreads the payload
        rather than nesting it under `data`, so this asserts the exact key
        instead of trusting that convention to hold."""
        resp = _client(TENANT, "auditor").get("/v1/tenant/billing", headers=_headers())
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert "billing" in body, body
        assert "trace_id" in body
        billing = body["billing"]
        assert billing["entries"] == 0
        assert billing["usage_entries"] == 0
        assert billing["adjustment_entries"] == 0
        assert billing["total_tokens"] == 0
        assert billing["period_end"] > billing["period_start"]

    def test_a_support_admin_cannot_read_commercial_totals(self) -> None:
        """AUDIT_READ, not CASE_READ. Billing totals are commercial data; the
        support role can see the live run count but not the ledger."""
        resp = _client(TENANT, "support_admin").get("/v1/tenant/billing", headers=_headers())
        assert resp.status_code == 403, resp.text

    def test_an_auditor_can_read_them(self) -> None:
        resp = _client(TENANT, "auditor").get("/v1/tenant/billing", headers=_headers())
        assert resp.status_code == 200, resp.text

    def test_a_recorded_usage_entry_appears_in_the_rollup(self) -> None:
        """End-to-end through the API: a ledger row written for this tenant
        is visible in the response, so the endpoint is not reading an
        unrelated table."""
        admin = create_engine(ADMIN_URL)
        with admin.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO billing_entries (id, tenant_id, event_id, run_id, "
                    "entry_kind, route, run_status, prompt_tokens, completion_tokens, "
                    "period_start, recorded_at) "
                    "VALUES (:id, :t, :ev, :run, 'usage', 'knowledge_qa', 'completed', "
                    "120, 40, :period, :ts)"
                ),
                {
                    "id": str(uuid.uuid4()),
                    "t": TENANT,
                    "ev": str(uuid.uuid4()),
                    "run": str(uuid.uuid4()),
                    "period": _period_start(),
                    "ts": _now(),
                },
            )
        admin.dispose()

        resp = _client(TENANT, "auditor").get("/v1/tenant/billing", headers=_headers())
        assert resp.status_code == 200, resp.text
        billing = resp.json()["billing"]
        assert billing["usage_entries"] == 1
        assert billing["prompt_tokens"] == 120
        assert billing["completion_tokens"] == 40
        assert billing["total_tokens"] == 160


def _now() -> int:
    import time

    return int(time.time())


def _period_start() -> int:
    """First instant of the current calendar month, matching the API."""
    from platform_core.identity.usage import period_bounds

    return period_bounds(_now())[0]
