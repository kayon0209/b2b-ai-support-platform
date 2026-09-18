"""Integration tests: billing ledger and the `usage.recorded` consumer.

`docs/development-plan.md` Phase 5 lists "usage quotas and billing events".
The emitter (`orchestrator.enqueue`) and the quota check existed and were
tested; the consumer did not, because `build_default_relay` never registered
`usage.recorded`. These tests exist so that gap cannot reopen silently:

- the first test calls the handler through the *relay*, not directly, so a
  registry that loses the event type fails here rather than in production;
- the idempotency test delivers the same event twice, which is the documented
  at-least-once behaviour of the outbox;
- the append-only test proves the grant, not the intention - a role that can
  UPDATE a ledger row is a ledger that can be rewritten.
"""

import json
import os
import time
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from platform_core.billing.service import monthly_rollup, record_adjustment, record_usage

pytestmark = pytest.mark.integration

ADMIN_URL = os.environ.get(
    "APP_ADMIN_DATABASE_URL",
    "postgresql+psycopg://platform:platform@localhost:5435/platform",
)
APP_URL = "postgresql+psycopg://platform_app:platform_app@localhost:5435/platform"

TENANT_A = "0190b000-0000-7000-8000-0000000000a1"
TENANT_B = "0190b000-0000-7000-8000-0000000000b1"

EVENT_INSERT = (
    "INSERT INTO outbox_events "
    "(id, tenant_id, event_id, event_type, event_version, aggregate_type, aggregate_id, "
    " payload, status, created_at, attempts, trace_id) "
    "VALUES (:id, :t, :event_id, 'usage.recorded', 1, 'agent_run', :agg, "
    " CAST(:payload AS jsonb), 'queued', :created, 0, 'trace')"
)


def _run(coro):
    import asyncio

    return asyncio.run(coro, loop_factory=asyncio.SelectorEventLoop)


@pytest.fixture(scope="module", autouse=True)
def seed_tenants() -> None:
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        for tid, slug in ((TENANT_A, "billing-a"), (TENANT_B, "billing-b")):
            conn.execute(
                text(
                    "INSERT INTO tenants (id, slug, name, status) VALUES "
                    "(:id, :slug, :name, 'active') ON CONFLICT (slug) DO NOTHING"
                ),
                {"id": tid, "slug": slug, "name": slug},
            )
    yield
    admin.dispose()


@pytest.fixture(autouse=True)
def clean_ledger() -> None:
    admin = create_engine(ADMIN_URL)

    def _clear() -> None:
        with admin.begin() as conn:
            conn.execute(
                text("DELETE FROM billing_entries WHERE tenant_id IN (:a, :b)"),
                {"a": TENANT_A, "b": TENANT_B},
            )
            conn.execute(
                text("DELETE FROM outbox_events WHERE tenant_id IN (:a, :b)"),
                {"a": TENANT_A, "b": TENANT_B},
            )

    _clear()
    yield
    _clear()
    admin.dispose()


def _app_session():
    from platform_core.db import create_engine as app_engine

    return app_engine(APP_URL)


async def _with_session(tenant: str, fn):
    engine = _app_session()
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            await session.execute(
                text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant}
            )
            return await fn(session)
    finally:
        await engine.dispose()


# --- The consumer, driven through the relay ---------------------------------


def test_relay_registers_the_usage_event() -> None:
    """The gap itself: an event type with no handler is never aggregated."""
    from worker.outbox_relay import build_default_relay

    relay = build_default_relay()
    assert "usage.recorded" in relay.handlers, (
        "usage.recorded has no registered handler, so billing events are logged "
        "and dropped - the Phase 5 gap this suite guards"
    )


