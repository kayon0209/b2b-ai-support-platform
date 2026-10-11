"""Integration tests: stuck-claim recovery for the inbox consumer.

A worker that claims a row (status PROCESSING) and then dies leaves it
PROCESSING forever — nothing else transitions it. That silently drops the
customer's question, which is the exact failure the durable-inbox design
exists to prevent.

These tests drive the real database: claiming is a row-status transition
inside a transaction, so an in-memory fake would not exercise the thing
that actually breaks.
"""

import json
import os
import selectors
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from worker.inbox_consumer import (
    STALE_PROCESSING_SECONDS,
    claim_events,
    mark_completed,
    reclaim_stale_processing,
)

pytestmark = pytest.mark.integration

ADMIN_URL = os.environ.get(
    "APP_ADMIN_DATABASE_URL",
    "postgresql+psycopg://platform:platform@localhost:5435/platform",
)
TENANT = uuid.UUID("01900000-0000-7000-8000-00000000dead")
_CRASHED_WORKER_BOOTSTRAP = """
import asyncio
import sys
import uuid
from platform_core.db import get_session_factory
from worker.inbox_consumer import claim_events

event_id = uuid.UUID(sys.argv[1])

async def main():
    async with get_session_factory()() as session:
        claimed = await claim_events(session, batch=1)
        await session.commit()
    if len(claimed) != 1 or claimed[0].event_id != event_id:
        raise SystemExit(3)
    await asyncio.Event().wait()

asyncio.run(main())
"""
_RECOVERED_WORKER_BOOTSTRAP = """
import asyncio
import sys
import uuid
from platform_core.db import get_session_factory
from worker.inbox_consumer import claim_events, reclaim_stale_processing

event_id = uuid.UUID(sys.argv[1])

async def main():
    factory = get_session_factory()
    async with factory() as session:
        reclaimed = await reclaim_stale_processing(session, timeout_seconds=30)
        await session.commit()
    async with factory() as session:
        claimed = await claim_events(session, batch=1)
        await session.commit()
    found = any(row.event_id == event_id for row in claimed)
    print(f"reclaimed={reclaimed}; target_claimed={str(found).lower()}")
    if not found:
        raise SystemExit(4)

asyncio.run(main())
"""
_CONCURRENT_CLAIM_BOOTSTRAP = """
import asyncio
import json
import sys
from sqlalchemy import text
from platform_core.db import get_session_factory
from worker.inbox_consumer import claim_events

print("READY", flush=True)
if sys.stdin.readline().strip() != "go":
    raise SystemExit(5)

async def main():
    async with get_session_factory()() as session:
        claimed = await claim_events(session, batch=1)
        await session.commit()
    print(json.dumps([str(row.event_id) for row in claimed]), flush=True)

asyncio.run(main())
"""


@pytest.fixture
def admin() -> object:
    engine = create_engine(ADMIN_URL)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO tenants (id, slug, name, status) VALUES "
                "(:id, 'reclaim-test', 'Reclaim Tenant', 'active') "
                "ON CONFLICT (slug) DO NOTHING"
            ),
            {"id": str(TENANT)},
        )
        # The inbox table is global (the relay scans cross-tenant on
        # purpose), so each test must clean up after itself rather than
        # rely on tenant scoping.
        conn.execute(text("DELETE FROM inbox_events WHERE tenant_id = :tid"), {"tid": str(TENANT)})
    yield engine
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM inbox_events WHERE tenant_id = :tid"), {"tid": str(TENANT)})
    engine.dispose()


def _insert(
    admin: object,
    *,
    status: str,
    received_at: int,
    tag: str,
    claimed_at: int | None = None,
    heartbeat_at: int | None = None,
    conversation_ref: uuid.UUID | None = None,
    event_id: uuid.UUID | None = None,
) -> uuid.UUID:
    event_id = event_id or uuid.uuid4()
    payload = {"conversation_ref": str(conversation_ref)} if conversation_ref is not None else {}
    with admin.begin() as conn:  # type: ignore[attr-defined]
        conn.execute(
            text(
                "INSERT INTO inbox_events "
                "(id, tenant_id, delivery_id, event_type, payload_hash, "
                " minimized_payload, status, received_at, claimed_at, heartbeat_at) VALUES "
                "(:id, :tid, :did, 'message_created', 'h', CAST(:payload AS jsonb), "
                ":st, :ra, :ca, :hb)"
            ),
            {
                "id": str(event_id),
                "tid": str(TENANT),
                "did": f"reclaim-{tag}-{event_id}",
                "payload": json.dumps(payload),
                "st": status,
                "ra": received_at,
                "ca": claimed_at,
                "hb": heartbeat_at,
            },
        )
    return event_id


