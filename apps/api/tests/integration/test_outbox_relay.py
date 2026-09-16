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

from platform_core.db import session_scope
from platform_core.outbox import OutboxEvent, OutboxStatus
from worker.outbox_relay import (
    MAX_ATTEMPTS,
    OutboxRelay,
    pending_count,
)

pytestmark = pytest.mark.integration

ADMIN_URL = os.environ.get(
    "APP_ADMIN_DATABASE_URL",
    "postgresql+psycopg://platform:platform@localhost:5435/platform",
)

TENANT = "01900000-0000-7000-8000-0000000000d1"
SLUG = "outbox-relay"
EVENT_TYPE = "relay.test"


@pytest.fixture(autouse=True)
def _isolate_rows() -> None:
    """Clear this suite's rows before each test.

    The outbox is a global table the relay scans without a tenant filter
    (that is the point — one relay drains everything). So a row left by an
    earlier test in this module would be claimed by the next one, and any
    assertion on claim counts would be measuring the wrong thing.
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
    probe = uuid.uuid4()
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
                "(gen_random_uuid(), :tid, :eid, :etype, 1, 'case', :agg, "
                "CAST(:payload AS jsonb), :status, :created, :attempts, :trace)"
            ),
            {
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

    async with session_scope() as session:
        stats = await relay.run_once(session)

    assert stats.claimed == 1
    assert stats.sent == 1
    assert len(seen) == 1

    # The handler saw the real tenant and trace, not a placeholder.
    assert str(seen[0].tenant_id) == TENANT
    assert seen[0].trace_id == "tr-relay"

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

    event_id = _seed_event()
    relay = OutboxRelay(handlers={EVENT_TYPE: boom})

    async with session_scope() as session:
        stats = await relay.run_once(session)

    assert stats.failed == 1
    assert stats.sent == 0

    status, attempts, last_error = await _row_status(event_id)
    assert status == OutboxStatus.QUEUED.value  # still deliverable
    assert attempts == 1
    assert last_error is not None
    assert "downstream refused" in last_error


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
    assert (await _row_status(event_id))[0] == OutboxStatus.QUEUED.value


def _seed_event_with_payload(payload: str) -> uuid.UUID:
    """Seed one queued row with an explicit payload."""
    event_id = uuid.uuid4()
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO outbox_events (id, tenant_id, event_id, event_type, "
                "event_version, aggregate_type, aggregate_id, payload, status, "
                "created_at, attempts, trace_id) VALUES "
                "(gen_random_uuid(), :tid, :eid, :etype, 1, 'case', :agg, "
                "CAST(:payload AS jsonb), 'queued', :created, 0, 'tr-batch')"
            ),
            {
                "tid": TENANT,
                "eid": str(event_id),
                "etype": EVENT_TYPE,
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

    bad = _seed_event_with_payload('{"fail": true}')
    good_a = _seed_event_with_payload('{"fail": false}')
    good_b = _seed_event_with_payload('{"fail": false}')

    relay = OutboxRelay(handlers={EVENT_TYPE: selective})
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
