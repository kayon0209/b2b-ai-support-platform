"""Integration tests: outbox relay delivery semantics.

The transactional outbox is only half a pattern without a relay. These
tests pin the guarantees that make the other half worth having:

- a committed event is actually delivered, and marked sent;
- an event with no consumer is not retried forever;
- a failing handler is recorded and retried, then parked, never silently
  dropped;
- one bad event does not stop the rest of its batch;
- a handler sees the event's own tenant and trace, so dispatch cannot
  accidentally act on the wrong tenant.

Events are seeded directly (not through a router) so the relay is tested in
isolation from HTTP concerns.
"""

import os
import time
import uuid
from typing import Any

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import AsyncSession
from uuid6 import uuid7

from platform_core.db import session_scope
from platform_core.outbox import OutboxEvent, OutboxStatus
from worker.outbox_relay import (
    MAX_ATTEMPTS,
    OutboxOutcomeUnknown,
    OutboxRelay,
    pending_count,
)

pytestmark = pytest.mark.integration

ADMIN_URL = os.environ.get(
    "APP_ADMIN_DATABASE_URL",
    "postgresql+psycopg://platform:platform@localhost:5435/platform",
)

# Its own tenant id - see the note in `test_membership_resolution`. Sharing
# `...d1` with two other files meant the seed's `ON CONFLICT (slug)` guard did
# not cover the primary key, so the second file to run failed on
# `tenants_pkey`.
TENANT = "01900000-0000-7000-8000-0000000000d6"
SLUG = "outbox-relay"
EVENT_TYPE = "relay.test"


@pytest.fixture(autouse=True)
def _isolate_rows() -> None:
    """Make the outbox empty of claimable work before each test.

    The outbox is a global table the relay scans without a tenant filter
    (that is the point - one relay drains everything). So any queued row
    left behind by another suite is claimed in the same batch, and these
    tests' assertions on claim/park/unhandled counts start measuring other
    suites' rows instead of their own. That failure is intermittent and
    ordering-dependent, which is the worst kind: it looks like a code bug in
    the relay.

    Deleting only this module's rows is not enough. `test_outbox.py` and the
    case-lifecycle journey commit real `case.created` events, and a run that
    is interrupted mid-suite leaves them queued. Draining the whole table is
    safe here because these rows are test artefacts: production rows come
    from real traffic, not from a pytest run.
    """
    admin = create_engine(ADMIN_URL)
    _cleanup(admin)
    _assert_no_live_relay(admin)
    with admin.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO tenants (id, slug, name, status) VALUES "
                "(:id, :slug, 'Outbox', 'active') ON CONFLICT (slug) DO NOTHING"
            ),
            {"id": TENANT, "slug": SLUG},
        )
    admin.dispose()
    yield
    admin = create_engine(ADMIN_URL)
    _cleanup(admin)
    admin.dispose()


def _cleanup(admin: Any) -> None:
    with admin.begin() as conn:
        conn.execute(text("DELETE FROM outbox_events WHERE tenant_id = :t"), {"t": TENANT})
        conn.execute(text("DELETE FROM tenants WHERE slug = :s"), {"s": SLUG})
        # Stray queued rows from other suites would be claimed by our relay.
        # `sent` rows are inert (claim_pending filters on queued), so only the
        # claimable ones have to go.
        conn.execute(text("DELETE FROM outbox_events WHERE status = 'queued'"))