def _status_of(admin: object, event_id: uuid.UUID) -> str:
    with admin.connect() as conn:  # type: ignore[attr-defined]
        return str(
            conn.execute(
                text("SELECT status FROM inbox_events WHERE id = :id"), {"id": str(event_id)}
            ).scalar_one()
        )


async def test_stale_processing_row_is_returned_to_received(admin: object) -> None:
    """The core guarantee: an abandoned claim gets retried, not lost."""
    from platform_core.db import get_session_factory

    stale = _insert(admin, status="processing", received_at=int(time.time()) - 3600, tag="stale")

    async with get_session_factory()() as session:
        reclaimed = await reclaim_stale_processing(session)
        await session.commit()

    assert reclaimed >= 1
    assert _status_of(admin, stale) == "received"


async def test_fresh_processing_row_is_left_alone(admin: object) -> None:
    """An in-flight run must not have its claim stolen.

    A knowledge answer takes ~30 s; reclaiming too eagerly would start a
    second run for a question that is already being answered, producing a
    duplicate reply.
    """
    from platform_core.db import get_session_factory

    in_flight = _insert(
        admin, status="processing", received_at=int(time.time()) - 5, tag="inflight"
    )

    async with get_session_factory()() as session:
        await reclaim_stale_processing(session)
        await session.commit()

    assert _status_of(admin, in_flight) == "processing"


async def test_just_claimed_row_is_not_reclaimed_even_when_enqueued_long_ago(
    admin: object,
) -> None:
    """The defect this suite could not previously express.

    A backlog is the precondition. An event sits in RECEIVED for longer than
    the reclaim threshold, a worker finally claims it, and the *next* poll
    cycle looks at it again - and because the old predicate compared
    `received_at` rather than the claim time, the row was handed straight back
    to the queue while the first worker was still processing it. Two workers,
    one customer message: duplicated model calls, duplicated tool executions,
    duplicated spend. The customer-facing duplicate is suppressed downstream by
    the outbound command id; the cost is not.

    Reproduced against the live stack before the fix: a row enqueued 11 minutes
    earlier and claimed a moment ago came back as `received` on the next poll.
    """
    from platform_core.db import get_session_factory

    now = int(time.time())
    # Enqueued well past the threshold, claimed and heartbeated just now.
    claimed = _insert(
        admin,
        status="processing",
        received_at=now - 3600,
        claimed_at=now,
        heartbeat_at=now,
        tag="claimed-now",
    )
    # Control: same age, but the claim itself is stale - a dead worker. This
    # one must still be recovered, or the fix would trade a duplicate for a
    # silent drop.
    dead = _insert(
        admin,
        status="processing",
        received_at=now - 3600,
        claimed_at=now - 3600,
        heartbeat_at=now - 3600,
        tag="claim-stale",
    )

    async with get_session_factory()() as session:
        reclaimed = await reclaim_stale_processing(session)
        await session.commit()

    assert _status_of(admin, claimed) == "processing", "a live claim was stolen"
    assert _status_of(admin, dead) == "received", "a dead claim was not recovered"
    assert reclaimed >= 1


async def test_heartbeat_keeps_a_long_run_out_of_reclaim(admin: object) -> None:
    """A run that outlives the threshold stays claimed while it heartbeats.

    Without this, a slow-but-healthy worker is indistinguishable from a dead
    one, and the only way to tell them apart is the claim timestamp - which a
    long run keeps moving.
    """
    from platform_core.db import get_session_factory

    now = int(time.time())
    long_run = _insert(
        admin,
        status="processing",
        received_at=now - 3600,
        claimed_at=now - 3600,
        heartbeat_at=now,  # heartbeating
        tag="long-run",
    )

    async with get_session_factory()() as session:
        await reclaim_stale_processing(session, timeout_seconds=30)
        await session.commit()

    assert _status_of(admin, long_run) == "processing"


async def test_terminal_rows_are_never_reclaimed(admin: object) -> None:
    """completed/failed are decisions, not claims. Reopening them would
    re-answer questions the platform already settled."""
    from platform_core.db import get_session_factory

    completed = _insert(admin, status="completed", received_at=int(time.time()) - 3600, tag="done")
    failed = _insert(admin, status="failed", received_at=int(time.time()) - 3600, tag="fail")

    async with get_session_factory()() as session:
        await reclaim_stale_processing(session)
        await session.commit()

    assert _status_of(admin, completed) == "completed"
    assert _status_of(admin, failed) == "failed"


