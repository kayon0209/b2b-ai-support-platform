"""Integration: feature list 1.7 - the queue position is counted, not guessed.

`queue_status` does a real count against real rows, so a fake session would
prove nothing: the whole claim is "there are N conversations ahead of this
one, and N comes from the leases, not from an estimate".

Also pinned: a conversation whose lease is NOT owned by the queue is not
queued. Reporting "position 1" for every conversation would make the feature
look alive while telling every customer the same lie.
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import async_sessionmaker

pytestmark = pytest.mark.integration

ADMIN_URL = os.environ.get(
    "APP_ADMIN_DATABASE_URL",
    "postgresql+psycopg://platform:platform@localhost:5435/platform",
)
APP_URL = "postgresql+psycopg://platform_app:platform_app@localhost:5435/platform"

TENANT = "01900000-0000-7000-8000-0000000000d9"
SLUG = "agent-queue-status"


def _run(coro):
    return asyncio.run(coro, loop_factory=asyncio.SelectorEventLoop)


def _seed_tenant() -> None:
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO tenants (id, slug, name, status) VALUES "
                "(:id, :slug, 'Queue Status', 'active') ON CONFLICT (slug) DO NOTHING"
            ),
            {"id": TENANT, "slug": SLUG},
        )
    admin.dispose()


def _clear() -> None:
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        conn.execute(
            text("DELETE FROM conversation_control_leases WHERE tenant_id = :t"), {"t": TENANT}
        )
        conn.execute(text("DELETE FROM tenants WHERE slug = :slug"), {"slug": SLUG})
    admin.dispose()


def _insert_lease(conversation_id: str, owner_type: str, updated_at: int) -> None:
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO conversation_control_leases (id, tenant_id, "
                "conversation_ref_id, owner_type, mode, lease_version, "
                "changed_reason, updated_at) VALUES "
                "(:id, :t, :conv, :owner, 'AI_ACTIVE', 1, 'created', :updated)"
            ),
            {
                "id": str(uuid.uuid4()),
                "t": TENANT,
                "conv": conversation_id,
                "owner": owner_type,
                "updated": updated_at,
            },
        )
    admin.dispose()


async def _status(conversation_id: str):
    from sqlalchemy import text as sa_text

    from platform_core.agent_runtime.queue_status import queue_status
    from platform_core.db import create_engine as async_engine

    engine = async_engine(APP_URL)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        await session.execute(
            sa_text("SELECT set_config('app.tenant_id', :t, true)"), {"t": TENANT}
        )
        return await queue_status(
            session,
            tenant_id=uuid.UUID(TENANT),
            conversation_ref_id=uuid.UUID(conversation_id),
        )


@pytest.fixture(autouse=True)
def tenant() -> None:
    _clear()
    _seed_tenant()
    yield
    _clear()


def test_position_counts_the_leases_that_entered_earlier() -> None:
    now = int(time.time())
    mine = str(uuid.uuid4())
    _insert_lease(str(uuid.uuid4()), "queue", now - 300)  # ahead
    _insert_lease(mine, "queue", now - 100)
    _insert_lease(str(uuid.uuid4()), "queue", now - 10)  # behind

    status = _run(_status(mine))
    assert status.queued is True
    assert status.ahead == 1
    assert status.position == 2


def test_first_in_line_reports_nobody_ahead() -> None:
    now = int(time.time())
    mine = str(uuid.uuid4())
    _insert_lease(mine, "queue", now - 100)
    _insert_lease(str(uuid.uuid4()), "queue", now - 10)

    status = _run(_status(mine))
    assert status.position == 1
    assert status.ahead == 0


def test_a_conversation_not_owned_by_the_queue_is_not_queued() -> None:
    """AI still holds it - telling that customer they are queued is a lie."""
    now = int(time.time())
    mine = str(uuid.uuid4())
    _insert_lease(mine, "ai", now - 100)
    _insert_lease(str(uuid.uuid4()), "queue", now - 300)

    status = _run(_status(mine))
    assert status.queued is False
    assert status.position == 0


def test_a_conversation_with_no_lease_at_all_is_not_queued() -> None:
    status = _run(_status(str(uuid.uuid4())))
    assert status.queued is False


def test_no_wait_estimate_without_a_declared_average(monkeypatch) -> None:
    """1.7 gives a count it can defend and no time it cannot."""
    monkeypatch.delenv("APP_QUEUE_AVG_HANDLE_MINUTES", raising=False)
    now = int(time.time())
    mine = str(uuid.uuid4())
    _insert_lease(str(uuid.uuid4()), "queue", now - 300)
    _insert_lease(mine, "queue", now - 100)

    status = _run(_status(mine))
    assert status.ahead == 1
    assert status.estimated_wait_minutes is None