def test_usage_recorded_is_aggregated_through_the_relay() -> None:
    """End to end: outbox row -> relay -> ledger row -> rollup."""
    event_id = uuid.uuid4()
    run_id = uuid.uuid4()
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        conn.execute(
            text(EVENT_INSERT),
            {
                "id": str(uuid.uuid4()),
                "t": TENANT_A,
                "event_id": str(event_id),
                "agg": str(run_id),
                "payload": json.dumps(
                    {
                        "run_id": str(run_id),
                        "route": "knowledge_qa",
                        "status": "completed",
                        "prompt_tokens": 120,
                        "completion_tokens": 30,
                    }
                ),
                "created": int(time.time()),
            },
        )
    admin.dispose()

    async def drive(session):
        from worker.outbox_relay import build_default_relay

        # `commit=True`: this caller owns the transaction. Without it the
        # ledger insert and `mark_sent` are rolled back when the session
        # closes, and the relay still reports `sent=1` - the failure mode
        # that let an empty ledger look healthy.
        return await build_default_relay(batch=10).run_once(session, commit=True)

    stats = _run(_with_session(TENANT_A, drive))
    assert stats.sent == 1, f"relay did not send: {stats}"
    assert stats.failed == 0

    async def rollup(session):
        return await monthly_rollup(session, tenant_id=uuid.UUID(TENANT_A))

    result = _run(_with_session(TENANT_A, rollup))
    assert result.usage_entries == 1
    assert result.prompt_tokens == 120
    assert result.completion_tokens == 30
    assert result.total_tokens == 150


def test_malformed_payload_fails_rather_than_silently_dropping() -> None:
    """A billing event that cannot be parsed must park, not vanish."""
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        conn.execute(
            text(EVENT_INSERT),
            {
                "id": str(uuid.uuid4()),
                "t": TENANT_A,
                "event_id": str(uuid.uuid4()),
                "agg": "not-a-uuid",
                "payload": '{"status": "completed"}',
                "created": int(time.time()),
            },
        )
    admin.dispose()

    async def drive(session):
        from worker.outbox_relay import build_default_relay

        return await build_default_relay(batch=10).run_once(session, commit=True)

    stats = _run(_with_session(TENANT_A, drive))
    assert stats.sent == 0
    assert stats.failed == 1, "an unusable billing payload must surface as a failure"

    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        err = conn.execute(
            text(
                "SELECT last_error FROM outbox_events "
                "WHERE tenant_id = :t ORDER BY attempts DESC LIMIT 1"
            ),
            {"t": TENANT_A},
        ).scalar()
    admin.dispose()
    assert err, "the relay must record why it could not process the event"


# --- The commit contract ----------------------------------------------------


def test_run_once_without_commit_does_not_persist() -> None:
    """`run_once` writes into the caller's transaction; the caller commits.

    This is the failure that hid for the life of the billing feature: the
    relay ran every handler successfully and reported `sent`, while the
    enclosing transaction was rolled back on close and the ledger stayed
    empty. The two assertions below are the same call with and without
    `commit=True`, so the contract is pinned rather than assumed - a future
    reader cannot "simplify" the flag away without failing here.
    """
    event_id = uuid.uuid4()
    run_id = uuid.uuid4()
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        conn.execute(
            text(EVENT_INSERT),
            {
                "id": str(uuid.uuid4()),
                "t": TENANT_A,
                "event_id": str(event_id),
                "agg": str(run_id),
                "payload": json.dumps({"run_id": str(run_id), "prompt_tokens": 70}),
                "created": int(time.time()),
            },
        )
    admin.dispose()

    async def without_commit(session):
        from worker.outbox_relay import build_default_relay

        # No commit=True: the session closes and rolls back.
        return await build_default_relay(batch=10).run_once(session)

    stats = _run(_with_session(TENANT_A, without_commit))
    assert stats.sent == 1, "the handler did run - that is what makes this silent"

    async def rollup(session):
        return await monthly_rollup(session, tenant_id=uuid.UUID(TENANT_A))

    assert _run(_with_session(TENANT_A, rollup)).usage_entries == 0, (
        "without commit=True the write must not survive - if this fails the "
        "commit contract changed and the flag is now meaningless"
    )

    async def with_commit(session):
        from worker.outbox_relay import build_default_relay

        return await build_default_relay(batch=10).run_once(session, commit=True)

    # The same row is still queued (the rollback released the claim).
    stats = _run(_with_session(TENANT_A, with_commit))
    assert stats.sent == 1
    assert _run(_with_session(TENANT_A, rollup)).usage_entries == 1


