"""HTTP integration tests for the quality dashboard endpoints (ticket 35).

`test_quality_metrics.py` proves the *aggregation* is correct. This suite
proves the *endpoint* around it is safe, which is a different question: the
handler is where tenant binding and the role gate are decided, and a
dashboard is the classic place a leak hides because it answers with counts
rather than rows. An unbound query does not look broken - it just returns a
bigger number than it should.

What is pinned here:

1. Shape     - the documented payload reaches the client with real numbers.
2. Role gate - `support_agent` can read cases but must not read the
               tenant-wide quality picture; `auditor` can.
3. Tenant    - a run belonging to another tenant is never counted.
4. Bounds    - `window_seconds` is clamped by the query validator, so the
               dashboard cannot be turned into an unbounded scan.
"""

import os
import time
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

TENANT_A = "0190a000-0000-7000-8000-0000000000a1"
TENANT_B = "0190a000-0000-7000-8000-0000000000b1"
CONV = "0190a000-0000-7000-8000-0000000000c1"

_RUN_INSERT = (
    "INSERT INTO agent_runs "
    "(id, tenant_id, conversation_ref_id, route, status, latency_ms, started_at, input_hash) "
    "VALUES (:id, :t, :conv, :route, :status, :latency, :started, :hash)"
)


class _RoleResolver:
    """Fabricate a TenantContext with a fixed tenant and role.

    Resolution is server-side in production (bootstrap token -> membership
    row); this stands in for that lookup so the policy gate is what is under
    test, not the auth backend.
    """

    def __init__(self, tenant_id: str, role: str) -> None:
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


def _client(tenant_id: str, role: str) -> TestClient:
    """A fresh app sharing the real routers under a fabricated role.

    A fresh FastAPI instance is required because middleware cannot be added
    after the app has started.
    """
    import importlib

    main_mod = importlib.import_module("platform_core.main")
    fresh = FastAPI()
    for route in main_mod.app.router.routes:
        fresh.router.routes.append(route)
    fresh.add_middleware(TenantContextMiddleware, resolver=_RoleResolver(tenant_id, role))
    return TestClient(fresh, raise_server_exceptions=False)


def _new_run(
    *,
    tenant: str = TENANT_A,
    route: str = "knowledge_qa",
    status: str = "completed",
    latency: int | None = 100,
    age_seconds: int = 0,
) -> dict:
    return {
        "id": str(uuid.uuid4()),
        "t": tenant,
        "conv": CONV,
        "route": route,
        "status": status,
        "latency": latency,
        "started": int(time.time()) - age_seconds,
    }


def _clear_runs() -> None:
    """Citations reference agent_runs, so they must go first."""
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        conn.execute(
            text("DELETE FROM citations WHERE tenant_id IN (:a, :b)"),
            {"a": TENANT_A, "b": TENANT_B},
        )
        conn.execute(
            text("DELETE FROM agent_runs WHERE tenant_id IN (:a, :b)"),
            {"a": TENANT_A, "b": TENANT_B},
        )
    admin.dispose()


@pytest.fixture(scope="module", autouse=True)
def seed_tenants():
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        for tid, slug in ((TENANT_A, "quality-api-a"), (TENANT_B, "quality-api-b")):
            conn.execute(
                text(
                    "INSERT INTO tenants (id, slug, name, status) VALUES "
                    "(:id, :slug, :name, 'active') ON CONFLICT (slug) DO NOTHING"
                ),
                {"id": tid, "slug": slug, "name": slug},
            )
    yield
    with admin.begin() as conn:
        conn.execute(text("DELETE FROM tenants WHERE slug LIKE 'quality-api-%'"))
    admin.dispose()


@pytest.fixture(autouse=True)
def clean_runs():
    _clear_runs()
    yield
    _clear_runs()


def _insert(*rows: dict) -> None:
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        for row in rows:
            # Every row here models a run that executed, so it carries a
            # question hash - the field the platform writes the moment
            # execution begins, and the one that separates a real run from a
            # queue placeholder. Seeding '' made these fixtures depend on
            # placeholders being aggregated as if they had run.
            conn.execute(text(_RUN_INSERT), {**row, "hash": uuid.uuid4().hex * 2})
    admin.dispose()


def _headers() -> dict[str, str]:
    return {"Authorization": "Bearer pt_bootstrap_test"}


# --- 1. Shape ---------------------------------------------------------------


def test_metrics_endpoint_returns_the_documented_shape() -> None:
    """A caller with the right role gets the full payload, not a stub."""
    _insert(
        _new_run(route="knowledge_qa", status="completed"),
        _new_run(route="knowledge_qa", status="abstained"),
        _new_run(route="human_required", status="handed_off"),
    )

    resp = _client(TENANT_A, "auditor").get("/v1/quality/metrics", headers=_headers())

    assert resp.status_code == 200
    body = resp.json()
    for field in (
        "window_seconds",
        "total_runs",
        "completed",
        "abstained",
        "handed_off",
        "failed",
        "untimed_runs",
        "abstention_rate",
        "handoff_rate",
        "citation_coverage",
        "route_counts",
        "latency_p50_ms",
        "latency_p95_ms",
    ):
        assert field in body, f"missing metric: {field}"
    assert body["total_runs"] == 3
    assert body["abstained"] == 1
    assert body["handed_off"] == 1
    assert body["route_counts"]["knowledge_qa"] == 2