def _assert_no_live_relay(admin: Any) -> None:
    """Fail loudly if a running worker is draining the outbox.

    The relay scans the outbox globally, which is correct in production but
    means a `docker compose up` worker will claim rows this suite just
    seeded. The symptom is a bewildering `claimed == 1` instead of 3, which
    reads like a code bug. Check the precondition first and say what it is.

    Detection is empirical rather than connection-based: a sentinel row is
    seeded and we watch whether it disappears. `client_addr` is NULL for
    local connections (which on Windows includes the test process itself),
    so there is no reliable way to tell the two apart from the catalog.
    """
    probe = uuid7()
    with admin.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO outbox_events (id, tenant_id, event_id, event_type, "
                "event_version, aggregate_type, aggregate_id, payload, status, "
                "created_at, attempts, trace_id) VALUES "
                "(:id, :t, :e, 'relay.probe', 1, 'case', :agg, "
                "'{}'::jsonb, 'queued', :now, 0, 'probe')"
            ),
            {
                "id": str(probe),
                "t": TENANT,
                "e": str(uuid.uuid4()),
                "agg": str(uuid.uuid4()),
                "now": int(time.time()),
            },
        )
    time.sleep(2.0)
    with admin.connect() as conn:
        remaining = conn.execute(
            text("SELECT count(*) FROM outbox_events WHERE id = :id"), {"id": str(probe)}
        ).scalar_one()
        still_queued = conn.execute(
            text("SELECT status FROM outbox_events WHERE id = :id"), {"id": str(probe)}
        ).scalar_one_or_none()
    with admin.begin() as conn:
        conn.execute(text("DELETE FROM outbox_events WHERE id = :id"), {"id": str(probe)})

    if remaining == 0 or still_queued != OutboxStatus.QUEUED.value:
        pytest.skip(
            "a live worker/relay is consuming outbox rows; "
            "`docker compose stop ai-worker-interactive ai-worker-outbox` "
            "before running this suite"
        )


def _seed_event(
    *,
    status: str = OutboxStatus.QUEUED.value,
    attempts: int = 0,
    event_type: str = EVENT_TYPE,
    trace_id: str = "tr-relay",
) -> uuid.UUID:
    """Insert one outbox row directly, bypassing the service layer."""
    event_id = uuid.uuid4()
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO outbox_events (id, tenant_id, event_id, event_type, "
                "event_version, aggregate_type, aggregate_id, payload, status, "
                "created_at, attempts, trace_id) VALUES "
                "(:id, :tid, :eid, :etype, 1, 'case', :agg, "
                "CAST(:payload AS jsonb), :status, :created, :attempts, :trace)"
            ),
            {
                # UUIDv7, not `gen_random_uuid()`: the relay claims by primary
                # key (`claim_pending` -> `ORDER BY OutboxEvent.id LIMIT batch`),
                # and production ids are v7. A random v4 id makes the relay's
                # own ordering meaningless under test, so the suite stops
                # pinning the FIFO the relay actually depends on.
                "id": str(uuid7()),
                "tid": TENANT,
                "eid": str(event_id),
                "etype": event_type,
                "agg": str(uuid.uuid4()),
                "payload": '{"case_id": "abc"}',
                "status": status,
                "created": int(time.time()),
                "attempts": attempts,
                "trace": trace_id,
            },
        )
    admin.dispose()
    return event_id


async def _row_status(event_id: uuid.UUID) -> tuple[str, int, str | None]:
    """Read back (status, attempts, last_error) as admin, bypassing RLS."""
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        row = conn.execute(
            text("SELECT status, attempts, last_error FROM outbox_events WHERE event_id = :eid"),
            {"eid": str(event_id)},
        ).one()
    admin.dispose()
    return row[0], row[1], row[2]


@pytest.mark.asyncio
async def test_queued_event_is_delivered_and_marked_sent() -> None:
    """The core promise: a committed event reaches its handler exactly once."""
    seen: list[OutboxEvent] = []

    async def handler(session: AsyncSession, event: OutboxEvent) -> None:
        seen.append(event)

    event_id = _seed_event()
    relay = OutboxRelay(handlers={EVENT_TYPE: handler})

    # The fixture drains the table of queued rows first, so this batch really
    # does contain exactly the one event seeded here.
    async with session_scope() as session:
        stats = await relay.run_once(session)

    assert stats.claimed == 1
    assert stats.sent == 1
    assert len(seen) == 1

    mine = [e for e in seen if str(e.event_id) == str(event_id)]
    assert len(mine) == 1, "our event must be the one delivered"

    # The handler saw the real tenant and trace, not a placeholder.
    assert str(mine[0].tenant_id) == TENANT
    assert mine[0].trace_id == "tr-relay"

    status, attempts, _ = await _row_status(event_id)
    assert status == OutboxStatus.SENT.value
    assert attempts == 1  # claim bumped it


@pytest.mark.asyncio
async def test_event_with_no_handler_is_not_retried_forever() -> None:
    """An event nobody consumes is marked sent, not left to accumulate.

    Retrying an event with no audience would pin the queue indefinitely and
    make `pending_count` useless as an operational signal.
    """
    event_id = _seed_event(event_type="nobody.listens")
    relay = OutboxRelay(handlers={})

    async with session_scope() as session:
        stats = await relay.run_once(session)

    assert stats.unhandled == 1
    assert stats.sent == 0
    assert (await _row_status(event_id))[0] == OutboxStatus.SENT.value


