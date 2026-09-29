"""Integration: issue categories against a real Postgres with RLS on.

The unit tests cover the derivation, the strict definition and the state
machine. What needs a database is what the unit tests cannot answer:

1. **RLS actually holds** — a category marked by tenant A is invisible to
   tenant B's app-role session, and a write with the wrong tenant binding does
   not land. A dashboard returning *counts* is the classic place a leak hides,
   because an unbound query does not look wrong, it just returns a bigger number.
2. **The decision survives** — mark, reopen a session, read it back.
3. **The rate is computed from real runs**, including the follow-up rule, so the
   number on the dashboard is the number the definition says it is.
"""

from __future__ import annotations

import os
import time
import uuid

import pytest
from sqlalchemy import create_engine as _admin_engine
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from platform_core.db import create_engine

pytestmark = pytest.mark.integration

ADMIN_URL = os.environ.get(
    "APP_ADMIN_DATABASE_URL",
    "postgresql+psycopg://platform:platform@localhost:5435/platform",
)
APP_URL = os.environ.get(
    "APP_TEST_DATABASE_URL",
    "postgresql+psycopg://platform_app:platform_app@localhost:5435/platform",
)

_NS = uuid.uuid5(uuid.NAMESPACE_URL, "b2b-ai-support/tests/issue-categories")
TENANT = str(uuid.uuid5(_NS, "tenant"))
TENANT_OTHER = str(uuid.uuid5(_NS, "tenant-other"))

# The demo story's category: 加急咨询 on the PCB line.
KEY = "pcb|order|expedite"
INTENT = {"business_line": "pcb", "scene": "order", "primary_kind": "expedite"}


def _run(coro):
    import asyncio

    return asyncio.run(coro, loop_factory=asyncio.SelectorEventLoop)


@pytest.fixture(autouse=True)
def _clean():
    yield
    admin = _admin_engine(ADMIN_URL)
    with admin.begin() as conn:
        for table in ("issue_categories", "citations", "agent_runs"):
            conn.execute(
                text(f"DELETE FROM {table} WHERE tenant_id IN (:a, :b)"),  # noqa: S608
                {"a": TENANT, "b": TENANT_OTHER},
            )
    admin.dispose()


@pytest.fixture(scope="module", autouse=True)
def _tenants():
    admin = _admin_engine(ADMIN_URL)
    with admin.begin() as conn:
        for tid, slug in ((TENANT, "cat-a"), (TENANT_OTHER, "cat-b")):
            conn.execute(
                text(
                    "INSERT INTO tenants (id, slug, name, status) VALUES "
                    "(:id, :slug, :slug, 'active') ON CONFLICT (slug) DO NOTHING"
                ),
                {"id": tid, "slug": slug},
            )
    yield
    with admin.begin() as conn:
        for table in ("issue_categories", "citations", "agent_runs"):
            conn.execute(
                text(
                    f"DELETE FROM {table} WHERE tenant_id IN "  # noqa: S608
                    "(SELECT id FROM tenants WHERE slug LIKE 'cat-%')"
                )
            )
        conn.execute(text("DELETE FROM tenants WHERE slug LIKE 'cat-%'"))
    admin.dispose()


def _seed_run(
    *,
    tenant: str,
    conversation: uuid.UUID,
    status: str,
    started_at: int,
    abstain_reason: str | None = None,
    intent: dict | None = None,
) -> None:
    from platform_core.agent_runtime.models import AgentRun

    admin = _admin_engine(ADMIN_URL)
    with admin.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO agent_runs (id, tenant_id, conversation_ref_id, route, "
                "status, started_at, model_config, retrieval_config, policy_version, "
                "code_version, trace_id, input_hash, token_usage, abstain_reason) VALUES "
                "(:id, :t, :c, 'knowledge_qa', :s, :started, CAST(:cfg AS jsonb), "
                "CAST('{}' AS jsonb), 'v1', 'test', 'tr', 'h', CAST('{}' AS jsonb), :reason)"
            ),
            {
                "id": str(uuid.uuid4()),
                "t": tenant,
                "c": str(conversation),
                "s": status,
                "started": started_at,
                "cfg": __import__("json").dumps({"intent": intent or INTENT}),
                "reason": abstain_reason,
            },
        )
    _ = AgentRun
    admin.dispose()


async def _with_session(tenant: str, fn):
    engine = create_engine(APP_URL)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            await session.execute(
                text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant}
            )
            result = await fn(session)
            await session.commit()
            return result
    finally:
        await engine.dispose()


def test_the_dashboard_reports_what_the_definition_says() -> None:
    """Two runs, one answered and one that had to be handed off.

    The rate is 0.5 and the gap is attributed to `content`, and both come from
    the run rows rather than from anything stored on the category.
    """
    from platform_core.evaluation.categories import category_report

    now = int(time.time())
    _seed_run(tenant=TENANT, conversation=uuid.uuid4(), status="completed", started_at=now - 60)
    _seed_run(
        tenant=TENANT,
        conversation=uuid.uuid4(),
        status="abstained",
        started_at=now - 50,
        abstain_reason="NO_AUTHORIZED_EVIDENCE",
    )

    report = _run(_with_session(TENANT, lambda s: category_report(s, tenant_id=uuid.UUID(TENANT))))

    stat = next(c for c in report.categories if c.category_key == KEY)
    assert stat.runs == 2
    assert stat.automated == 1
    assert stat.escalated == 1
    assert stat.automation_rate == 0.5
    assert stat.fix_type_counts == {"content": 1}
    assert stat.leak_volume == 1
    # Proposed, because a document can close it; not yet marked, so no state.
    assert stat.state == "observed"
    assert [c.category_key for c in report.candidates] == [KEY]


