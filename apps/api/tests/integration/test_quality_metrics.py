"""Integration tests: quality metrics aggregation (ticket 35).

This suite exists because `aggregate_quality_metrics` had **no callers and no
test**, which let it filter on `AgentRun.created_at` - a column that does not
exist on the model or in the schema. Every call would have raised
`UndefinedColumn`. The first test below is the one that would have caught it.

The rest pin the parts that are easy to get subtly wrong: the trailing
window must exclude old runs, RLS must bound the count to the caller's
tenant, and rows without a timestamp must be reported rather than silently
counted.
"""

import os
import time
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from platform_core.evaluation.metrics import QualityMetrics, aggregate_quality_metrics

pytestmark = pytest.mark.integration

ADMIN_URL = os.environ.get(
    "APP_ADMIN_DATABASE_URL",
    "postgresql+psycopg://platform:platform@localhost:5435/platform",
)
APP_URL = "postgresql+psycopg://platform_app:platform_app@localhost:5435/platform"

TENANT_A = "0190a000-0000-7000-8000-0000000000a1"
TENANT_B = "0190a000-0000-7000-8000-0000000000b1"
CONV = "0190a000-0000-7000-8000-0000000000c1"

# Distinguishes "use the current time" from an explicit NULL, which the
# untimed-legacy-row test needs to insert.
_UNSET = object()

_RUN_INSERT = (
    "INSERT INTO agent_runs "
    "(id, tenant_id, conversation_ref_id, route, status, latency_ms, started_at) "
    "VALUES (:id, :t, :conv, :route, :status, :latency, :started)"
)


def _run(coro):
    import asyncio

    return asyncio.run(coro, loop_factory=asyncio.SelectorEventLoop)


def _new_run(
    *,
    tenant: str = TENANT_A,
    route: str = "knowledge_qa",
    status: str = "completed",
    latency: int | None = 100,
    started_at: int | None | object = _UNSET,
) -> dict:
    return {
        "id": str(uuid.uuid4()),
        "t": tenant,
        "conv": CONV,
        "route": route,
        "status": status,
        "latency": latency,
        "started": int(time.time()) if started_at is _UNSET else started_at,
    }


@pytest.fixture(scope="module", autouse=True)
def seed_tenants() -> None:
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        for tid, slug in ((TENANT_A, "metrics-a"), (TENANT_B, "metrics-b")):
            conn.execute(
                text(
                    "INSERT INTO tenants (id, slug, name, status) VALUES "
                    "(:id, :slug, :name, 'active') ON CONFLICT (slug) DO NOTHING"
                ),
                {"id": tid, "slug": slug, "name": slug},
            )
    yield
    with admin.begin() as conn:
        conn.execute(
            text("DELETE FROM citations WHERE tenant_id IN (:a, :b)"),
            {"a": TENANT_A, "b": TENANT_B},
        )
        conn.execute(
            text("DELETE FROM agent_runs WHERE tenant_id IN (:a, :b)"),
            {"a": TENANT_A, "b": TENANT_B},
        )
        conn.execute(text("DELETE FROM tenants WHERE slug IN ('metrics-a', 'metrics-b')"))
    admin.dispose()


@pytest.fixture
def clean_runs() -> None:
    """Each test starts from an empty agent_runs for both tenants."""
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        _clear_runs(conn)
    yield
    with admin.begin() as conn:
        _clear_runs(conn)
    admin.dispose()


def _clear_runs(conn) -> None:
    """Citations reference agent_runs, so they must go first."""
    conn.execute(
        text("DELETE FROM citations WHERE tenant_id IN (:a, :b)"),
        {"a": TENANT_A, "b": TENANT_B},
    )
    conn.execute(
        text("DELETE FROM agent_runs WHERE tenant_id IN (:a, :b)"),
        {"a": TENANT_A, "b": TENANT_B},
    )


def _insert_events(*rows: dict) -> None:
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        for row in rows:
            conn.execute(text(_RUN_INSERT), row)
    admin.dispose()


async def _aggregate(tenant: str = TENANT_A, *, window_seconds: int = 3600) -> QualityMetrics:
    from platform_core.db import create_engine as app_engine

    engine = app_engine(APP_URL)
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            await session.execute(
                text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant}
            )
            return await aggregate_quality_metrics(
                session, tenant_id=uuid.UUID(tenant), window_seconds=window_seconds
            )
    finally:
        await engine.dispose()


# --- The regression this suite was written for ---


def test_aggregation_query_executes_at_all(clean_runs: None) -> None:
    """The original bug: the window filtered `AgentRun.created_at`, which
    does not exist, so *any* call raised UndefinedColumn. A test that merely
    runs the aggregator is therefore a real regression test, not a tautology.
    """
    metrics = _run(_aggregate())
    assert metrics.total_runs == 0
    assert metrics.latency_p50_ms is None


