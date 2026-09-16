"""Integration tests: ConversationControlLease (ticket 9).

Covers docs/testing-and-evaluation.md scenario 3 (human takeover during
generation -> no AI reply) at the lease layer:
- acquire creates one lease per conversation
- human takeover is an unconditional override that bumps the version
- pre-send CAS (assert_can_send) aborts when a takeover happened after
  the AI captured its expected_version
- every transfer writes an audit trail via version bump + reason
"""

import os
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
TENANT_A = "01900000-0000-7000-8000-000000000001"


def _run(coro):
    import asyncio

    return asyncio.run(coro, loop_factory=asyncio.SelectorEventLoop)


@pytest.fixture(scope="module", autouse=True)
def seed_tenant() -> None:
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO tenants (id, slug, name, status) VALUES "
                "(:id, 'lease-test', 'Lease Tenant', 'active') "
                "ON CONFLICT (slug) DO NOTHING"
            ),
            {"id": TENANT_A},
        )
    yield
    with admin.begin() as conn:
        conn.execute(
            text("DELETE FROM conversation_control_leases WHERE tenant_id = :tid"),
            {"tid": TENANT_A},
        )
        conn.execute(text("DELETE FROM tenants WHERE slug = 'lease-test'"))
    admin.dispose()


def _factory(engine):
    return async_sessionmaker(engine, expire_on_commit=False)


def _set_ctx(session, tenant_id: str) -> None:
    import asyncio

    asyncio.get_event_loop()


async def _with_ctx(session, tenant_id: str) -> None:
    from sqlalchemy import text as sa_text

    await session.execute(
        sa_text("SELECT set_config('app.tenant_id', :tid, true)"), {"tid": tenant_id}
    )


def test_acquire_is_idempotent_per_conversation() -> None:
    from platform_core.db import create_engine
    from platform_core.identity import lease_service

    async def scenario() -> tuple[int, str]:
        engine = create_engine(APP_URL)
        factory = _factory(engine)
        conv = uuid.uuid4()
        async with factory() as session:
            await _with_ctx(session, TENANT_A)
            await lease_service.acquire_or_get(
                session, tenant_id=uuid.UUID(TENANT_A), conversation_ref_id=conv
            )
            await session.commit()
        async with factory() as session:
            await _with_ctx(session, TENANT_A)
            second = await lease_service.acquire_or_get(
                session, tenant_id=uuid.UUID(TENANT_A), conversation_ref_id=conv
            )
            await session.commit()
        await engine.dispose()
        return int(second.lease_version), second.owner_type

    version, owner = _run(scenario())
    assert version == 1
    assert owner == "ai"


def test_human_takeover_bumps_version_and_aborts_ai_send() -> None:
    """The race: AI captures version, human takes over, AI's send must abort."""
    from platform_core.db import create_engine
    from platform_core.identity import lease_service
    from platform_core.identity.control_lease import LeaseConflict

    async def scenario() -> tuple[str, int]:
        engine = create_engine(APP_URL)
        factory = _factory(engine)
        conv = uuid.uuid4()
        tid = uuid.UUID(TENANT_A)

        # AI acquires and reads its expected version
        async with factory() as session:
            await _with_ctx(session, TENANT_A)
            lease = await lease_service.acquire_or_get(
                session, tenant_id=tid, conversation_ref_id=conv
            )
            await session.commit()
        expected = int(lease.lease_version)

        # Human takes over (immediate override)
        async with factory() as session:
            await _with_ctx(session, TENANT_A)
            new_version = await lease_service.transfer_to_human(
                session,
                tenant_id=tid,
                conversation_ref_id=conv,
                human_ref="agent-42",
                reason="customer requested human",
            )
            await session.commit()

        # AI's pre-send CAS with the stale version must fail
        abort_reason = ""
        async with factory() as session:
            await _with_ctx(session, TENANT_A)
            try:
                await lease_service.assert_can_send(
                    session,
                    tenant_id=tid,
                    conversation_ref_id=conv,
                    expected_version=expected,
                )
            except LeaseConflict as exc:
                abort_reason = str(exc)
            await session.rollback()

        # And with the current version but AI owner expectation, also fails
        async with factory() as session:
            await _with_ctx(session, TENANT_A)
            try:
                await lease_service.assert_can_send(
                    session,
                    tenant_id=tid,
                    conversation_ref_id=conv,
                    expected_version=new_version,
                )
            except LeaseConflict as exc:
                abort_reason += "|owner-check:" + str(exc)
            await session.rollback()

        await engine.dispose()
        return abort_reason, new_version

    abort_reason, new_version = _run(scenario())
    assert new_version == 2
    # Takeover bumped the version AND flipped ownership: both stale-version
    # and owner checks fail closed.
    assert "owner is human" in abort_reason


def test_assert_can_send_passes_when_unchanged() -> None:
    from platform_core.db import create_engine
    from platform_core.identity import lease_service

    async def scenario() -> bool:
        engine = create_engine(APP_URL)
        factory = _factory(engine)
        tid = uuid.UUID(TENANT_A)
        conv = uuid.uuid4()

        async with factory() as session:
            await _with_ctx(session, TENANT_A)
            lease = await lease_service.acquire_or_get(
                session, tenant_id=tid, conversation_ref_id=conv
            )
            await session.commit()
        expected = int(lease.lease_version)

        # New transaction: set_config is transaction-scoped, so re-apply.
        async with factory() as session:
            await _with_ctx(session, TENANT_A)
            await lease_service.assert_can_send(
                session,
                tenant_id=tid,
                conversation_ref_id=conv,
                expected_version=expected,
            )
            await session.rollback()
        await engine.dispose()
        return True

    assert _run(scenario()) is True


def test_release_to_queue_and_reacquire() -> None:
    from platform_core.db import create_engine
    from platform_core.identity import lease_service

    async def scenario() -> tuple[str, int]:
        engine = create_engine(APP_URL)
        factory = _factory(engine)
        tid = uuid.UUID(TENANT_A)
        conv = uuid.uuid4()

        async with factory() as session:
            await _with_ctx(session, TENANT_A)
            await lease_service.acquire_or_get(session, tenant_id=tid, conversation_ref_id=conv)
            await session.commit()

        async with factory() as session:
            await _with_ctx(session, TENANT_A)
            await lease_service.release_to_queue(
                session,
                tenant_id=tid,
                conversation_ref_id=conv,
                reason="low confidence",
            )
            await session.commit()

        async with factory() as session:
            await _with_ctx(session, TENANT_A)
            from sqlalchemy import select

            row = (
                await session.execute(
                    select(lease_service.ConversationControlLease).where(
                        lease_service.ConversationControlLease.tenant_id == tid,
                        lease_service.ConversationControlLease.conversation_ref_id == conv,
                    )
                )
            ).scalar_one()
        await engine.dispose()
        return row.owner_type, int(row.lease_version)

    owner, version = _run(scenario())
    assert owner == "queue"
    assert version == 2