# --- Idempotency ------------------------------------------------------------


def test_the_same_event_twice_bills_once() -> None:
    """The outbox is at-least-once; the ledger must not be."""
    event_id = uuid.uuid4()
    run_id = uuid.uuid4()

    async def twice(session):
        first = await record_usage(
            session,
            tenant_id=uuid.UUID(TENANT_A),
            event_id=event_id,
            run_id=run_id,
            prompt_tokens=100,
        )
        second = await record_usage(
            session,
            tenant_id=uuid.UUID(TENANT_A),
            event_id=event_id,
            run_id=run_id,
            prompt_tokens=100,
        )
        await session.commit()
        return first, second

    first, second = _run(_with_session(TENANT_A, twice))
    assert not first.duplicate and first.entry_id is not None
    assert second.duplicate and second.entry_id is None

    async def rollup(session):
        return await monthly_rollup(session, tenant_id=uuid.UUID(TENANT_A))

    result = _run(_with_session(TENANT_A, rollup))
    assert result.usage_entries == 1
    assert result.prompt_tokens == 100, "a redelivery must not double the total"


def test_two_events_about_one_run_are_both_recorded() -> None:
    """Keying on run_id would collapse a correction into the original."""
    run_id = uuid.uuid4()

    async def two_events(session):
        await record_usage(
            session,
            tenant_id=uuid.UUID(TENANT_A),
            event_id=uuid.uuid4(),
            run_id=run_id,
            prompt_tokens=100,
        )
        await record_usage(
            session,
            tenant_id=uuid.UUID(TENANT_A),
            event_id=uuid.uuid4(),
            run_id=run_id,
            prompt_tokens=50,
        )
        await session.commit()

    _run(_with_session(TENANT_A, two_events))

    async def rollup(session):
        return await monthly_rollup(session, tenant_id=uuid.UUID(TENANT_A))

    result = _run(_with_session(TENANT_A, rollup))
    assert result.usage_entries == 2
    assert result.prompt_tokens == 150


# --- Append-only ------------------------------------------------------------


def test_the_ledger_cannot_be_updated_by_the_app_role() -> None:
    """Proves the grant, not the intent: a rewritable ledger is not a ledger."""

    async def write(session):
        await record_usage(
            session,
            tenant_id=uuid.UUID(TENANT_A),
            event_id=uuid.uuid4(),
            run_id=uuid.uuid4(),
            prompt_tokens=10,
        )
        await session.commit()

    _run(_with_session(TENANT_A, write))

    from sqlalchemy.exc import ProgrammingError

    admin = create_engine(ADMIN_URL)
    raises = False
    try:
        app = create_engine(APP_URL)
        with app.begin() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": TENANT_A})
            conn.execute(
                text("UPDATE billing_entries SET prompt_tokens = 999 WHERE tenant_id = :t"),
                {"t": TENANT_A},
            )
    except ProgrammingError:
        raises = True
    finally:
        admin.dispose()
    assert raises, "platform_app must not be able to UPDATE a ledger row"


def test_the_ledger_cannot_be_deleted_by_the_app_role() -> None:
    async def write(session):
        await record_usage(
            session,
            tenant_id=uuid.UUID(TENANT_A),
            event_id=uuid.uuid4(),
            run_id=uuid.uuid4(),
            prompt_tokens=10,
        )
        await session.commit()

    _run(_with_session(TENANT_A, write))

    from sqlalchemy.exc import ProgrammingError

    raises = False
    try:
        app = create_engine(APP_URL)
        with app.begin() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": TENANT_A})
            conn.execute(text("DELETE FROM billing_entries WHERE tenant_id = :t"), {"t": TENANT_A})
    except ProgrammingError:
        raises = True
    assert raises, "platform_app must not be able to DELETE a ledger row"


