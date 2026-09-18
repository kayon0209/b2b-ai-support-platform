"""Integration: a rollout flag really controls the answer path's reranker.

The defect being closed is not "the flag logic is wrong" - that was already
correct and unit-tested. It is that **no code read a flag at all**, so
`set_rollout` moved a number nothing acted on and a canary release was not
executable. This test is the proof that an operator can now change customer
-visible behaviour without a deploy: it drives
`define_flag` -> `set_rollout` -> `evaluate` against real tables and asserts
the reranker is picked up or not.

The reranker is the right thing to gate first because it changes *which
evidence* an answer is built from, so its blast radius is every answer.
"""

import asyncio
import os
import uuid

import pytest
from sqlalchemy import create_engine, text

from platform_core.agent_runtime.orchestrator import RERANK_FLAG_KEY, reranker_for_tenant
from platform_core.identity.tenant_context import TenantContext
from platform_core.knowledge import flag_service

pytestmark = pytest.mark.integration

ADMIN_URL = os.environ.get(
    "APP_ADMIN_DATABASE_URL",
    "postgresql+psycopg://platform:platform@localhost:5435/platform",
)
APP_URL = "postgresql+psycopg://platform_app:platform_app@localhost:5435/platform"

TENANT = "0190d000-0000-7000-8000-0000000000a3"

_CLEAN: tuple[str, ...] = (
    "DELETE FROM feature_flag_targets WHERE tenant_id = :t",
    "DELETE FROM feature_flags WHERE tenant_id = :t",
    "DELETE FROM audit_events WHERE tenant_id = :t",
)


def _clean(conn) -> None:
    for stmt in _CLEAN:
        conn.execute(text(stmt), {"t": TENANT})


def _run(coro):
    return asyncio.run(coro, loop_factory=asyncio.SelectorEventLoop)


@pytest.fixture(scope="module", autouse=True)
def seed_tenant():
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO tenants (id, slug, name, status) VALUES "
                "(:id, 'rerank-t1', 'rerank-t1', 'active') ON CONFLICT (slug) DO NOTHING"
            ),
            {"id": TENANT},
        )
    yield
    with admin.begin() as conn:
        _clean(conn)
        conn.execute(text("DELETE FROM tenants WHERE slug = 'rerank-t1'"))
    admin.dispose()


@pytest.fixture(autouse=True)
def clean_flags():
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        _clean(conn)
    yield
    with admin.begin() as conn:
        _clean(conn)
    admin.dispose()


class _Reranker:
    """Stands in for the provider-backed reranker."""


async def _in_session(fn):
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from platform_core.db import create_engine as app_engine
    from platform_core.identity.tenant_context import apply_rls_tenant

    engine = app_engine(APP_URL)
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            ctx = TenantContext(tenant_id=uuid.UUID(TENANT), actor_id=None, actor_kind="system")
            await apply_rls_tenant(session, ctx)
            result = await fn(session, ctx)
            await session.commit()
            return result
    finally:
        await engine.dispose()


def _reranker_in_use() -> tuple[bool, str]:
    async def _fn(session, ctx):
        return await reranker_for_tenant(
            session,
            tenant_id=uuid.UUID(TENANT),
            reranker=_Reranker(),  # type: ignore[arg-type]
        )

    chosen, reason = _run(_in_session(_fn))
    return chosen is not None, reason


def _define_flag() -> None:
    async def _fn(session, ctx):
        await flag_service.define_flag(
            session,
            ctx=ctx,
            key=RERANK_FLAG_KEY,
            description="Rerank the evidence for this tenant's answers",
        )

    _run(_in_session(_fn))


def _set_enabled(enabled: bool) -> None:
    async def _fn(session, ctx):
        await flag_service.set_enabled(session, ctx=ctx, key=RERANK_FLAG_KEY, enabled=enabled)

    _run(_in_session(_fn))


def _set_rollout(percent: int) -> None:
    async def _fn(session, ctx):
        await flag_service.set_rollout(
            session, ctx=ctx, key=RERANK_FLAG_KEY, rollout_percent=percent
        )

    _run(_in_session(_fn))