def test_a_repeat_question_is_not_counted_as_two_automations() -> None:
    """The same category asked twice in one conversation: one automation at most."""
    from platform_core.evaluation.categories import category_report

    now = int(time.time())
    conversation = uuid.uuid4()
    _seed_run(tenant=TENANT, conversation=conversation, status="completed", started_at=now - 200)
    _seed_run(tenant=TENANT, conversation=conversation, status="completed", started_at=now - 100)

    report = _run(_with_session(TENANT, lambda s: category_report(s, tenant_id=uuid.UUID(TENANT))))
    stat = next(c for c in report.categories if c.category_key == KEY)

    assert stat.runs == 2
    # The platform spoke twice; the customer had to ask twice. One of those is
    # a resolution.
    assert stat.automated == 1
    assert stat.automation_rate == 0.5


def test_a_marked_decision_survives_and_keeps_the_category_off_the_list() -> None:
    from platform_core.evaluation.categories import category_report
    from platform_core.evaluation.category_service import set_category_state

    now = int(time.time())
    _seed_run(
        tenant=TENANT,
        conversation=uuid.uuid4(),
        status="abstained",
        started_at=now - 50,
        abstain_reason="COMPLAINT_REQUIRES_HUMAN",
    )

    async def mark(session):
        return await set_category_state(
            session,
            tenant_id=uuid.UUID(TENANT),
            category_key=KEY,
            state="human_only",
            actor_id=uuid.uuid4(),
            fix_type="routing",
            note="complaints go to a person",
        )

    row = _run(_with_session(TENANT, mark))
    assert row.state == "human_only"

    report = _run(_with_session(TENANT, lambda s: category_report(s, tenant_id=uuid.UUID(TENANT))))
    stat = next(c for c in report.categories if c.category_key == KEY)
    assert stat.state == "human_only"
    assert stat.confirmed_fix_type == "routing"
    # Marked as a human decision, so it must not be proposed for automation.
    assert stat.category_key not in [c.category_key for c in report.candidates]


def test_another_tenant_sees_no_categories() -> None:
    """RLS, not just the tenant filter: an unbound query returns more, not less."""
    from platform_core.evaluation.categories import category_report

    now = int(time.time())
    _seed_run(tenant=TENANT, conversation=uuid.uuid4(), status="completed", started_at=now - 60)

    report = _run(
        _with_session(TENANT_OTHER, lambda s: category_report(s, tenant_id=uuid.UUID(TENANT_OTHER)))
    )
    assert report.categories == []
    assert report.automation_rate is None


def test_an_illegal_transition_is_refused_rather_than_normalised() -> None:
    from platform_core.evaluation.category_service import CategoryStateError, set_category_state

    async def mark(session):
        with pytest.raises(CategoryStateError, match="before it was worked on"):
            await set_category_state(
                session,
                tenant_id=uuid.UUID(TENANT),
                category_key=KEY,
                state="automated",
                actor_id=uuid.uuid4(),
            )

    _run(_with_session(TENANT, mark))


def test_automating_a_category_stamps_a_baseline_the_report_measures_from() -> None:
    """The before/after pair is what closes the loop with a number."""
    from platform_core.evaluation.categories import category_report
    from platform_core.evaluation.category_service import set_category_state

    now = int(time.time())
    # Before the automation: asked, and handed off.
    _seed_run(
        tenant=TENANT,
        conversation=uuid.uuid4(),
        status="abstained",
        started_at=now - 4000,
        abstain_reason="NO_AUTHORIZED_EVIDENCE",
    )

    async def mark(session):
        await set_category_state(
            session,
            tenant_id=uuid.UUID(TENANT),
            category_key=KEY,
            state="candidate",
            actor_id=uuid.uuid4(),
            fix_type="content",
        )
        return await set_category_state(
            session,
            tenant_id=uuid.UUID(TENANT),
            category_key=KEY,
            state="automating",
            actor_id=uuid.uuid4(),
        )

    _run(_with_session(TENANT, mark))

    async def promote(session):
        return await set_category_state(
            session,
            tenant_id=uuid.UUID(TENANT),
            category_key=KEY,
            state="automated",
            actor_id=uuid.uuid4(),
        )

    row = _run(_with_session(TENANT, promote))
    assert row.automated_at is not None

    # After: answered, without a re-ask.
    #
    # Timestamped relative to `automated_at`, not to `now`. `now` was captured
    # *before* the promote, so in a slow run the "after" run landed before the
    # automation and the report counted both runs as the baseline - passing in
    # isolation (same second) and failing in a full run. The comparison is
    # measured from `automated_at`, so the fixture has to be too.
    _seed_run(
        tenant=TENANT,
        conversation=uuid.uuid4(),
        status="completed",
        started_at=int(row.automated_at) + 1,
    )

    report = _run(
        _with_session(
            TENANT,
            lambda s: category_report(s, tenant_id=uuid.UUID(TENANT), window_seconds=86400),
        )
    )
    stat = next(c for c in report.categories if c.category_key == KEY)
    assert stat.state == "automated"
    # 0% before, 100% after - the loop closed with two numbers rather than a
    # claim.
    assert stat.rate_before == 0.0
    assert stat.rate_after == 1.0