def test_started_at_column_exists_in_schema() -> None:
    """Guards the contract the code depends on, independently of the ORM."""
    admin = create_engine(ADMIN_URL)
    try:
        with admin.connect() as conn:
            count = conn.execute(
                text(
                    "SELECT count(*) FROM information_schema.columns "
                    "WHERE table_name = 'agent_runs' AND column_name = 'started_at'"
                )
            ).scalar_one()
        assert count == 1, "agent_runs.started_at is missing; run alembic upgrade head"
    finally:
        admin.dispose()


# --- Window semantics ---


def test_counts_runs_inside_the_window(clean_runs: None) -> None:
    now = int(time.time())
    _insert_events(
        _new_run(status="completed", started_at=now - 10),
        _new_run(status="abstained", started_at=now - 20),
        _new_run(status="handed_off", started_at=now - 30),
        _new_run(status="failed", started_at=now - 40),
    )
    metrics = _run(_aggregate(window_seconds=3600))
    assert metrics.total_runs == 4
    assert (metrics.completed, metrics.abstained, metrics.handed_off, metrics.failed) == (
        1,
        1,
        1,
        1,
    )


def test_excludes_runs_older_than_the_window(clean_runs: None) -> None:
    """A dashboard window that leaks old runs reports the past as current."""
    now = int(time.time())
    _insert_events(
        _new_run(started_at=now - 10),
        _new_run(started_at=now - 7200),  # 2h ago, outside a 1h window
    )
    metrics = _run(_aggregate(window_seconds=3600))
    assert metrics.total_runs == 1


def test_untimed_legacy_rows_are_reported_not_counted(clean_runs: None) -> None:
    """Rows written before migration 0012 have no `started_at`. They must not
    be assigned `now` (which would inflate an incident dashboard), and the
    gap must be visible rather than silent."""
    now = int(time.time())
    _insert_events(_new_run(started_at=now - 10), _new_run(started_at=None))
    metrics = _run(_aggregate(window_seconds=3600))
    assert metrics.total_runs == 1, "an untimed row must not enter the window"
    assert metrics.untimed_runs == 1, "but it must be visible to an operator"


# --- Derived ratios ---


def test_abstention_and_handoff_rates_are_ratios_of_all_runs(clean_runs: None) -> None:
    now = int(time.time())
    _insert_events(
        _new_run(status="abstained", started_at=now - 5),
        _new_run(status="handed_off", started_at=now - 6),
        _new_run(status="completed", started_at=now - 7),
        _new_run(status="completed", started_at=now - 8),
    )
    metrics = _run(_aggregate())
    assert metrics.abstention_rate == 0.25
    assert metrics.handoff_rate == 0.25


def test_citation_coverage_is_over_completed_runs_only(clean_runs: None) -> None:
    """An abstention has no citations by design; counting it as a coverage
    failure would make correct abstention look like a quality regression."""
    now = int(time.time())
    completed = _new_run(status="completed", started_at=now - 5)
    _insert_events(completed, _new_run(status="abstained", started_at=now - 6))

    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO citations "
                "(id, tenant_id, agent_run_id, document_version_id, chunk_id, "
                " excerpt_hash, source_uri, claim_index, retrieval_score) "
                "VALUES (:id, :t, :run, :dv, :chunk, 'h1', 'minio://eval/refund', 0, 0.9)"
            ),
            {
                "id": str(uuid.uuid4()),
                "t": TENANT_A,
                "run": completed["id"],
                "chunk": str(uuid.uuid4()),
                "dv": str(uuid.uuid4()),
            },
        )
    admin.dispose()

    metrics = _run(_aggregate())
    assert metrics.completed == 1
    assert metrics.citation_coverage == 1.0, "the single completed run is cited"


def test_latency_percentiles_ignore_null_latencies(clean_runs: None) -> None:
    now = int(time.time())
    _insert_events(
        *[_new_run(latency=value, started_at=now - 5) for value in (100, 200, 300, 400)],
        _new_run(latency=None, started_at=now - 6),
    )
    metrics = _run(_aggregate())
    assert metrics.latency_p50_ms is not None
    assert metrics.latency_p50_ms >= 200
    assert metrics.latency_p95_ms is not None
    assert metrics.latency_p95_ms >= metrics.latency_p50_ms


def test_route_distribution_is_counted(clean_runs: None) -> None:
    now = int(time.time())
    _insert_events(
        _new_run(route="knowledge_qa", started_at=now - 5),
        _new_run(route="knowledge_qa", started_at=now - 6),
        _new_run(route="business_read", started_at=now - 7),
    )
    metrics = _run(_aggregate())
    assert metrics.route_counts == {"knowledge_qa": 2, "business_read": 1}


# --- Tenant isolation ---


def test_aggregation_never_counts_another_tenant(clean_runs: None) -> None:
    """The dashboard is the kind of aggregate that leaks across tenants if
    the RLS binding is forgotten, because it returns counts, not rows."""
    now = int(time.time())
    _insert_events(
        _new_run(tenant=TENANT_A, started_at=now - 5),
        *[_new_run(tenant=TENANT_B, started_at=now - 5) for _ in range(3)],
    )

    metrics_a = _run(_aggregate(TENANT_A))
    metrics_b = _run(_aggregate(TENANT_B))

    assert metrics_a.total_runs == 1
    assert metrics_b.total_runs == 3
