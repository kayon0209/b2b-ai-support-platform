"""A failed agent run has to leave something an operator can act on.

The gap
-------
A failed run wrote `status = 'failed'` and returned. No reason, no dead letter,
no way to try again. An operator's entire view was a count in a dashboard, and
the customer whose question was never answered had no path to an answer at all
except somebody noticing the count and guessing.

Two properties are under test, and they pull in opposite directions:

1. **A failure must be visible.** A run that failed leaves a record naming the
   run and the reason it failed.
2. **A replay must not become a second answer.** Replay creates a *new* run and
   leaves the original failed one alone. Resurrecting the row would destroy the
   only record that the first attempt failed - which is the thing an operator
   replaying it is trying to learn about - and reusing it for a second delivery
   risks two replies to one question.

So the tests below assert both, and the refusal cases matter as much as the
happy path: a replay endpoint that accepts a `completed` run is a
send-everything-twice button.
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
APP_URL = "postgresql+psycopg://platform_app:platform_app@localhost:5435/platform"

TENANT = "0190c000-0000-7000-8000-0000000000f1"

FAILED = "failed"
COMPLETED = "completed"
QUEUED = "queued"


def _run(coro):
    import asyncio

    return asyncio.run(coro, loop_factory=asyncio.SelectorEventLoop)


def _seed_run(status: str) -> str:
    admin = create_engine(ADMIN_URL)
    run_id = str(uuid.uuid4())
    with admin.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO tenants (id, slug, name, status) VALUES "
                "(:id, 'replay-t', 'replay', 'active') ON CONFLICT (slug) DO NOTHING"
            ),
            {"id": TENANT},
        )
        conn.execute(
            text(
                "INSERT INTO agent_runs (id, tenant_id, conversation_ref_id, route, "
                "status, input_hash, started_at, model_config, retrieval_config) VALUES "
                "(:i,:t,:c,'kb',:st,'h',1,'{}'::jsonb,'{}'::jsonb)"
            ),
            {"i": run_id, "t": TENANT, "c": str(uuid.uuid4()), "st": status},
        )
    admin.dispose()
    return run_id


def _cleanup(*run_ids: str) -> None:
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        for run_id in run_ids:
            conn.execute(
                text("DELETE FROM dead_letter_items WHERE resource_id = :i"), {"i": run_id}
            )
        for run_id in run_ids:
            conn.execute(
                text("UPDATE agent_runs SET replay_of_run_id = NULL WHERE replay_of_run_id = :i"),
                {"i": run_id},
            )
            conn.execute(text("DELETE FROM agent_runs WHERE id = :i"), {"i": run_id})
    admin.dispose()


async def _record_failure(run_id: str, error_code: str, detail: str) -> object:
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from platform_core.agent_runtime.rerun import record_run_failure
    from platform_core.db import create_engine as app_engine

    engine = app_engine(APP_URL)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            await session.execute(
                text("SELECT set_config('app.tenant_id', :t, true)"), {"t": TENANT}
            )
            ref = await record_run_failure(
                session,
                run_id=uuid.UUID(run_id),
                tenant_id=uuid.UUID(TENANT),
                error_code=error_code,
                error_detail=detail,
                attempts=3,
                now=1_700_000_000,
            )
            await session.commit()
            return ref
    finally:
        await engine.dispose()


async def _replay(run_id: str) -> object:
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from platform_core.agent_runtime.rerun import ReplayRefused, rerun_failed_run
    from platform_core.db import create_engine as app_engine

    engine = app_engine(APP_URL)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            await session.execute(
                text("SELECT set_config('app.tenant_id', :t, true)"), {"t": TENANT}
            )
            try:
                out = await rerun_failed_run(
                    session,
                    run_id=uuid.UUID(run_id),
                    tenant_id=uuid.UUID(TENANT),
                    actor_ref="operator@example.test",
                    now=1_700_000_000,
                )
                await session.commit()
                return out
            except ReplayRefused as exc:
                await session.rollback()
                return exc
    finally:
        await engine.dispose()


def _dead_letters_for(run_id: str) -> list[tuple[str, str, str]]:
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        rows = conn.execute(
            text(
                "SELECT error_code, coalesce(error_detail,''), status FROM dead_letter_items "
                "WHERE resource_id = :i"
            ),
            {"i": run_id},
        ).all()
    admin.dispose()
    return [(str(a), str(b), str(c)) for a, b, c in rows]


def _run_row(run_id: str) -> tuple[str, str | None]:
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        row = conn.execute(
            text("SELECT status, replay_of_run_id::text FROM agent_runs WHERE id = :i"),
            {"i": run_id},
        ).first()
    admin.dispose()
    return (str(row[0]), str(row[1]) if row[1] else None) if row else ("", None)


# --- the failure has to be visible -----------------------------------------


def test_a_failed_run_leaves_a_dead_letter_that_points_at_it() -> None:
    """The whole point of the record: an operator can find the run.

    `resource_id` is what makes it actionable. The existing dead-letter row
    stores only an operation digest, which answers "is this the same failure
    repeating?" and not "which run do I open?" - fine for connector calls, and
    useless for replay.
    """
    run_id = _seed_run(FAILED)
    try:
        _run(_record_failure(run_id, "CHATWOOT_UNREACHABLE", "connection refused"))

        letters = _dead_letters_for(run_id)
        assert len(letters) == 1, letters
        code, _detail, status = letters[0]
        assert code == "CHATWOOT_UNREACHABLE", code
        assert status == "pending", status
    finally:
        _cleanup(run_id)


def test_the_record_keeps_the_error_code_not_the_exception_text() -> None:
    """Bounded, classified, non-customer content.

    A dead-letter table is read by an operational endpoint and copied into
    backups. An exception message can carry a connection string with a password
    in it, so what is stored is the code, and the detail is truncated.
    """
    run_id = _seed_run(FAILED)
    try:
        _run(
            _record_failure(
                run_id,
                "CHATWOOT_UNREACHABLE",
                "x" * 10_000,
            )
        )
        _code, detail, _status = _dead_letters_for(run_id)[0]
        assert len(detail) <= 2_000, len(detail)
    finally:
        _cleanup(run_id)


# --- replay must not become a second answer --------------------------------


def test_replay_creates_a_new_run_and_leaves_the_original_failed() -> None:
    """New row, not resurrection.

    Resurrecting the original would erase the evidence that the first attempt
    failed - which is the reason an operator is replaying it - and would make
    the replayed run indistinguishable from one that worked first time.
    """
    run_id = _seed_run(FAILED)
    try:
        result = _run(_replay(run_id))
        new_id = getattr(result, "new_run_id", None)
        assert new_id is not None, result

        status, replay_of = _run_row(run_id)
        assert status == FAILED, status
        assert replay_of is None, replay_of

        new_status, lineage = _run_row(str(new_id))
        assert new_status == QUEUED, new_status
        assert lineage == run_id, lineage
    finally:
        _cleanup(run_id)


def test_replay_refuses_a_run_that_already_succeeded() -> None:
    """The dangerous direction.

    A `completed` run has already sent its answer. Replaying it would send it
    again, and the customer would receive two replies to one question. This is
    the single most important refusal in the module.
    """
    run_id = _seed_run(COMPLETED)
    try:
        result = _run(_replay(run_id))
        assert type(result).__name__ == "ReplayRefused", result
        status, _ = _run_row(run_id)
        assert status == COMPLETED, "a refused replay mutated the run"
    finally:
        _cleanup(run_id)


def test_replay_refuses_when_one_is_already_in_flight() -> None:
    """No fan-out from a double-clicked button.

    An operator who cannot see the first request succeed will click again. If
    that produces a second run, the replay path becomes a way to multiply work
    exactly when the system is already struggling.
    """
    run_id = _seed_run(FAILED)
    try:
        first = _run(_replay(run_id))
        second = _run(_replay(run_id))
        assert getattr(first, "new_run_id", None) is not None, first
        assert type(second).__name__ == "ReplayRefused", second
    finally:
        _cleanup(run_id)
