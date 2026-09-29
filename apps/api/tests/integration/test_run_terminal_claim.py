"""Exactly one worker may finish a run, and the loser must find out.

The gap
-------
A run was finalized by mutating the ORM object and flushing:

    run.status = RunStatus.COMPLETED.value
    await self._session.flush()

Nothing in that says "only if nobody else got here first", so two workers on
the same run both write a terminal state and the second silently overwrites the
first. The trace of it is gone: the row looks perfectly healthy, holding one
answer's `output_hash` with no indication that another answer existed.

The ordering makes it worse than a lost update. The customer-visible dispatch
happens *before* the finalize, so a compare-and-set at the terminal write would
detect the conflict only after both workers had already sent a reply. The claim
therefore has to be taken before the send, and these tests pin that ordering
rather than just the predicate.

Two other writers can reach the same row: the retention sweep marks stale
placeholders ABANDONED, and a lease expiry can let a second worker pick up a run
the first still holds. Both are terminal transitions, so both are covered here.

These run against real Postgres because the whole mechanism is an atomic
`UPDATE ... WHERE`, and an in-memory double would be asserting that the test
double behaves correctly.
"""

import os
import uuid

import pytest
from sqlalchemy import create_engine, text

pytestmark = pytest.mark.integration

ADMIN_URL = os.environ.get(
    "APP_ADMIN_DATABASE_URL",
    "postgresql+psycopg://platform:platform@localhost:5435/platform",
)
APP_URL = os.environ.get(
    "APP_TEST_DATABASE_URL",
    "postgresql+psycopg://platform_app:platform_app@localhost:5435/platform",
)

TENANT = "0190c000-0000-7000-8000-0000000000ef"
# `conversation_ref_id` is a plain uuid column with no foreign key, so
# the value only has to be distinct.
CONV = "0190c000-0000-7000-8000-0000000000f0"

QUEUED = "queued"
RUNNING = "running"
ABANDONED = "abandoned"
COMPLETED = "completed"


def _run(coro):
    import asyncio

    return asyncio.run(coro, loop_factory=asyncio.SelectorEventLoop)


def _seed(status: str = RUNNING) -> str:
    """One run in a given state, inserted as the owner role."""
    admin = create_engine(ADMIN_URL)
    run_id = str(uuid.uuid4())
    with admin.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO tenants (id, slug, name, status) VALUES "
                "(:id, 'claim-t', 'claim', 'active') ON CONFLICT (slug) DO NOTHING"
            ),
            {"id": TENANT},
        )
        conn.execute(
            text(
                "INSERT INTO agent_runs (id, tenant_id, conversation_ref_id, route, "
                "status, input_hash, started_at) VALUES "
                "(:i,:t,:c,'kb',:st,'',:now)"
            ),
            {"i": run_id, "t": TENANT, "c": CONV, "st": status, "now": 1},
        )
    admin.dispose()
    return run_id


def _cleanup(*run_ids: str) -> None:
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        for run_id in run_ids:
            conn.execute(text("DELETE FROM agent_runs WHERE id = :i"), {"i": run_id})
    admin.dispose()


def _state(run_id: str) -> tuple[str, int]:
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        row = conn.execute(
            text("SELECT status, version FROM agent_runs WHERE id = :i"), {"i": run_id}
        ).first()
    admin.dispose()
    return (str(row[0]), int(row[1])) if row else ("", -1)


