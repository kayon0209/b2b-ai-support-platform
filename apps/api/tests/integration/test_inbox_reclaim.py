"""Integration tests: stuck-claim recovery for the inbox consumer.

A worker that claims a row (status PROCESSING) and then dies leaves it
PROCESSING forever — nothing else transitions it. That silently drops the
customer's question, which is the exact failure the durable-inbox design
exists to prevent.

These tests drive the real database: claiming is a row-status transition
inside a transaction, so an in-memory fake would not exercise the thing
that actually breaks.
"""

import os
import time
import uuid

import pytest
from sqlalchemy import create_engine, text

from worker.inbox_consumer import STALE_PROCESSING_SECONDS, reclaim_stale_processing

pytestmark = pytest.mark.integration

ADMIN_URL = os.environ.get(
    "APP_ADMIN_DATABASE_URL",
    "postgresql+psycopg://platform:platform@localhost:5435/platform",
)
TENANT = uuid.UUID("01900000-0000-7000-8000-00000000dead")


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


def _insert(admin: object, *, status: str, received_at: int, tag: str) -> uuid.UUID:
    event_id = uuid.uuid4()
    with admin.begin() as conn:  # type: ignore[attr-defined]
        conn.execute(
            text(
                "INSERT INTO inbox_events "
                "(id, tenant_id, delivery_id, event_type, payload_hash, "
                " minimized_payload, status, received_at) VALUES "
                "(:id, :tid, :did, 'message_created', 'h', '{}'::jsonb, :st, :ra)"
            ),
            {
                "id": str(event_id),
                "tid": str(TENANT),
                "did": f"reclaim-{tag}-{event_id}",
                "st": status,
                "ra": received_at,
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