async def test_reclaim_timeout_is_configurable(admin: object) -> None:
    """The threshold must be tunable without a code change, because the
    right value depends on the slowest deployed model."""
    from platform_core.db import get_session_factory

    row = _insert(admin, status="processing", received_at=int(time.time()) - 60, tag="tunable")

    async with get_session_factory()() as session:
        # Not old enough under the default, but old enough for a 30 s budget.
        await reclaim_stale_processing(session, timeout_seconds=STALE_PROCESSING_SECONDS)
        await session.commit()
    assert _status_of(admin, row) == "processing"

    async with get_session_factory()() as session:
        await reclaim_stale_processing(session, timeout_seconds=30)
        await session.commit()
    assert _status_of(admin, row) == "received"


async def test_stale_claim_at_attempt_limit_is_failed_not_requeued(admin: object) -> None:
    from platform_core.db import get_session_factory

    row = _insert(
        admin,
        status="processing",
        received_at=int(time.time()) - 3600,
        claimed_at=int(time.time()) - 3600,
        heartbeat_at=int(time.time()) - 3600,
        tag="retry-budget-exhausted",
    )
    with admin.begin() as conn:  # type: ignore[attr-defined]
        conn.execute(
            text("UPDATE inbox_events SET attempts = 2 WHERE id = :id"),
            {"id": str(row)},
        )

    async with get_session_factory()() as session:
        reclaimed = await reclaim_stale_processing(
            session,
            timeout_seconds=30,
            max_attempts=2,
        )
        await session.commit()

    assert reclaimed == 0
    assert _status_of(admin, row) == "failed"