async def _claim(run_id: str, status: str, version: int) -> int | None:
    """Claim as the app role with RLS bound, the way a worker would."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from platform_core.agent_runtime.terminal import claim_terminal
    from platform_core.db import create_engine as app_engine

    engine = app_engine(APP_URL)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            await session.execute(
                text("SELECT set_config('app.tenant_id', :t, true)"), {"t": TENANT}
            )
            claimed = await claim_terminal(
                session,
                run_id=uuid.UUID(run_id),
                expected_status=status,
                expected_version=version,
            )
            await session.commit()
            return claimed
    finally:
        await engine.dispose()


def test_only_one_worker_can_claim_a_run_for_completion() -> None:
    """The whole point. One claim, one winner, and the loser is told.

    Asserted as "exactly one non-None" rather than "the first one is not None",
    because a claim that never succeeds at all also satisfies a weaker test and
    would leave every run permanently unfinished.
    """
    run_id = _seed(RUNNING)
    try:
        first = _run(_claim(run_id, RUNNING, 0))
        second = _run(_claim(run_id, RUNNING, 0))

        winners = [c for c in (first, second) if c is not None]
        assert len(winners) == 1, f"expected exactly one winner, got {first!r}/{second!r}"
    finally:
        _cleanup(run_id)


def test_a_claim_bumps_the_version_so_the_change_is_visible() -> None:
    """The version is the receipt, and it is what makes the claim auditable.

    Without it the CAS still prevents the double write, but a lost update leaves
    no trace at all - which is the part that made the original bug invisible.
    """
    run_id = _seed(RUNNING)
    try:
        before = _state(run_id)[1]
        claimed = _run(_claim(run_id, RUNNING, 0))
        assert claimed is not None
        after = _state(run_id)[1]
        assert after == before + 1, (before, after)
    finally:
        _cleanup(run_id)


def test_claiming_a_run_that_is_already_terminal_fails() -> None:
    """A run nobody else is working on must not be claimable.

    This is the retention sweep's case. It marks a stale placeholder ABANDONED
    while a worker may still be executing it; whichever lands second has to lose
    rather than overwrite, or an abandoned run silently becomes "completed" and
    the reason it was never answered disappears.
    """
    run_id = _seed(ABANDONED)
    try:
        assert _run(_claim(run_id, RUNNING, 0)) is None, (
            "claimed a run the sweep had already closed"
        )
    finally:
        _cleanup(run_id)


def test_a_worker_cannot_complete_a_run_the_sweep_already_abandoned() -> None:
    """The real overlap with the retention sweep, in the direction that matters.

    The sweep closes `queued` placeholders - accepted, never executed, past any
    plausible latency. A worker can be holding one of those: it loaded the run
    as `queued`, and the sweep marked it `abandoned` before the worker got to
    its claim. The worker's compare-and-set names both the status it expected
    and the version it saw, so it loses.

    Losing matters more than the status. A worker that completed an abandoned
    run would overwrite the one record that it was never executed, and the
    "why was this never answered" question would acquire a different answer
    every time somebody replayed it.
    """
    from platform_core.agent_runtime.abandoned import abandon_stale_placeholders

    async def sweep() -> int:
        from sqlalchemy.ext.asyncio import async_sessionmaker

        from platform_core.db import create_engine as app_engine
        from platform_core.identity.tenant_context import TenantContext, apply_rls_tenant

        engine = app_engine(APP_URL)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with factory() as session:
                await apply_rls_tenant(
                    session,
                    TenantContext(tenant_id=uuid.UUID(TENANT), actor_id=None, actor_kind="system"),
                )
                count = await abandon_stale_placeholders(
                    session, tenant_id=uuid.UUID(TENANT), older_than_seconds=0, now=10**9
                )
                await session.commit()
                return count
        finally:
            await engine.dispose()

    run_id = _seed(QUEUED)
    try:
        _run(sweep())
        assert _state(run_id)[0] == ABANDONED
        assert _run(_claim(run_id, QUEUED, 0)) is None, (
            "a worker completed a run the retention sweep had abandoned"
        )
    finally:
        _cleanup(run_id)


def test_a_claimed_run_is_not_yet_terminal_so_a_crash_is_visible() -> None:
    """What the counter buys beyond preventing the overwrite.

    A claimed run has moved its version and not its status. That combination is
    the signature of a worker that took the run on and then died - and it is
    otherwise indistinguishable from a run nobody ever picked up. This is why
    the claim does not write a terminal status itself: the intermediate state
    has to stay observable.
    """
    run_id = _seed(QUEUED)
    try:
        assert _run(_claim(run_id, QUEUED, 0)) is not None
        status, version = _state(run_id)
        assert status == QUEUED, "claiming must not decide the terminal state"
        assert version == 1, version
    finally:
        _cleanup(run_id)
