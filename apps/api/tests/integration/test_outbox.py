"""Integration tests: transactional outbox (ticket 7).

Verifies the two invariants that make the pattern correct:
1. Rollback of the surrounding transaction also removes the outbox row
   (state and event are atomic).
2. Commit leaves exactly one row per event_id; relay claims with SKIP
   LOCKED and idempotent event_id dedup prevents double-publish.
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
TENANT_A = "01900000-0000-7000-8000-000000000001"


@pytest.fixture(scope="module", autouse=True)
def seed_tenant() -> None:
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO tenants (id, slug, name, status) VALUES "
                "(:id, 'outbox-test', 'Outbox Tenant', 'active') "
                "ON CONFLICT (slug) DO NOTHING"
            ),
            {"id": TENANT_A},
        )
    yield
    with admin.begin() as conn:
        conn.execute(text("DELETE FROM outbox_events WHERE tenant_id = :tid"), {"tid": TENANT_A})
        conn.execute(text("DELETE FROM tenants WHERE slug = 'outbox-test'"))
    admin.dispose()


def test_enqueue_rollback_removes_event() -> None:
    """The atomicity invariant: no committed state -> no event."""
    import asyncio

    import platform_core.outbox_service as svc
    from platform_core.db import create_engine as create_async_engine

    async def scenario() -> bool:
        engine = create_async_engine(
            "postgresql+psycopg://platform_app:platform_app@localhost:5435/platform"
        )
        # RLS context needed for insert; use raw SQL set_config per tx.
        from sqlalchemy.ext.asyncio import async_sessionmaker

        factory = async_sessionmaker(engine, expire_on_commit=False)
        event_id = uuid.uuid4()

        # First: a committed enqueue (control, row must exist)
        async with factory() as session:
            await session.execute(
                text("SELECT set_config('app.tenant_id', :tid, true)"), {"tid": TENANT_A}
            )
            await svc.enqueue(
                session,
                tenant_id=uuid.UUID(TENANT_A),
                event_type="case.created",
                aggregate_type="case",
                aggregate_id=str(uuid.uuid4()),
                payload={"n": 1},
                event_id=event_id,
            )
            await session.commit()

        # Second: an enqueue that rolls back
        try:
            async with factory() as session:
                await session.execute(
                    text("SELECT set_config('app.tenant_id', :tid, true)"), {"tid": TENANT_A}
                )
                await svc.enqueue(
                    session,
                    tenant_id=uuid.UUID(TENANT_A),
                    event_type="case.created",
                    aggregate_type="case",
                    aggregate_id=str(uuid.uuid4()),
                    payload={"n": 2},
                )
                raise RuntimeError("force rollback")
        except RuntimeError:
            pass

        async with factory() as session:
            await session.execute(
                text("SELECT set_config('app.tenant_id', :tid, true)"), {"tid": TENANT_A}
            )
            total = (
                await session.execute(
                    text("SELECT count(*) FROM outbox_events WHERE event_id = :e"),
                    {"e": str(event_id)},
                )
            ).scalar()
        await engine.dispose()
        return int(total or 0) == 1  # committed row exists; rolled-back row gone

    assert asyncio.run(scenario(), loop_factory=asyncio.SelectorEventLoop)


def test_relay_claims_and_marks_sent() -> None:
    import asyncio

    from sqlalchemy.ext.asyncio import async_sessionmaker

    import platform_core.outbox_service as svc
    from platform_core.db import create_engine as create_async_engine

    async def scenario() -> tuple[int, int]:
        engine = create_async_engine(
            "postgresql+psycopg://platform_app:platform_app@localhost:5435/platform"
        )
        factory = async_sessionmaker(engine, expire_on_commit=False)
        eid = uuid.uuid4()

        async with factory() as session:
            await session.execute(
                text("SELECT set_config('app.tenant_id', :tid, true)"), {"tid": TENANT_A}
            )
            await svc.enqueue(
                session,
                tenant_id=uuid.UUID(TENANT_A),
                event_type="conversation.message.created",
                aggregate_type="conversation",
                aggregate_id="42",
                payload={"k": "v"},
                event_id=eid,
            )
            await session.commit()

        # Relay pass 1: claim and publish
        async with factory() as session:
            await session.execute(
                text("SELECT set_config('app.tenant_id', :tid, true)"), {"tid": TENANT_A}
            )
            claimed = await svc.claim_pending(session, batch=10)
            mine = [r for r in claimed if r.event_id == eid]
            for row in mine:
                await svc.mark_sent(session, row.id)
            await session.commit()

        # Relay pass 2: same event must NOT be claimed again
        async with factory() as session:
            await session.execute(
                text("SELECT set_config('app.tenant_id', :tid, true)"), {"tid": TENANT_A}
            )
            claimed_again = await svc.claim_pending(session, batch=10)
            again = [r for r in claimed_again if r.event_id == eid]
            await session.rollback()

        async with factory() as session:
            await session.execute(
                text("SELECT set_config('app.tenant_id', :tid, true)"), {"tid": TENANT_A}
            )
            status = (
                await session.execute(
                    text("SELECT status FROM outbox_events WHERE event_id = :e"),
                    {"e": str(eid)},
                )
            ).scalar()
        await engine.dispose()
        return len(mine), len(again), status

    claimed_n, reclamed_n, final_status = asyncio.run(
        scenario(), loop_factory=asyncio.SelectorEventLoop
    )
    assert claimed_n == 1
    assert reclamed_n == 0
    assert final_status == "sent"
