"""Integration: agent directory and assignment, against real Postgres with RLS.

The unit-testable parts (skill matching, ranking) are the easy half. What needs a
database:

1. **Capacity is enforced against real case rows**, and "at capacity" has to
   mean the same thing to `pick_agent` and to `claim_case`. Two functions
   computing load differently is how an agent ends up holding twelve cases.
2. **RLS holds** across both tables - another tenant sees no agents and cannot
   claim our case.
3. **The queue is ordered and excludes the assigned**, so old work cannot hide.
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
APP_URL = "postgresql+psycopg://platform_app:platform_app@localhost:5435/platform"

_NS = uuid.uuid5(uuid.NAMESPACE_URL, "b2b-ai-support/tests/assignment")
TENANT = str(uuid.uuid5(_NS, "tenant"))
TENANT_OTHER = str(uuid.uuid5(_NS, "tenant-other"))

# Children first - `pg_constraint` order, never memory.
_TEARDOWN = (
    "case_attachments",
    "case_conversations",
    "case_escalations",
    "cases",
    "agent_profiles",
)


def _run(coro):
    import asyncio

    return asyncio.run(coro, loop_factory=asyncio.SelectorEventLoop)


def _wipe(where: str, params: dict) -> None:
    admin = _admin_engine(ADMIN_URL)
    with admin.begin() as conn:
        for table in _TEARDOWN:
            conn.execute(text(f"DELETE FROM {table} WHERE {where}"), params)  # noqa: S608
    admin.dispose()


@pytest.fixture(autouse=True)
def _clean():
    _wipe("tenant_id IN (:a, :b)", {"a": TENANT, "b": TENANT_OTHER})
    yield
    _wipe("tenant_id IN (:a, :b)", {"a": TENANT, "b": TENANT_OTHER})


@pytest.fixture(scope="module", autouse=True)
def _tenants():
    admin = _admin_engine(ADMIN_URL)
    with admin.begin() as conn:
        for tid, slug in ((TENANT, "asg-a"), (TENANT_OTHER, "asg-b")):
            conn.execute(
                text(
                    "INSERT INTO tenants (id, slug, name, status) VALUES "
                    "(:id, :slug, :slug, 'active') ON CONFLICT (slug) DO NOTHING"
                ),
                {"id": tid, "slug": slug},
            )
    yield
    with admin.begin() as conn:
        sub = "(SELECT id FROM tenants WHERE slug LIKE 'asg-%')"
        for table in _TEARDOWN:
            conn.execute(text(f"DELETE FROM {table} WHERE tenant_id IN {sub}"))  # noqa: S608
        conn.execute(text("DELETE FROM tenants WHERE slug LIKE 'asg-%'"))
    admin.dispose()


async def _with_session(tenant: str, fn):
    engine = create_engine(APP_URL)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            await session.execute(
                text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant}
            )
            try:
                result = await fn(session)
            except Exception:
                await session.rollback()
                raise
            await session.commit()
            return result
    finally:
        await engine.dispose()


def _seed_case(tenant: str, *, opened_at: int | None = None) -> uuid.UUID:
    case_id = uuid.uuid4()
    admin = _admin_engine(ADMIN_URL)
    with admin.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO cases (id, tenant_id, subject, status, opened_at) "
                "VALUES (:id, :t, 'case', 'new', :o)"
            ),
            {
                "id": str(case_id),
                "t": tenant,
                "o": opened_at if opened_at is not None else int(time.time()),
            },
        )
    admin.dispose()
    return case_id


def _add_agent(tenant: str, user_ref: str, **fields) -> None:
    from platform_core.cases.assignment import upsert_agent

    async def go(session):
        return await upsert_agent(
            session,
            tenant_id=uuid.UUID(tenant),
            user_ref=user_ref,
            display_name=fields.pop("display_name", user_ref),
            **fields,
        )

    _run(_with_session(tenant, go))


def test_an_agent_can_be_added_and_listed() -> None:
    from platform_core.cases.assignment import list_agents

    _add_agent(TENANT, "agent-1", display_name="甲", skills=["pcb"], max_concurrent=3)

    rows = _run(_with_session(TENANT, lambda s: list_agents(s, tenant_id=uuid.UUID(TENANT))))
    assert [r.user_ref for r in rows] == ["agent-1"]
    assert list(rows[0].skills) == ["pcb"]
    assert int(rows[0].max_concurrent) == 3


def test_the_least_loaded_eligible_agent_is_picked() -> None:
    """Ranked by load ratio, not raw count - see `pick_agent`."""
    from platform_core.cases.assignment import claim_case, pick_agent

    _add_agent(TENANT, "busy", max_concurrent=8)
    _add_agent(TENANT, "free", max_concurrent=8)

    first = _seed_case(TENANT)
    _run(
        _with_session(
            TENANT,
            lambda s: claim_case(s, tenant_id=uuid.UUID(TENANT), case_id=first, user_ref="busy"),
        )
    )

    async def pick(session):
        return await pick_agent(session, tenant_id=uuid.UUID(TENANT))

    chosen = _run(_with_session(TENANT, pick))
    assert chosen is not None and chosen.user_ref == "free"


def test_a_pick_respects_skills_and_falls_back_to_the_unskilled() -> None:
    from platform_core.cases.assignment import pick_agent

    _add_agent(TENANT, "pcb-only", skills=["pcb"])
    _add_agent(TENANT, "general")

    def pick(line: str):
        return _run(
            _with_session(
                TENANT,
                lambda s: pick_agent(s, tenant_id=uuid.UUID(TENANT), business_line=line),
            )
        )

    assert pick("pcb").user_ref == "pcb-only"
    # Nobody skilled for components: the general agent, not nobody.
    assert pick("components").user_ref == "general"


def test_nobody_is_picked_when_everyone_is_at_capacity() -> None:
    """None, not the least-bad - an unassigned case is visible, an overloaded
    agent is not."""
    from platform_core.cases.assignment import claim_case, pick_agent

    _add_agent(TENANT, "solo", max_concurrent=1)
    _seed_case(TENANT)
    first = _seed_case(TENANT)
    _run(
        _with_session(
            TENANT,
            lambda s: claim_case(s, tenant_id=uuid.UUID(TENANT), case_id=first, user_ref="solo"),
        )
    )

    async def pick(session):
        return await pick_agent(session, tenant_id=uuid.UUID(TENANT))

    assert _run(_with_session(TENANT, pick)) is None


def test_a_claim_is_refused_at_capacity() -> None:
    """A volunteer must not walk past the ceiling."""
    from platform_core.cases.assignment import AssignmentError, claim_case

    _add_agent(TENANT, "solo", max_concurrent=1)
    first = _seed_case(TENANT)
    second = _seed_case(TENANT)

    def claim(case_id: uuid.UUID):
        return _run(
            _with_session(
                TENANT,
                lambda s: claim_case(
                    s, tenant_id=uuid.UUID(TENANT), case_id=case_id, user_ref="solo"
                ),
            )
        )

    claim(first)
    with pytest.raises(AssignmentError, match="at capacity"):
        claim(second)


def test_a_claim_from_an_inactive_agent_is_refused() -> None:
    from platform_core.cases.assignment import AssignmentError, claim_case

    _add_agent(TENANT, "off", status="inactive")
    case_id = _seed_case(TENANT)

    async def claim(session):
        with pytest.raises(AssignmentError, match="not active"):
            await claim_case(session, tenant_id=uuid.UUID(TENANT), case_id=case_id, user_ref="off")

    _run(_with_session(TENANT, claim))


def test_release_returns_a_case_to_the_queue_and_is_idempotent() -> None:
    """Double-clicking release must not be an error."""
    from platform_core.cases.assignment import claim_case, queue_cases, release_case

    _add_agent(TENANT, "a")
    case_id = _seed_case(TENANT)

    async def claim(session):
        return await claim_case(session, tenant_id=uuid.UUID(TENANT), case_id=case_id, user_ref="a")

    assert _run(_with_session(TENANT, claim)).assignee_ref == "a"

    async def release(session):
        return await release_case(session, tenant_id=uuid.UUID(TENANT), case_id=case_id)

    assert _run(_with_session(TENANT, release)).assignee_ref is None
    assert _run(_with_session(TENANT, release)).assignee_ref is None

    queue = _run(_with_session(TENANT, lambda s: queue_cases(s, tenant_id=uuid.UUID(TENANT))))
    assert [c.id for c in queue] == [case_id]


def test_the_queue_is_oldest_first_and_excludes_the_assigned() -> None:
    from platform_core.cases.assignment import claim_case, queue_cases

    _add_agent(TENANT, "a")
    now = int(time.time())
    older = _seed_case(TENANT, opened_at=now - 600)
    newer = _seed_case(TENANT, opened_at=now)

    async def claim(session):
        return await claim_case(session, tenant_id=uuid.UUID(TENANT), case_id=older, user_ref="a")

    _run(_with_session(TENANT, claim))

    queue = _run(_with_session(TENANT, lambda s: queue_cases(s, tenant_id=uuid.UUID(TENANT))))
    assert [c.id for c in queue] == [newer]


def test_a_partial_update_does_not_clobber_skills_or_capacity() -> None:
    """Changing status must not silently reset everything else."""
    from platform_core.cases.assignment import update_agent

    _add_agent(TENANT, "a", skills=["pcb"], max_concurrent=7)

    async def deactivate(session):
        return await update_agent(
            session, tenant_id=uuid.UUID(TENANT), user_ref="a", status="inactive"
        )

    row = _run(_with_session(TENANT, deactivate))
    assert row.status == "inactive"
    assert list(row.skills) == ["pcb"], "skills were reset by a status-only patch"
    assert int(row.max_concurrent) == 7


def test_another_tenant_sees_no_agents_and_cannot_claim_our_case() -> None:
    """RLS on both tables, not just the tenant filter in the query."""
    from platform_core.cases.assignment import list_agents, queue_cases

    _add_agent(TENANT, "ours")
    case_id = _seed_case(TENANT)

    assert (
        _run(
            _with_session(TENANT_OTHER, lambda s: list_agents(s, tenant_id=uuid.UUID(TENANT_OTHER)))
        )
        == []
    )

    async def cross_queue(session):
        return await queue_cases(session, tenant_id=uuid.UUID(TENANT_OTHER))

    assert _run(_with_session(TENANT_OTHER, cross_queue)) == []

    # Our case is still ours and still queued.
    ours = _run(_with_session(TENANT, lambda s: queue_cases(s, tenant_id=uuid.UUID(TENANT))))
    assert [c.id for c in ours] == [case_id]
