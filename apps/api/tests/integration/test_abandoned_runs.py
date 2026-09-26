"""Runs that were accepted and never executed.

The queue endpoint writes a `queued` row before any work happens. Most are
adopted within seconds; some never are, and those used to stay `queued`
forever, which made one row status mean two things at once.

What these tests protect, in order of importance:

1. **Admission control still bites.** `usage_snapshot` is not only a report -
   `chat_service.queue_agent_run` reads it to refuse a burst with 429. The
   cheapest fix for the inflation was "count only runs that have a question
   hash", and that fix disarms the gate: every freshly queued run becomes
   invisible, so a thousand concurrent requests all see "under quota". The
   gate test near the end is the one that fails if that is ever done.
2. **Abandoned rows stop counting, and the exclusion is stated.** Usage read
   76 for one tenant against 42 runs that executed (dev database, September
   2026).
3. **The sweep closes only what it can age.** A row with no `started_at` has
   no age; a run that executed is not a placeholder; another tenant's rows are
   not this sweep's business.
4. **The quality surfaces stop inventing traffic.** A placeholder carries the
   queue's default route (`knowledge_qa`) and no intent at all, so counting it
   reported route traffic that never happened and filled the intent
   distribution's `unrecorded` bucket with rows that never had a chance to
   record.

Each group of assertions runs against its own tenant. Aggregates are
`total_runs == 0`-shaped claims, and a shared tenant would make them depend on
which test ran first - the failure mode a previous round of this project hit,
where three files reused one id.
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from platform_core.identity.middleware import TenantContextMiddleware
from platform_core.identity.tenant_context import TenantContext
from platform_core.orm_base import default_uuid

pytestmark = pytest.mark.integration

ADMIN_URL = os.environ.get(
    "APP_ADMIN_DATABASE_URL",
    "postgresql+psycopg://platform:platform@localhost:5435/platform",
)
APP_URL = "postgresql+psycopg://platform_app:platform_app@localhost:5435/platform"

# One tenant per assertion domain, all previously unused ids.
TENANTS = {
    "usage": ("01900000-0000-7000-8000-0000000000df", "abandoned-usage"),
    # A tenant of its own for the accounting delta: the sweep is tenant-wide,
    # so sharing one would make the assertion depend on what ran before it.
    "usage_only": ("01900000-0000-7000-8000-0000000000fb", "abandoned-usage-only"),
    "metrics": ("01900000-0000-7000-8000-0000000000e0", "abandoned-metrics"),
    "intent": ("01900000-0000-7000-8000-0000000000f6", "abandoned-intent"),
    "abandoned_metrics": ("01900000-0000-7000-8000-0000000000f7", "abandoned-metrics-2"),
    "gate": ("01900000-0000-7000-8000-0000000000e9", "abandoned-gate"),
}


def _tid(name: str) -> str:
    return TENANTS[name][0]


def _run(coro):
    return asyncio.run(coro, loop_factory=asyncio.SelectorEventLoop)


def _seed_tenants() -> None:
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        for tid, slug in TENANTS.values():
            conn.execute(
                text(
                    "INSERT INTO tenants (id, slug, name, status) VALUES "
                    "(:id, :slug, 'Abandoned', 'active') ON CONFLICT (slug) DO NOTHING"
                ),
                {"id": tid, "slug": slug},
            )
    admin.dispose()


def _clear() -> None:
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        for tid, _slug in TENANTS.values():
            conn.execute(
                text(
                    "DELETE FROM citations WHERE agent_run_id IN "
                    "(SELECT id FROM agent_runs WHERE tenant_id = :t)"
                ),
                {"t": tid},
            )
            conn.execute(text("DELETE FROM agent_runs WHERE tenant_id = :t"), {"t": tid})
        conn.execute(
            text("DELETE FROM tenants WHERE slug LIKE 'abandoned-%'"),
        )
    admin.dispose()


@pytest.fixture(scope="module", autouse=True)
def seeded() -> None:
    _seed_tenants()
    yield
    _clear()


async def _session(tenant: str):
    from sqlalchemy import text as sa_text

    from platform_core.db import create_engine as async_engine

    engine = async_engine(APP_URL)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    session = factory()
    await session.execute(sa_text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant})
    return session


def _add_run(
    *,
    tenant: str,
    status: str = "queued",
    executed: bool = False,
    route: str = "knowledge_qa",
    started_at: int | None = None,
    model_config: str = '{"model": "m", "intent": {"scene": "order", "business_line": "pcb"}}',
) -> uuid.UUID:
    """Insert one run. `executed=False` gives the queue-placeholder shape."""
    row_id = default_uuid()
    ts = int(time.time()) if started_at is None else started_at

    async def _inner():
        session = await _session(tenant)
        try:
            await session.execute(
                text(
                    "INSERT INTO agent_runs (id, tenant_id, conversation_ref_id, route, status, "
                    "model_config, retrieval_config, policy_version, code_version, trace_id, "
                    "input_hash, token_usage, latency_ms, abstain_reason, started_at) VALUES "
                    "(:id, :t, :ref, :route, :status, "
                    "CAST(:mc AS jsonb), '{}'::jsonb, 'v1', '0.1.0', '', :hash, "
                    "'{}'::jsonb, NULL, NULL, :started)"
                ),
                {
                    "id": row_id,
                    "t": tenant,
                    "ref": default_uuid(),
                    "route": route,
                    "status": status,
                    # An executed run always has a question hash; a placeholder
                    # never does. That is the whole distinction.
                    "hash": "a" * 64 if executed else "",
                    "mc": model_config if executed else '{"mode": "customer_reply"}',
                    "started": ts,
                },
            )
            await session.commit()
        finally:
            await session.close()

    _run(_inner())
    return row_id


def _usage(tenant: str):
    from platform_core.identity.usage import usage_snapshot

    async def _inner():
        session = await _session(tenant)
        try:
            return await usage_snapshot(session, tenant_id=uuid.UUID(tenant))
        finally:
            await session.close()

    return _run(_inner())


def _metrics(tenant: str, window_seconds: int = 86400):
    from platform_core.evaluation.metrics import aggregate_quality_metrics

    async def _inner():
        session = await _session(tenant)
        try:
            return await aggregate_quality_metrics(
                session, tenant_id=uuid.UUID(tenant), window_seconds=window_seconds
            )
        finally:
            await session.close()

    return _run(_inner())


def _intents(tenant: str, window_seconds: int = 86400):
    from platform_core.evaluation.metrics import aggregate_intent_distribution

    async def _inner():
        session = await _session(tenant)
        try:
            return await aggregate_intent_distribution(
                session, tenant_id=uuid.UUID(tenant), window_seconds=window_seconds
            )
        finally:
            await session.close()

    return _run(_inner())


def _sweep(
    *, tenant: str, older_than_seconds: int = 3600, now: int | None = None, batch: int = 500
):
    from platform_core.agent_runtime.abandoned import abandon_stale_placeholders

    ts = int(time.time()) if now is None else now

    async def _inner():
        session = await _session(tenant)
        try:
            closed = await abandon_stale_placeholders(
                session,
                tenant_id=uuid.UUID(tenant),
                older_than_seconds=older_than_seconds,
                now=ts,
                batch=batch,
            )
            await session.commit()
            return closed
        finally:
            await session.close()

    return _run(_inner())


def _sweep_now(*, tenant: str, batch: int = 500) -> int:
    """Sweep as if a second had passed.

    `older_than_seconds=0` evaluates to "age strictly greater than zero", and
    the age is a whole number of seconds, so a row written in the same second
    as the sweep does not match. Passing `now` one second ahead is what makes
    "everything stale" mean what the test intends rather than nothing at all.
    """
    return _sweep(tenant=tenant, older_than_seconds=0, now=int(time.time()) + 1, batch=batch)


def _status_of(run_id: uuid.UUID) -> str:
    admin = create_engine(ADMIN_URL)
    try:
        with admin.begin() as conn:
            return conn.execute(
                text("SELECT status FROM agent_runs WHERE id = :id"), {"id": run_id}
            ).scalar_one()
    finally:
        admin.dispose()


# --- 1. Admission control ---------------------------------------------------


def test_a_freshly_queued_run_still_counts_towards_usage() -> None:
    """The invariant the whole design turns on.

    A run that was just accepted has not executed yet, and it must count: the
    429 gate reads this number, so not counting it admits an unbounded burst.
    """
    tenant = _tid("usage")
    before = _usage(tenant).runs_used
    _add_run(tenant=tenant, status="queued", executed=False)

    usage = _usage(tenant)
    assert usage.runs_used == before + 1
    assert usage.abandoned == 0


def test_an_abandoned_run_stops_counting_and_is_reported() -> None:
    """The inflation this closes, and the visible statement of the exclusion."""
    tenant = _tid("usage_only")
    run_id = _add_run(tenant=tenant, status="queued", executed=False)
    assert _usage(tenant).runs_used == 1

    assert _sweep_now(tenant=tenant) == 1
    assert _status_of(run_id) == "abandoned"

    usage = _usage(tenant)
    assert usage.runs_used == 0, "an abandoned run is not usage"
    assert usage.abandoned == 1, "and the exclusion is stated, not silent"


def test_an_executed_run_is_unaffected_by_the_sweep() -> None:
    """`abandoned` is for rows that never ran, not for rows without a status."""
    tenant = _tid("usage")
    run_id = _add_run(tenant=tenant, status="queued", executed=True)
    _sweep_now(tenant=tenant)
    assert _status_of(run_id) == "queued"


# --- 2. What the sweep may touch -------------------------------------------


def test_the_sweep_leaves_a_recent_placeholder_alone() -> None:
    """It must not race a worker that is merely slow.

    Closing a run that was about to execute would lose the customer's answer
    and release its quota reservation while the work still happens.
    """
    tenant = _tid("usage")
    run_id = _add_run(
        tenant=tenant, status="queued", executed=False, started_at=int(time.time()) - 30
    )
    _sweep(tenant=tenant, older_than_seconds=3600)
    assert _status_of(run_id) == "queued"


def test_the_sweep_leaves_a_timeless_row_alone() -> None:
    """No `started_at`, no age, no judgement.

    A sweep that guesses which timeless rows are old is how it closes
    something that was about to run.
    """
    tenant = _tid("usage")
    run_id = _add_run(tenant=tenant, status="queued", executed=False)
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        conn.execute(text("UPDATE agent_runs SET started_at = NULL WHERE id = :id"), {"id": run_id})
    admin.dispose()

    _sweep_now(tenant=tenant)
    assert _status_of(run_id) == "queued"


def test_the_sweep_does_not_reach_another_tenant() -> None:
    """RLS is the boundary, asserted rather than assumed."""
    theirs = _add_run(tenant=_tid("gate"), status="queued", executed=False)

    _sweep_now(tenant=_tid("usage"))
    assert _status_of(theirs) == "queued"

    assert _sweep_now(tenant=_tid("gate")) >= 1
    assert _status_of(theirs) == "abandoned"


def test_the_sweep_is_bounded_and_idempotent() -> None:
    """Paged, so a large backlog drains over cycles without a long lock."""
    tenant = _tid("usage")
    for _ in range(3):
        _add_run(tenant=tenant, status="queued", executed=False, started_at=1_600_000_000)

    assert _sweep(tenant=tenant, older_than_seconds=3600, batch=2) == 2
    assert _sweep(tenant=tenant, older_than_seconds=3600, batch=2) == 1
    # Nothing left that matches, so a further pass changes nothing.
    assert _sweep(tenant=tenant, older_than_seconds=3600, batch=2) == 0


# --- 3. The quality surfaces ------------------------------------------------


def test_quality_metrics_exclude_placeholders_and_report_them() -> None:
    """Route counts must not report traffic that never happened.

    The placeholder is written with `route="knowledge_qa"` because the queue
    endpoint has to put something there - so counting it invented
    `knowledge_qa` runs, and every rate's denominator with them.
    """
    tenant = _tid("metrics")
    _add_run(tenant=tenant, status="queued", executed=False)

    metrics = _metrics(tenant)
    assert metrics.total_runs == 0
    assert metrics.route_counts == {}
    assert metrics.never_executed_runs == 1

    _add_run(tenant=tenant, status="completed", executed=True)
    metrics = _metrics(tenant)
    assert metrics.total_runs == 1
    assert metrics.route_counts == {"knowledge_qa": 1}
    assert metrics.never_executed_runs == 1


def test_intent_distribution_does_not_pollute_the_unrecorded_bucket() -> None:
    """`unrecorded` means "predates the field", not "plus never ran"."""
    tenant = _tid("intent")
    _add_run(tenant=tenant, status="queued", executed=False)

    dist = _intents(tenant)
    assert dist.total_runs == 0
    assert dist.by_business_line == {}
    assert dist.by_scene == {}
    assert dist.never_executed_runs == 1


def test_an_abandoned_run_is_equally_excluded_from_the_metrics() -> None:
    """Both states of never-executed are excluded, not just the pending one."""
    tenant = _tid("abandoned_metrics")
    _add_run(tenant=tenant, status="queued", executed=False)
    _sweep_now(tenant=tenant)

    metrics = _metrics(tenant)
    assert metrics.total_runs == 0
    assert metrics.never_executed_runs == 1


# --- 4. The gate (HTTP) -----------------------------------------------------


def _client(tenant_id: str, role: str) -> TestClient:
    import importlib

    main_mod = importlib.import_module("platform_core.main")
    fresh = FastAPI()
    for route in main_mod.app.router.routes:
        fresh.router.routes.append(route)

    actor_id = uuid.uuid5(uuid.NAMESPACE_URL, f"abandoned:{tenant_id}-{role}")

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
    headers = {"Authorization": "Bearer pt_bootstrap_test"}
    if idem:
        headers["Idempotency-Key"] = idem
    return headers


def _queue(client: TestClient):
    return client.post(
        f"/v1/conversations/{uuid.uuid4()}/agent-runs",
        headers=_headers(str(uuid.uuid4())),
        json={"trigger_message_ref": "m-abandoned"},
    )


def test_the_admission_gate_counts_a_run_that_is_only_queued() -> None:
    """The regression guard for the tempting wrong fix.

    Excluding rows without a question hash also excludes everything sitting in
    the queue - and then nothing refuses the next request. Here the quota is
    set to exactly what has been consumed, so the following request must be a
    429. If this test fails, admission control has been disarmed.
    """
    tenant = _tid("gate")
    owner = _client(tenant, "tenant_owner")
    queued = _queue(_client(tenant, "support_admin"))
    assert queued.status_code == 200, queued.text

    usage = _client(tenant, "support_agent").get("/v1/tenant/usage", headers=_headers())
    assert usage.status_code == 200, usage.text
    used = usage.json()["usage"]["runs_used"]
    assert used >= 1, "a run that was just accepted must be visible to the gate"

    set_quota = owner.put(
        "/v1/tenant/quota",
        headers=_headers(str(uuid.uuid4())),
        json={"monthly_run_quota": used},
    )
    assert set_quota.status_code == 200, set_quota.text

    refused = _queue(_client(tenant, "support_admin"))
    assert refused.status_code == 429, refused.text
    assert refused.json()["error"]["code"] == "QUOTA_EXCEEDED"


def test_the_usage_endpoint_reports_the_abandoned_count() -> None:
    """The number is on the wire, not only in the dataclass."""
    resp = _client(_tid("gate"), "support_agent").get("/v1/tenant/usage", headers=_headers())
    assert resp.status_code == 200, resp.text
    assert "abandoned" in resp.json()["usage"]