def test_routes_endpoint_returns_just_the_route_mix() -> None:
    """The alerting-facing endpoint stays small and stable."""
    _insert(_new_run(route="knowledge_qa"), _new_run(route="tool_action"))

    resp = _client(TENANT_A, "auditor").get("/v1/quality/routes", headers=_headers())

    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"window_seconds", "route_counts", "total_runs"}
    assert body["route_counts"] == {"knowledge_qa": 1, "tool_action": 1}
    assert body["total_runs"] == 2


def test_empty_tenant_reports_zeros_not_an_error() -> None:
    """A quiet tenant is a legitimate state, not a 404."""
    resp = _client(TENANT_A, "auditor").get("/v1/quality/metrics", headers=_headers())

    assert resp.status_code == 200
    body = resp.json()
    assert body["total_runs"] == 0
    assert body["route_counts"] == {}


# --- 2. Role gate -----------------------------------------------------------


@pytest.mark.parametrize("path", ["/v1/quality/metrics", "/v1/quality/routes"])
def test_support_agent_cannot_read_quality(path: str, assert_denied) -> None:
    """Quality telemetry is not part of the agent's job."""
    _insert(_new_run())

    resp = _client(TENANT_A, "support_agent").get(path, headers=_headers())

    assert_denied(resp, "QUALITY_ACCESS_DENIED")


def test_unknown_role_fails_closed(assert_denied) -> None:
    """Deny-by-default: an unrecognized role is not an implicit allow."""
    resp = _client(TENANT_A, "no_such_role").get("/v1/quality/metrics", headers=_headers())

    assert_denied(resp, "QUALITY_ACCESS_DENIED")


def test_support_viewer_cannot_read_quality(assert_denied) -> None:
    """Read access to cases does not imply access to tenant-wide metrics."""
    resp = _client(TENANT_A, "support_viewer").get("/v1/quality/metrics", headers=_headers())

    assert_denied(resp, "QUALITY_ACCESS_DENIED")


def test_denial_leaks_no_counts() -> None:
    """The denial body must not carry the number it refused to show."""
    _insert(*[_new_run() for _ in range(5)])

    body = _client(TENANT_A, "support_agent").get("/v1/quality/metrics", headers=_headers()).json()

    assert "total_runs" not in body
    assert "route_counts" not in body


# --- 3. Tenant isolation ----------------------------------------------------


def test_another_tenants_runs_are_never_counted() -> None:
    """The binding is server-side; tenant B's rows must not inflate A's count."""
    _insert(_new_run(tenant=TENANT_A), _new_run(tenant=TENANT_B), _new_run(tenant=TENANT_B))

    body = _client(TENANT_A, "auditor").get("/v1/quality/metrics", headers=_headers()).json()

    assert body["total_runs"] == 1


def test_each_tenant_sees_only_itself() -> None:
    """The same endpoint answers differently per caller, which is the point."""
    _insert(*[_new_run(tenant=TENANT_A) for _ in range(2)], _new_run(tenant=TENANT_B))

    a = _client(TENANT_A, "auditor").get("/v1/quality/metrics", headers=_headers()).json()
    b = _client(TENANT_B, "auditor").get("/v1/quality/metrics", headers=_headers()).json()

    assert a["total_runs"] == 2
    assert b["total_runs"] == 1


# --- 4. Window bounds -------------------------------------------------------


def test_window_excludes_runs_older_than_the_window() -> None:
    """A trailing window is the contract; stale runs must drop out."""
    _insert(_new_run(age_seconds=0), _new_run(age_seconds=7200))

    body = (
        _client(TENANT_A, "auditor")
        .get("/v1/quality/metrics", params={"window_seconds": 3600}, headers=_headers())
        .json()
    )

    assert body["total_runs"] == 1
    assert body["window_seconds"] == 3600


def test_window_below_the_floor_is_rejected() -> None:
    """`ge=60` stops a caller scanning an arbitrarily narrow slice."""
    resp = _client(TENANT_A, "auditor").get(
        "/v1/quality/metrics", params={"window_seconds": 1}, headers=_headers()
    )

    assert resp.status_code == 422


def test_window_above_the_ceiling_is_rejected() -> None:
    """`le=30d` stops the dashboard becoming an unbounded table scan."""
    resp = _client(TENANT_A, "auditor").get(
        "/v1/quality/metrics", params={"window_seconds": 86400 * 365}, headers=_headers()
    )

    assert resp.status_code == 422
