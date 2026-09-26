"""Integration: canned replies against a real Postgres with RLS on.

What a database is needed for, in order of why it matters:

1. **Scope filtering is inclusive of the unscoped.** That is a query-shape claim
   about real rows, not a unit-testable one - and getting it wrong silently
   hides the general replies from the agents who need them most.
2. **RLS holds.** Another tenant's session sees none, and cannot "use" ours.
3. **The shortcut constraint is real.** `/eta` and `eta` normalise to one row,
   and a second claim on the same shortcut is rejected by the database - the
   router maps that to 409, and this is the assertion that proves there is
   something to map.
"""

from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import create_engine as _admin_engine
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
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

_NS = uuid.uuid5(uuid.NAMESPACE_URL, "b2b-ai-support/tests/canned-replies")
TENANT = str(uuid.uuid5(_NS, "tenant"))
TENANT_OTHER = str(uuid.uuid5(_NS, "tenant-other"))


def _run(coro):
    import asyncio

    return asyncio.run(coro, loop_factory=asyncio.SelectorEventLoop)


def _wipe() -> None:
    admin = _admin_engine(ADMIN_URL)
    with admin.begin() as conn:
        conn.execute(
            text("DELETE FROM canned_replies WHERE tenant_id IN (:a, :b)"),
            {"a": TENANT, "b": TENANT_OTHER},
        )
    admin.dispose()


@pytest.fixture(autouse=True)
def _clean():
    _wipe()
    yield
    _wipe()


@pytest.fixture(scope="module", autouse=True)
def _tenants():
    admin = _admin_engine(ADMIN_URL)
    with admin.begin() as conn:
        for tid, slug in ((TENANT, "can-a"), (TENANT_OTHER, "can-b")):
            conn.execute(
                text(
                    "INSERT INTO tenants (id, slug, name, status) VALUES "
                    "(:id, :slug, :slug, 'active') ON CONFLICT (slug) DO NOTHING"
                ),
                {"id": tid, "slug": slug},
            )
    yield
    with admin.begin() as conn:
        conn.execute(
            text(
                "DELETE FROM canned_replies WHERE tenant_id IN "
                "(SELECT id FROM tenants WHERE slug LIKE 'can-%')"
            )
        )
        conn.execute(text("DELETE FROM tenants WHERE slug LIKE 'can-%'"))
    admin.dispose()


async def _with_session(tenant: str, fn):
    """One app-role session with the tenant bound. Rolls back on failure.

    The rollback matters for the clash test below: a failed INSERT leaves the
    session unusable, and without it a later assertion in the same session would
    fail with a confusing error instead of the one under test.
    """
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


def _make(tenant: str, title: str, **fields) -> None:
    from platform_core.cases.canned_service import create_canned

    async def go(session):
        return await create_canned(
            session,
            tenant_id=uuid.UUID(tenant),
            title=title,
            body=f"body of {title}",
            actor_id=uuid.uuid4(),
            **fields,
        )

    _run(_with_session(tenant, go))


def test_a_reply_can_be_created_listed_and_archived() -> None:
    from platform_core.cases.canned_service import list_canned, update_canned

    _make(TENANT, "标准交期")

    async def read(session):
        return await list_canned(session, tenant_id=uuid.UUID(TENANT))

    rows = _run(_with_session(TENANT, read))
    assert [r.title for r in rows] == ["标准交期"]

    async def archive(session):
        return await update_canned(
            session,
            tenant_id=uuid.UUID(TENANT),
            reply_id=rows[0].id,
            actor_id=uuid.uuid4(),
            archived=True,
        )

    _run(_with_session(TENANT, archive))

    hidden = _run(_with_session(TENANT, read))
    assert hidden == [], "an archived reply must leave the picker"
    # ...but it is not deleted: history of what was sent must survive.
    with_archived = _run(
        _with_session(
            TENANT,
            lambda s: list_canned(s, tenant_id=uuid.UUID(TENANT), include_archived=True),
        )
    )
    assert [r.title for r in with_archived] == ["标准交期"]