# --- Rollup arithmetic ------------------------------------------------------


def test_adjustments_subtract_from_the_period_total() -> None:
    async def scenario(session):
        await record_usage(
            session,
            tenant_id=uuid.UUID(TENANT_A),
            event_id=uuid.uuid4(),
            run_id=uuid.uuid4(),
            prompt_tokens=200,
        )
        await record_adjustment(
            session,
            tenant_id=uuid.UUID(TENANT_A),
            run_id=uuid.uuid4(),
            prompt_tokens_delta=50,
        )
        await session.commit()

    _run(_with_session(TENANT_A, scenario))

    async def rollup(session):
        return await monthly_rollup(session, tenant_id=uuid.UUID(TENANT_A))

    result = _run(_with_session(TENANT_A, rollup))
    assert result.usage_entries == 1
    assert result.adjustment_entries == 1
    assert result.prompt_tokens == 150


def test_a_rollup_never_reports_negative_consumption() -> None:
    """An over-applied correction is a data problem, not negative usage."""

    async def scenario(session):
        await record_usage(
            session,
            tenant_id=uuid.UUID(TENANT_A),
            event_id=uuid.uuid4(),
            run_id=uuid.uuid4(),
            prompt_tokens=10,
        )
        await record_adjustment(
            session,
            tenant_id=uuid.UUID(TENANT_A),
            run_id=uuid.uuid4(),
            prompt_tokens_delta=500,
        )
        await session.commit()

    _run(_with_session(TENANT_A, scenario))

    async def rollup(session):
        return await monthly_rollup(session, tenant_id=uuid.UUID(TENANT_A))

    result = _run(_with_session(TENANT_A, rollup))
    assert result.prompt_tokens == 0


def test_the_rollup_is_scoped_to_one_tenant_and_one_period() -> None:
    now = int(time.time())
    last_month = now - 40 * 24 * 3600

    async def scenario(session):
        await record_usage(
            session,
            tenant_id=uuid.UUID(TENANT_A),
            event_id=uuid.uuid4(),
            run_id=uuid.uuid4(),
            prompt_tokens=100,
            recorded_at=now,
        )
        await record_usage(
            session,
            tenant_id=uuid.UUID(TENANT_A),
            event_id=uuid.uuid4(),
            run_id=uuid.uuid4(),
            prompt_tokens=999,
            recorded_at=last_month,
        )
        await session.commit()

    async def other_tenant(session):
        # Written under TENANT_B's RLS binding. Attempting this from A's
        # session is refused by the policy, which is the behaviour asserted
        # separately below.
        await record_usage(
            session,
            tenant_id=uuid.UUID(TENANT_B),
            event_id=uuid.uuid4(),
            run_id=uuid.uuid4(),
            prompt_tokens=777,
            recorded_at=now,
        )
        await session.commit()

    _run(_with_session(TENANT_A, scenario))
    _run(_with_session(TENANT_B, other_tenant))

    async def rollup(session):
        return await monthly_rollup(session, tenant_id=uuid.UUID(TENANT_A), now=now)

    result = _run(_with_session(TENANT_A, rollup))
    assert result.usage_entries == 1
    assert result.prompt_tokens == 100, "another tenant and another month must be excluded"


def test_the_ledger_refuses_a_write_for_another_tenant() -> None:
    """RLS on the new table, proved by attempting a cross-tenant insert.

    The rollup-scoping test above would still pass if this table had no
    policy at all and every read happened to be filtered in Python. This one
    fails in that case, so the two together pin the isolation at the
    database rather than at the caller.
    """
    from sqlalchemy.exc import ProgrammingError

    async def cross_tenant_write(session):
        # Session is bound to TENANT_A; the row claims TENANT_B.
        return await record_usage(
            session,
            tenant_id=uuid.UUID(TENANT_B),
            event_id=uuid.uuid4(),
            run_id=uuid.uuid4(),
            prompt_tokens=1,
        )

    with pytest.raises(ProgrammingError):
        _run(_with_session(TENANT_A, cross_tenant_write))
