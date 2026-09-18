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

    def test_the_put_response_reports_real_usage_not_zero(self) -> None:
        """The regression this handler had.

        `set_config('app.tenant_id', ..., true)` is transaction-scoped, and the
        handler commits the new quota before reading the snapshot. The read
        after that commit was therefore unbound, and RLS returned **zero rows
        with no error** - so the endpoint reported `runs_used: 0` for a tenant
        that had consumed runs. `quota` and `remaining` come from the tenant
        row and were still right, which is why asserting only those left the
        bug invisible.
        """
        assert _queue_run(_client(TENANT, "support_admin")).status_code == 200

        resp = _client(TENANT, "tenant_owner").put(
            "/v1/tenant/quota", headers=_headers(str(uuid.uuid4())), json={"monthly_run_quota": 5}
        )
        assert resp.status_code == 200, resp.text
        usage = resp.json()["usage"]
        assert usage["runs_used"] == 1, "a committed run must still be visible after the commit"
        assert usage["remaining"] == 4

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


class TestBillingAdjustment:
    """`POST /v1/tenant/billing/adjustments` — correcting an append-only ledger.

    The ledger grants the app role no UPDATE and no DELETE, so a correction is
    a new row. That makes the endpoint the only way to fix a billing error
    without a database console, and makes its idempotency load-bearing: a
    retried correction that applied twice would misstate what a customer owes.
    """

    def _adjust(self, client: TestClient, *, key: str | None = None, **body: object) -> object:
        payload = {"run_id": str(uuid.uuid4()), "prompt_tokens_delta": -50, **body}
        headers = _headers(key if key is not None else str(uuid.uuid4()))
        return client.post("/v1/tenant/billing/adjustments", headers=headers, json=payload)

    def test_only_the_owner_may_adjust(self) -> None:
        """BILLING_ADJUST, not TENANT_ADMIN. A support admin administers the
        tenant; crediting an account is a financial statement about a
        customer."""
        resp = self._adjust(_client(TENANT, "support_admin"))
        assert resp.status_code == 403, resp.text

    def test_an_auditor_may_not_adjust_either(self) -> None:
        """Reading the ledger and changing it are different capabilities."""
        resp = self._adjust(_client(TENANT, "auditor"))
        assert resp.status_code == 403, resp.text

    def test_a_write_without_an_idempotency_key_is_refused(self) -> None:
        resp = _client(TENANT, "tenant_owner").post(
            "/v1/tenant/billing/adjustments",
            headers=_headers(),
            json={"run_id": str(uuid.uuid4()), "prompt_tokens_delta": -10},
        )
        assert resp.status_code == 400, resp.text
        assert resp.json()["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"

    def test_an_empty_adjustment_is_refused(self) -> None:
        """A zero delta would append a row that changes no total."""
        resp = self._adjust(_client(TENANT, "tenant_owner"), prompt_tokens_delta=0)
        assert resp.status_code == 400, resp.text
        assert resp.json()["error"]["code"] == "ADJUSTMENT_EMPTY"

    def test_a_credit_reduces_the_period_total(self) -> None:
        admin = create_engine(ADMIN_URL)
        with admin.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO billing_entries (id, tenant_id, event_id, run_id, "
                    "entry_kind, route, run_status, prompt_tokens, completion_tokens, "
                    "period_start, recorded_at) "
                    "VALUES (:id, :t, :ev, :run, 'usage', '', '', 500, 0, :period, :ts)"
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

        resp = self._adjust(_client(TENANT, "tenant_owner"), prompt_tokens_delta=-200)
        assert resp.status_code == 200, resp.text
        billing = resp.json()["billing"]
        assert billing["usage_entries"] == 1
        assert billing["adjustment_entries"] == 1
        assert billing["prompt_tokens"] == 300, "500 consumed less a 200 credit"

    def test_the_same_idempotency_key_does_not_credit_twice(self) -> None:
        """The property that makes this safe to retry. Without it, a client
        that retried after a timeout would credit the account twice."""
        client = _client(TENANT, "tenant_owner")
        key = str(uuid.uuid4())

        first = self._adjust(client, key=key, prompt_tokens_delta=-100)
        assert first.status_code == 200, first.text
        assert first.json()["duplicate"] is False

        second = self._adjust(client, key=key, prompt_tokens_delta=-100)
        assert second.status_code == 200, second.text
        assert second.json()["duplicate"] is True, "a redelivery must be a no-op"
        assert second.json()["billing"]["adjustment_entries"] == 1

    def test_a_redelivery_writes_no_second_audit_event(self) -> None:
        """An audit trail that records a retry as an action reports activity
        that never happened."""
        client = _client(TENANT, "tenant_owner")
        key = str(uuid.uuid4())
        self._adjust(client, key=key)
        self._adjust(client, key=key)

        admin = create_engine(ADMIN_URL)
        with admin.begin() as conn:
            count = conn.execute(
                text(
                    "SELECT count(*) FROM audit_events WHERE tenant_id = :t "
                    "AND action = 'billing.adjustment.recorded'"
                ),
                {"t": TENANT},
            ).scalar()
        admin.dispose()
        assert count == 1

    def test_a_different_key_is_a_second_correction(self) -> None:
        """Two deliberate corrections are two rows; only a retry collapses."""
        client = _client(TENANT, "tenant_owner")
        self._adjust(client, key=str(uuid.uuid4()))
        resp = self._adjust(client, key=str(uuid.uuid4()))
        assert resp.json()["billing"]["adjustment_entries"] == 2