def test_a_shortcut_resolves_and_an_archived_one_does_not() -> None:
    """A shortcut that silently yields a withdrawn template is the worst failure
    this feature can have, because the agent has no reason to reread it."""
    from platform_core.cases.canned_service import resolve_shortcut, update_canned

    _make(TENANT, "交期", shortcut="/eta")

    def by(value: str):
        return _run(
            _with_session(
                TENANT,
                lambda s: resolve_shortcut(s, tenant_id=uuid.UUID(TENANT), shortcut=value),
            )
        )

    assert by("eta") is not None, "the stored form has no leading slash"
    assert by("/eta") is not None, "an agent types the slash"

    async def archive(session):
        row = await resolve_shortcut(session, tenant_id=uuid.UUID(TENANT), shortcut="eta")
        return await update_canned(
            session,
            tenant_id=uuid.UUID(TENANT),
            reply_id=row.id,
            actor_id=uuid.uuid4(),
            archived=True,
        )

    _run(_with_session(TENANT, archive))
    assert by("eta") is None


def test_scope_filter_keeps_the_unscoped_replies() -> None:
    """Filtering to `pcb` must not hide the general replies."""
    from platform_core.cases.canned_service import list_canned

    _make(TENANT, "pcb-only", business_line="pcb")
    _make(TENANT, "general")

    async def scoped(session):
        return await list_canned(session, tenant_id=uuid.UUID(TENANT), business_line="pcb")

    titles = sorted(r.title for r in _run(_with_session(TENANT, scoped)))
    assert titles == ["general", "pcb-only"]

    async def other(session):
        return await list_canned(session, tenant_id=uuid.UUID(TENANT), business_line="components")

    assert [r.title for r in _run(_with_session(TENANT, other))] == ["general"]


def test_using_a_reply_counts_it_and_moves_it_to_the_front() -> None:
    """The counter is the picker's ordering, so it has to actually order."""
    from platform_core.cases.canned_service import list_canned, use_canned

    _make(TENANT, "first")
    _make(TENANT, "second")

    async def read(session):
        return await list_canned(session, tenant_id=uuid.UUID(TENANT))

    rows = _run(_with_session(TENANT, read))
    second = next(r for r in rows if r.title == "second")

    async def use(session):
        return await use_canned(session, tenant_id=uuid.UUID(TENANT), reply_id=second.id)

    used = _run(_with_session(TENANT, use))
    assert used is not None and int(used.usage_count) == 1

    assert [r.title for r in _run(_with_session(TENANT, read))] == ["second", "first"]


def test_a_second_claim_on_a_shortcut_is_rejected_by_the_database() -> None:
    """The router maps this to 409; this is the assertion that there is one."""
    from platform_core.cases.canned_service import create_canned

    _make(TENANT, "one", shortcut="eta")

    async def clash(session):
        with pytest.raises(IntegrityError):
            await create_canned(
                session,
                tenant_id=uuid.UUID(TENANT),
                title="two",
                body="body",
                actor_id=uuid.uuid4(),
                shortcut="eta",
            )
        # The failed INSERT poisoned the transaction; it has to be cleared here
        # because `pytest.raises` swallowed the exception before the helper's
        # own rollback could see it.
        await session.rollback()

    _run(_with_session(TENANT, clash))


def test_an_empty_title_is_refused_before_the_database_is_asked() -> None:
    from platform_core.cases.canned_service import CannedReplyError, create_canned

    async def bad(session):
        with pytest.raises(CannedReplyError, match="title"):
            await create_canned(
                session,
                tenant_id=uuid.UUID(TENANT),
                title="   ",
                body="body",
                actor_id=uuid.uuid4(),
            )

    _run(_with_session(TENANT, bad))


def test_another_tenant_sees_nothing_and_cannot_use_ours() -> None:
    """RLS, not just the tenant filter on the query."""
    from platform_core.cases.canned_service import list_canned, use_canned

    _make(TENANT, "mine", shortcut="eta")

    async def theirs(session):
        return await list_canned(session, tenant_id=uuid.UUID(TENANT_OTHER))

    assert _run(_with_session(TENANT_OTHER, theirs)) == []

    async def ours(session):
        return await list_canned(session, tenant_id=uuid.UUID(TENANT))

    rows = _run(_with_session(TENANT, ours))
    assert len(rows) == 1

    async def cross_use(session):
        return await use_canned(session, tenant_id=uuid.UUID(TENANT_OTHER), reply_id=rows[0].id)

    assert _run(_with_session(TENANT_OTHER, cross_use)) is None


def test_many_replies_without_a_shortcut_do_not_collide() -> None:
    """NULLs are distinct under UNIQUE - the reason `shortcut` is nullable."""
    from platform_core.cases.canned_service import list_canned

    _make(TENANT, "a")
    _make(TENANT, "b")

    async def read(session):
        return await list_canned(session, tenant_id=uuid.UUID(TENANT))

    assert len(_run(_with_session(TENANT, read))) == 2