# --- the rollout, as an operator would drive it ----------------------------


def test_an_undefined_flag_keeps_the_fused_order() -> None:
    """Default False: a missing flag is never a silent opt-in to a
    quality-affecting change."""
    in_use, reason = _reranker_in_use()

    assert in_use is False
    assert reason == "UNKNOWN_FLAG"


def test_a_flag_created_closed_does_not_rerank() -> None:
    """Defining a flag must not enable the feature - that is the whole point
    of a release process."""
    _define_flag()

    in_use, reason = _reranker_in_use()

    assert in_use is False
    assert reason == "DISABLED"


def test_enabling_at_zero_percent_still_does_not_rerank() -> None:
    _define_flag()
    _set_enabled(True)

    in_use, reason = _reranker_in_use()

    assert in_use is False
    assert reason == "NOT_IN_ROLLOUT"


def test_full_rollout_reranks() -> None:
    _define_flag()
    _set_enabled(True)
    _set_rollout(100)

    in_use, reason = _reranker_in_use()

    assert in_use is True
    assert reason == "ROLLOUT"


def test_the_kill_switch_wins_over_a_full_rollout() -> None:
    """`set_enabled(False)` is checked before the rollout maths, so an
    operator stopping a bad rollout does not have to reason about
    percentages."""
    _define_flag()
    _set_enabled(True)
    _set_rollout(100)
    assert _reranker_in_use()[0] is True

    _set_enabled(False)

    in_use, reason = _reranker_in_use()
    assert in_use is False
    assert reason == "DISABLED"


def test_a_tenant_cannot_target_itself() -> None:
    """Pins the constraint that shapes how this flag can be rolled out.

    Flags are tenant-owned and RLS-scoped, and the orchestrator evaluates the
    flag inside the *running tenant's* session - so the flag a run can see is
    that tenant's own. `target_tenant` refuses self-targeting by design, which
    leaves exactly two levers: the kill switch and the rollout percentage.

    The consequence, stated rather than discovered later: **a central canary
    across tenants is not expressible today**. A tenant can roll the reranker
    out to itself; the platform cannot do it on their behalf, because under
    RLS the platform's flag is invisible in the tenant's session. Making it
    expressible needs a platform-owned flag read through a SECURITY DEFINER
    resolver, the same pattern the tenant bootstrap uses - a change worth its
    own decision, not something to smuggle in here.
    """
    _define_flag()
    _set_enabled(True)

    async def _fn(session, ctx):
        return await flag_service.target_tenant(
            session, ctx=ctx, key=RERANK_FLAG_KEY, tenant_id=ctx.tenant_id, enabled=True
        )

    with pytest.raises(flag_service.FlagError) as exc:
        _run(_in_session(_fn))

    assert exc.value.code == "SELF_TARGET"


def test_a_percentage_rollout_is_stable_for_one_tenant() -> None:
    """A rollout has to be sticky: a tenant that flips in and out of a
    canary between runs would make the comparison meaningless and the
    customer's experience inconsistent."""
    _define_flag()
    _set_enabled(True)
    _set_rollout(50)

    outcomes = {_reranker_in_use()[1] for _ in range(5)}

    # One stable answer, and it is one of the two rollout outcomes.
    assert len(outcomes) == 1
    assert outcomes <= {"ROLLOUT", "NOT_IN_ROLLOUT"}


def test_the_rollout_decisions_are_audited() -> None:
    """A rollout has to be explainable after the fact: "why did tenant X
    rerank yesterday" needs an answer that is not a guess."""
    _define_flag()
    _set_enabled(True)
    _set_rollout(50)

    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        actions = (
            conn.execute(
                text(
                    "SELECT action FROM audit_events WHERE tenant_id = :t "
                    "AND action LIKE 'feature_flag.%' ORDER BY occurred_at"
                ),
                {"t": TENANT},
            )
            .scalars()
            .all()
        )
    admin.dispose()

    assert "feature_flag.defined" in actions
    assert "feature_flag.enabled_changed" in actions
    assert "feature_flag.rollout_changed" in actions
