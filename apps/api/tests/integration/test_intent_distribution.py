"""Integration: feature list 8.7 - intent distribution and trend.

Against real Postgres because the claim is about aggregation over rows, and
about the two failure modes that make a distribution useless:

- Runs classified before an axis existed must be counted as `unrecorded`, not
  dropped. A dashboard that omits what it could not measure looks complete
  while hiding the gap - the same reason `aggregate_quality_metrics` reports
  its untimed rows instead of ignoring them.
- The trend must be bucketed by time, because the whole value of a trend is
  direction; a single total cannot show it.
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import async_sessionmaker

pytestmark = pytest.mark.integration

ADMIN_URL = os.environ.get(
    "APP_ADMIN_DATABASE_URL",
    "postgresql+psycopg://platform:platform@localhost:5435/platform",
)
APP_URL = "postgresql+psycopg://platform_app:platform_app@localhost:5435/platform"

TENANT = "01900000-0000-7000-8000-0000000000d8"
SLUG = "agent-intent-distribution"

DAY = 86400


def _run(coro):
    return asyncio.run(coro, loop_factory=asyncio.SelectorEventLoop)


def _insert_runs(rows: list[tuple[int, str, dict | None]]) -> None:
    """(started_at, route, intent_snapshot) -> one AgentRun each.

    `input_hash` is populated because the row is `completed`: the orchestrator
    writes the question's hash the moment execution begins, so a completed run
    with an empty one cannot occur. Seeding `''` also made this fixture depend
    on the bug where runs that never executed were aggregated as if they had -
    they are excluded now, and this fixture has to look like a real run.
    """
    import hashlib
    import json

    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        for index, (started_at, route, intent) in enumerate(rows):
            payload = {"intent": intent} if intent is not None else {}
            conn.execute(
                text(
                    "INSERT INTO agent_runs (id, tenant_id, conversation_ref_id, route, "
                    "status, model_config, retrieval_config, trace_id, input_hash, "
                    "policy_version, code_version, started_at) VALUES "
                    "(:id, :t, :conv, :route, 'completed', CAST(:cfg AS jsonb), '{}', "
                    "'', :hash, 'v1', 'dev', :started)"
                ),
                {
                    "id": str(uuid.uuid4()),
                    "t": TENANT,
                    "conv": str(uuid.uuid4()),
                    "route": route,
                    "cfg": json.dumps(payload),
                    "hash": hashlib.sha256(f"{index}:{route}:{started_at}".encode()).hexdigest(),
                    "started": started_at,
                },
            )
    admin.dispose()


def _clear() -> None:
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        conn.execute(text("DELETE FROM agent_runs WHERE tenant_id = :t"), {"t": TENANT})
        conn.execute(text("DELETE FROM tenants WHERE slug = :slug"), {"slug": SLUG})
    admin.dispose()


def _seed() -> None:
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO tenants (id, slug, name, status) VALUES "
                "(:id, :slug, 'Intent Distribution', 'active') ON CONFLICT (slug) DO NOTHING"
            ),
            {"id": TENANT, "slug": SLUG},
        )
    admin.dispose()


async def _distribute(window_seconds: int, bucket_seconds: int):
    from sqlalchemy import text as sa_text

    from platform_core.db import create_engine as async_engine
    from platform_core.evaluation.metrics import aggregate_intent_distribution

    engine = async_engine(APP_URL)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        await session.execute(
            sa_text("SELECT set_config('app.tenant_id', :t, true)"), {"t": TENANT}
        )
        return await aggregate_intent_distribution(
            session,
            tenant_id=uuid.UUID(TENANT),
            window_seconds=window_seconds,
            bucket_seconds=bucket_seconds,
        )


@pytest.fixture(autouse=True)
def tenant() -> None:
    _clear()
    _seed()
    yield
    _clear()


def _intent(scene: str, kind: str, line: str) -> dict:
    return {
        "scene": scene,
        "primary_kind": kind,
        "secondary_kinds": [],
        "route": "knowledge_qa",
        "action": "answer_from_knowledge",
        "confidence": 0.9,
        "multi_intent": False,
        "business_line": line,
    }


def test_counts_each_intent_axis() -> None:
    now = int(time.time())
    _insert_runs(
        [
            (now - 60, "knowledge_qa", _intent("pre_sales", "knowledge_question", "pcb")),
            (now - 120, "knowledge_qa", _intent("pre_sales", "sales_inquiry", "pcb")),
            (now - 180, "business_read", _intent("order_fulfilment", "business_query", "smt")),
        ]
    )
    dist = _run(_distribute(window_seconds=DAY, bucket_seconds=3600))
    assert dist.total_runs == 3
    assert dist.by_business_line == {"pcb": 2, "smt": 1}
    assert dist.by_scene == {"pre_sales": 2, "order_fulfilment": 1}
    assert dist.by_kind == {"knowledge_question": 1, "sales_inquiry": 1, "business_query": 1}


def test_runs_without_a_recorded_intent_are_visible_not_dropped() -> None:
    """A window that is only partly measured must look partly measured."""
    now = int(time.time())
    _insert_runs(
        [
            (now - 60, "knowledge_qa", _intent("pre_sales", "knowledge_question", "pcb")),
            (now - 120, "knowledge_qa", None),
        ]
    )
    dist = _run(_distribute(window_seconds=DAY, bucket_seconds=3600))
    assert dist.total_runs == 2
    assert dist.by_business_line.get("unrecorded") == 1
    assert dist.by_business_line.get("pcb") == 1


def test_runs_older_than_the_window_are_excluded() -> None:
    now = int(time.time())
    _insert_runs(
        [
            (now - 60, "knowledge_qa", _intent("pre_sales", "knowledge_question", "pcb")),
            (now - 10 * DAY, "knowledge_qa", _intent("pre_sales", "knowledge_question", "smt")),
        ]
    )
    dist = _run(_distribute(window_seconds=DAY, bucket_seconds=3600))
    assert dist.total_runs == 1
    assert "smt" not in dist.by_business_line


def test_trend_is_bucketed_so_direction_is_visible() -> None:
    """Three in one bucket, one two buckets later: visible as a change."""
    now = int(time.time())
    bucket = 3600
    base = (now // bucket) * bucket
    _insert_runs(
        [
            (base - 3 * bucket + 10, "knowledge_qa", _intent("pre_sales", "sales_inquiry", "pcb")),
            (base - 3 * bucket + 20, "knowledge_qa", _intent("pre_sales", "sales_inquiry", "pcb")),
            (base - 3 * bucket + 30, "knowledge_qa", _intent("pre_sales", "sales_inquiry", "pcb")),
            (base - bucket + 10, "knowledge_qa", _intent("pre_sales", "sales_inquiry", "smt")),
        ]
    )
    dist = _run(_distribute(window_seconds=DAY, bucket_seconds=bucket))
    totals = [bucket_.total for bucket_ in dist.trend]
    assert totals == [3, 1]
    assert dist.trend[0].by_business_line == {"pcb": 3}
    assert dist.trend[1].by_business_line == {"smt": 1}


def test_trend_is_ordered_oldest_first() -> None:
    now = int(time.time())
    bucket = 3600
    base = (now // bucket) * bucket
    _insert_runs(
        [
            (base - bucket + 5, "knowledge_qa", _intent("pre_sales", "sales_inquiry", "pcb")),
            (base - 3 * bucket + 5, "knowledge_qa", _intent("pre_sales", "sales_inquiry", "pcb")),
        ]
    )
    dist = _run(_distribute(window_seconds=DAY, bucket_seconds=bucket))
    starts = [bucket_.bucket_start for bucket_ in dist.trend]
    assert starts == sorted(starts)