def test_worker_process_kill_recovers_and_reclaims_its_committed_inbox_event(
    admin: object,
) -> None:
    """A replacement process recovers a committed claim left by a killed worker."""
    event_id = _insert(
        admin,
        status="received",
        received_at=-(2**62),
        tag="worker-hard-exit",
        event_id=uuid.uuid4(),
    )
    root = Path(__file__).resolve().parents[4]
    environment = os.environ.copy()
    source_roots = (
        root / "apps/api/src",
        root / "apps/worker/src",
        root / "packages/policy/src",
        root / "packages/contracts/src",
        root / "packages/observability/src",
        root,
    )
    pythonpath = [str(path) for path in source_roots]
    if environment.get("PYTHONPATH"):
        pythonpath.append(environment["PYTHONPATH"])
    environment["PYTHONPATH"] = os.pathsep.join(pythonpath)

    # This is the owner-role queue-bookkeeping session used to claim across
    # tenants before the worker knows which tenant to bind for processing.
    worker = subprocess.Popen(  # noqa: S603
        [sys.executable, "-c", _CRASHED_WORKER_BOOTSTRAP, str(event_id)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        env=environment,
    )
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if worker.poll() is not None:
                pytest.fail("worker exited before committing the inbox claim")
            if _status_of(admin, event_id) == "processing":
                break
            time.sleep(0.05)
        else:
            pytest.fail("worker did not commit its inbox claim within 10 seconds")

        with admin.connect() as conn:  # type: ignore[attr-defined]
            attempts, first_started_at = conn.execute(
                text("SELECT attempts, first_started_at FROM inbox_events WHERE id = :id"),
                {"id": str(event_id)},
            ).one()
        assert attempts == 1
        assert first_started_at is not None

        worker.kill()
        worker.wait(timeout=5)
        assert _status_of(admin, event_id) == "processing"

        # Age the liveness stamp instead of waiting for the production
        # ten-minute reclaim window during integration tests.
        with admin.begin() as conn:  # type: ignore[attr-defined]
            conn.execute(
                text(
                    "UPDATE inbox_events SET claimed_at = :old, heartbeat_at = :old "
                    "WHERE id = :id AND status = 'processing'"
                ),
                {"old": int(time.time()) - 60, "id": str(event_id)},
            )
        recovered = subprocess.run(  # noqa: S603
            [sys.executable, "-c", _RECOVERED_WORKER_BOOTSTRAP, str(event_id)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            env=environment,
            text=True,
            timeout=15,
            check=False,
        )
        assert recovered.returncode == 0, "replacement worker did not recover the claim"
        assert "target_claimed=true" in recovered.stdout
        assert _status_of(admin, event_id) == "processing"
        with admin.connect() as conn:  # type: ignore[attr-defined]
            retried_attempts, retry_first_started_at = conn.execute(
                text("SELECT attempts, first_started_at FROM inbox_events WHERE id = :id"),
                {"id": str(event_id)},
            ).one()
        assert retried_attempts == 2
        assert retry_first_started_at == first_started_at
    finally:
        if worker.poll() is None:
            worker.kill()
            worker.wait(timeout=5)


def test_two_worker_processes_never_claim_the_same_inbox_event(admin: object) -> None:
    """Separate worker processes contend on a real SKIP LOCKED inbox claim."""
    target = _insert(
        admin,
        status="received",
        received_at=-(2**62),
        tag="concurrent-workers",
        event_id=uuid.uuid4(),
    )
    with admin.begin() as conn:  # type: ignore[attr-defined]
        conn.execute(text("DROP TRIGGER IF EXISTS phase2_pause_inbox_claim ON inbox_events"))
        conn.execute(text("DROP FUNCTION IF EXISTS phase2_pause_inbox_claim()"))
        conn.execute(
            text(
                "CREATE FUNCTION phase2_pause_inbox_claim() RETURNS trigger "
                "LANGUAGE plpgsql AS $$ BEGIN "
                "IF OLD.status = 'received' AND NEW.status = 'processing' "
                "THEN PERFORM pg_sleep(0.8); END IF; "
                "RETURN NEW; END $$"
            )
        )
        conn.execute(
            text(
                "CREATE TRIGGER phase2_pause_inbox_claim BEFORE UPDATE OF status "
                "ON inbox_events FOR EACH ROW EXECUTE FUNCTION "
                "phase2_pause_inbox_claim()"
            )
        )

    root = Path(__file__).resolve().parents[4]
    environment = os.environ.copy()
    source_roots = (
        root / "apps/api/src",
        root / "apps/worker/src",
        root / "packages/policy/src",
        root / "packages/contracts/src",
        root / "packages/observability/src",
        root,
    )
    environment["PYTHONPATH"] = os.pathsep.join(
        [*(str(path) for path in source_roots), environment.get("PYTHONPATH", "")]
    )
    workers = [
        subprocess.Popen(  # noqa: S603 - controlled local worker child
            [sys.executable, "-c", _CONCURRENT_CLAIM_BOOTSTRAP],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            close_fds=True,
            env=environment,
            text=True,
        )
        for _ in range(2)
    ]
    try:
        for worker in workers:
            if worker.stdout is None:
                pytest.fail("worker stdout was not captured")
            with selectors.DefaultSelector() as selector:
                selector.register(worker.stdout, selectors.EVENT_READ)
                if not selector.select(timeout=10):
                    pytest.fail("worker did not reach the synchronized claim barrier")
            assert worker.stdout.readline().strip() == "READY"
        for worker in workers:
            assert worker.stdin is not None
            worker.stdin.write("go\n")
            worker.stdin.flush()
            worker.stdin.close()
            worker.stdin = None

        claimed_sets: list[list[str]] = []
        for worker in workers:
            stdout, stderr = worker.communicate(timeout=15)
            assert worker.returncode == 0, stderr[-2000:]
            claimed_sets.append(json.loads(stdout))
        target_claims = sum(str(target) in rows for rows in claimed_sets)
        assert target_claims == 1, f"target event appeared in claim batches {claimed_sets}"
    finally:
        for worker in workers:
            if worker.poll() is None:
                worker.kill()
                worker.wait(timeout=5)
        with admin.begin() as conn:  # type: ignore[attr-defined]
            conn.execute(text("DROP TRIGGER IF EXISTS phase2_pause_inbox_claim ON inbox_events"))
            conn.execute(text("DROP FUNCTION IF EXISTS phase2_pause_inbox_claim()"))


async def test_a_later_message_waits_while_an_older_message_for_that_conversation_runs(
    admin: object,
) -> None:
    """Multiple workers must not process one conversation out of order."""
    from platform_core.db import get_session_factory

    conversation = uuid.uuid4()
    first = _insert(
        admin,
        status="received",
        received_at=100,
        tag="same-conversation-first",
        conversation_ref=conversation,
        event_id=uuid.UUID("0190e000-0000-7000-8000-000000000001"),
    )
    second = _insert(
        admin,
        status="received",
        received_at=100,
        tag="same-conversation-second",
        conversation_ref=conversation,
        event_id=uuid.UUID("0190e000-0000-7000-8000-000000000002"),
    )

    async with get_session_factory()() as session:
        first_claim = await claim_events(session, batch=10)
        await session.commit()
    first_claim_ids = {event.event_id for event in first_claim}
    assert first in first_claim_ids
    assert second not in first_claim_ids
    assert _status_of(admin, first) == "processing"
    assert _status_of(admin, second) == "received"

    async with get_session_factory()() as session:
        blocked_claim = await claim_events(session, batch=10)
        await session.commit()
    assert second not in {event.event_id for event in blocked_claim}, (
        "a later event passed an older live claim"
    )

    async with get_session_factory()() as session:
        await mark_completed(session, first)
        await session.commit()
    async with get_session_factory()() as session:
        second_claim = await claim_events(session, batch=10)
        await session.commit()
    assert second in {event.event_id for event in second_claim}