@pytest.mark.asyncio
async def test_failing_handler_records_error_and_stays_queued() -> None:
    """A delivery failure is recorded and retried, never silently dropped."""

    async def boom(session: AsyncSession, event: OutboxEvent) -> None:
        raise RuntimeError("downstream refused")

    event_id = _seed_event(event_type="case.created")
    relay = OutboxRelay(handlers={"case.created": boom})

    async with session_scope() as session:
        stats = await relay.run_once(session)

    assert stats.failed == 1
    assert stats.sent == 0

    status, attempts, last_error = await _row_status(event_id)
    assert status == OutboxStatus.QUEUED.value  # still deliverable
    assert attempts == 1
    assert last_error == "RuntimeError"


@pytest.mark.asyncio
async def test_retryable_outbox_handler_reuses_total_budget_and_absolute_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Five safe deliveries share one persisted event cap and deadline."""
    from platform_core import config as config_module
    from platform_core.config import Settings
    from platform_core.execution_budget import current_execution_budget

    monkeypatch.setattr(
        config_module,
        "get_settings",
        lambda: Settings(
            environment="test",
            allow_bootstrap_tokens=True,
            outbox_job_deadline_seconds=300,
            outbox_event_max_external_attempts=20,
            outbox_handler_deadline_seconds=30,
            outbox_handler_max_external_attempts=4,
        ),
    )
    event_id = _seed_event(event_type="case.created")
    delivery_attempts: list[tuple[int, int]] = []
    external_attempts = 0

    async def retry_four_times(session: AsyncSession, event: OutboxEvent) -> None:
        nonlocal external_attempts
        budget = current_execution_budget()
        assert budget is not None
        delivery_attempts.append((budget.max_attempts, budget.operation_limits["model"]))
        for _ in range(budget.operation_limits["model"]):
            budget.reserve_attempt("model")
            external_attempts += 1
        if len(delivery_attempts) < MAX_ATTEMPTS:
            raise RuntimeError("synthetic retryable failure")

    relay = OutboxRelay(handlers={"case.created": retry_four_times})
    persisted_budget: tuple[int | None, int | None, int | None] | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        async with session_scope() as session:
            result = await relay.run_once(session)
        assert result.claimed == 1
        status, claimed_attempts, _error = await _row_status(event_id)
        assert claimed_attempts == attempt
        if attempt < MAX_ATTEMPTS:
            assert status == OutboxStatus.QUEUED.value
        else:
            assert status == OutboxStatus.SENT.value
        admin = create_engine(ADMIN_URL)
        with admin.connect() as conn:
            values = conn.execute(
                text(
                    "SELECT first_attempt_at, deadline_at, external_attempt_limit "
                    "FROM outbox_events WHERE event_id = :event"
                ),
                {"event": str(event_id)},
            ).one()
        admin.dispose()
        current_budget = (values[0], values[1], values[2])
        if persisted_budget is None:
            persisted_budget = current_budget
        assert current_budget == persisted_budget

    assert delivery_attempts == [(4, 4)] * MAX_ATTEMPTS
    assert external_attempts == 20
    assert persisted_budget is not None
    first_attempt_at, deadline_at, external_limit = persisted_budget
    assert deadline_at == first_attempt_at + 300
    assert external_limit == 20


@pytest.mark.asyncio
async def test_expired_queued_outbox_job_is_failed_without_a_handler_call() -> None:
    event_id = _seed_event(event_type="case.created")
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        conn.execute(
            text(
                "UPDATE outbox_events SET first_attempt_at = :first, deadline_at = :deadline, "
                "external_attempt_limit = 20 WHERE event_id = :event"
            ),
            {
                "first": int(time.time()) - 301,
                "deadline": int(time.time()) - 1,
                "event": str(event_id),
            },
        )
    admin.dispose()
    calls = 0

    async def must_not_run(session: AsyncSession, event: OutboxEvent) -> None:
        nonlocal calls
        calls += 1

    relay = OutboxRelay(handlers={"case.created": must_not_run})
    async with session_scope() as session:
        result = await relay.run_once(session)

    status, attempts, last_error = await _row_status(event_id)
    assert result.claimed == 0
    assert result.failed == 1
    assert calls == 0
    assert status == OutboxStatus.FAILED.value
    assert attempts == 0
    assert last_error == "outbox_job_deadline_exhausted"


@pytest.mark.asyncio
async def test_row_over_attempt_budget_is_parked() -> None:
    """Past the retry budget a row stops consuming relay cycles."""
    event_id = _seed_event(attempts=MAX_ATTEMPTS + 1)
    calls: list[str] = []

    async def handler(session: AsyncSession, event: OutboxEvent) -> None:
        calls.append("called")

    relay = OutboxRelay(handlers={EVENT_TYPE: handler})

    async with session_scope() as session:
        stats = await relay.run_once(session)

    assert stats.parked == 1
    assert calls == []  # the handler was never invoked
    # Still queued, so an operator can requeue it deliberately.
    status, attempts, _ = await _row_status(event_id)
    assert status == OutboxStatus.QUEUED.value
    assert attempts == MAX_ATTEMPTS + 1

    # Parked rows are counted for operators but no longer claimed or bumped
    # on every poll cycle.
    async with session_scope() as session:
        repeated = await relay.run_once(session)
    assert repeated.parked == 1
    assert repeated.claimed == 0
    assert (await _row_status(event_id))[1] == attempts


@pytest.mark.asyncio
async def test_external_unknown_is_terminal_and_never_replayed() -> None:
    event_id = _seed_event()
    calls = 0

    async def ambiguous(session: AsyncSession, event: OutboxEvent) -> None:
        nonlocal calls
        calls += 1
        raise OutboxOutcomeUnknown("provider may have accepted the side effect")

    relay = OutboxRelay(handlers={EVENT_TYPE: ambiguous})
    async with session_scope() as session:
        result = await relay.run_once(session)
    assert result.failed == 1
    status, attempts, last_error = await _row_status(event_id)
    assert status == OutboxStatus.FAILED.value
    assert attempts == 1
    assert last_error == "outbox_delivery_outcome_unknown"

    async with session_scope() as session:
        replay = await relay.run_once(session)
    assert replay.claimed == 0
    assert calls == 1


@pytest.mark.asyncio
async def test_crashed_external_reply_claim_is_not_automatically_requeued() -> None:
    event_id = _seed_event(
        status=OutboxStatus.PROCESSING.value,
        attempts=1,
        event_type="conversation.agent_reply",
    )
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        conn.execute(
            text(
                "UPDATE outbox_events SET processing_started_at = :stale, "
                "processing_token = :token WHERE event_id = :event"
            ),
            {
                "stale": int(time.time()) - 3600,
                "token": uuid.uuid4(),
                "event": str(event_id),
            },
        )
    admin.dispose()
    calls = 0

    async def should_not_replay(session: AsyncSession, event: OutboxEvent) -> None:
        nonlocal calls
        calls += 1

    relay = OutboxRelay(handlers={"conversation.agent_reply": should_not_replay})
    async with session_scope() as session:
        result = await relay.run_once(session)

    status, attempts, last_error = await _row_status(event_id)
    assert result.claimed == 0
    assert result.failed == 1
    assert calls == 0
    assert status == OutboxStatus.FAILED.value
    assert attempts == 1
    assert last_error == "outbox_delivery_outcome_unknown"


@pytest.mark.asyncio
async def test_stale_claim_replays_database_work_but_parks_external_reply() -> None:
    safe_id = _seed_event(
        status=OutboxStatus.PROCESSING.value,
        attempts=1,
        event_type="case.created",
    )
    unsafe_id = _seed_event(
        status=OutboxStatus.PROCESSING.value,
        attempts=1,
        event_type="conversation.agent_reply",
    )
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        conn.execute(
            text(
                "UPDATE outbox_events SET processing_started_at = :stale, "
                "processing_token = :token WHERE event_id IN (:safe, :unsafe)"
            ),
            {
                "stale": int(time.time()) - 3600,
                "token": uuid.uuid4(),
                "safe": str(safe_id),
                "unsafe": str(unsafe_id),
            },
        )
    admin.dispose()
    delivered: list[uuid.UUID] = []

    async def safe_handler(session: AsyncSession, event: OutboxEvent) -> None:
        delivered.append(event.event_id)

    relay = OutboxRelay(handlers={"case.created": safe_handler})
    async with session_scope() as session:
        stats = await relay.run_once(session)

    assert stats.claimed == 1
    assert stats.sent == 1
    assert stats.failed == 1
    assert delivered == [safe_id]
    assert (await _row_status(safe_id))[0] == OutboxStatus.SENT.value
    unsafe_status, unsafe_attempts, unsafe_error = await _row_status(unsafe_id)
    assert unsafe_status == OutboxStatus.FAILED.value
    assert unsafe_attempts == 1
    assert unsafe_error == "outbox_delivery_outcome_unknown"


def _seed_event_with_payload(payload: str, *, event_type: str = EVENT_TYPE) -> uuid.UUID:
    """Seed one queued row with an explicit payload."""
    event_id = uuid.uuid4()
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO outbox_events (id, tenant_id, event_id, event_type, "
                "event_version, aggregate_type, aggregate_id, payload, status, "
                "created_at, attempts, trace_id) VALUES "
                "(:id, :tid, :eid, :etype, 1, 'case', :agg, "
                "CAST(:payload AS jsonb), 'queued', :created, 0, 'tr-batch')"
            ),
            {
                # UUIDv7 for the same reason as `_seed_event`: the relay claims
                # in primary-key order, so the id has to be time-sortable for
                # the test to exercise the ordering production relies on.
                "id": str(uuid7()),
                "tid": TENANT,
                "eid": str(event_id),
                "etype": event_type,
                "agg": str(uuid.uuid4()),
                "payload": payload,
                "created": int(time.time()),
            },
        )
    admin.dispose()
    return event_id


@pytest.mark.asyncio
async def test_one_failing_event_does_not_block_the_batch() -> None:
    """A failure is contained: siblings still deliver in the same cycle.

    Without per-event error containment, one poison event would roll back
    or halt the batch and a single bad payload could stop all delivery.
    """
    delivered: list[str] = []

    async def selective(session: AsyncSession, event: OutboxEvent) -> None:
        if event.payload.get("fail"):
            raise RuntimeError("nope")
        delivered.append(str(event.event_id))

    bad = _seed_event_with_payload('{"fail": true}', event_type="case.created")
    good_a = _seed_event_with_payload('{"fail": false}', event_type="case.created")
    good_b = _seed_event_with_payload('{"fail": false}', event_type="case.created")

    relay = OutboxRelay(handlers={"case.created": selective})
    async with session_scope() as session:
        stats = await relay.run_once(session)

    assert stats.claimed == 3
    assert stats.failed == 1
    assert stats.sent == 2
    assert len(delivered) == 2

    # The failing row is still queued for retry; the others are done.
    assert (await _row_status(bad))[0] == OutboxStatus.QUEUED.value
    assert (await _row_status(good_a))[0] == OutboxStatus.SENT.value
    assert (await _row_status(good_b))[0] == OutboxStatus.SENT.value


@pytest.mark.asyncio
async def test_already_sent_events_are_not_reclaimed() -> None:
    """The relay only picks up queued rows."""
    event_id = _seed_event(status=OutboxStatus.SENT.value)
    calls: list[str] = []

    async def handler(session: AsyncSession, event: OutboxEvent) -> None:
        calls.append("called")

    relay = OutboxRelay(handlers={EVENT_TYPE: handler})

    async with session_scope() as session:
        stats = await relay.run_once(session)

    assert stats.claimed == 0
    assert calls == []
    assert (await _row_status(event_id))[0] == OutboxStatus.SENT.value


@pytest.mark.asyncio
async def test_empty_outbox_is_a_no_op() -> None:
    relay = OutboxRelay(handlers={EVENT_TYPE: lambda s, e: None})  # type: ignore[arg-type]
    async with session_scope() as session:
        stats = await relay.run_once(session)
    assert stats.processed == 0


@pytest.mark.asyncio
async def test_pending_count_reflects_queued_rows() -> None:
    """pending_count is the operational signal for relay health."""
    _seed_event()
    _seed_event()
    _seed_event(status=OutboxStatus.SENT.value)

    async with session_scope() as session:
        count = await pending_count(session)

    # Two queued rows exist; the sent one is excluded. The count is global
    # because the relay drains globally, so assert the delta rather than
    # exact equality.
    assert count >= 2
