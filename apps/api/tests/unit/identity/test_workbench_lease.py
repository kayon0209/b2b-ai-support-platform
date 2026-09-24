"""Conversation-first inbox ownership invariants without a running database."""

import uuid

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from platform_core.identity.control_lease import ConversationControlLease, LeaseConflict
from platform_core.identity.lease_service import (
    mark_customer_replied,
    mark_waiting_for_customer,
    workbench_leases,
    workbench_transition,
)


@pytest.mark.asyncio
async def test_claim_transfer_wait_and_close_require_current_version_and_owner() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(ConversationControlLease.__table__.create)

    tenant = uuid.uuid4()
    ref = uuid.uuid4()
    alice = str(uuid.uuid4())
    bob = str(uuid.uuid4())
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            session.add(
                ConversationControlLease(
                    tenant_id=tenant,
                    conversation_ref_id=ref,
                    owner_type="queue",
                    owner_ref=None,
                    mode="QUEUED_FOR_HUMAN",
                    lease_version=1,
                    updated_at=1,
                )
            )
            await session.flush()
            claimed = await workbench_transition(
                session,
                tenant_id=tenant,
                conversation_ref_id=ref,
                actor_ref=alice,
                expected_version=1,
                operation="claim",
            )
            assert (claimed.owner_type, claimed.owner_ref, claimed.lease_version) == (
                "human",
                alice,
                2,
            )
            with pytest.raises(LeaseConflict, match="ownership changed"):
                await workbench_transition(
                    session,
                    tenant_id=tenant,
                    conversation_ref_id=ref,
                    actor_ref=bob,
                    expected_version=1,
                    operation="claim",
                )
            with pytest.raises(LeaseConflict, match="current agent"):
                await workbench_transition(
                    session,
                    tenant_id=tenant,
                    conversation_ref_id=ref,
                    actor_ref=bob,
                    expected_version=2,
                    operation="close",
                )
            await mark_waiting_for_customer(
                session, tenant_id=tenant, conversation_ref_id=ref, actor_ref=alice
            )
            await mark_customer_replied(session, tenant_id=tenant, conversation_ref_id=ref)
            transferred = await workbench_transition(
                session,
                tenant_id=tenant,
                conversation_ref_id=ref,
                actor_ref=alice,
                expected_version=2,
                operation="transfer",
                target_ref=bob,
            )
            assert (transferred.owner_ref, transferred.lease_version) == (bob, 3)
            closed = await workbench_transition(
                session,
                tenant_id=tenant,
                conversation_ref_id=ref,
                actor_ref=bob,
                expected_version=3,
                operation="close",
            )
            assert (closed.owner_type, closed.mode, closed.lease_version) == (
                "closed",
                "RESOLVED",
                4,
            )
            with pytest.raises(LeaseConflict, match="current agent"):
                await workbench_transition(
                    session,
                    tenant_id=tenant,
                    conversation_ref_id=ref,
                    actor_ref=bob,
                    expected_version=4,
                    operation="release",
                )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_queue_filters_by_server_tenant_even_with_foreign_ref_search() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(ConversationControlLease.__table__.create)
    tenant_a, tenant_b = uuid.uuid4(), uuid.uuid4()
    ref_a, ref_b, ref_mine, ref_waiting = (uuid.uuid4() for _ in range(4))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            for tenant, ref, owner, owner_ref, mode in (
                (tenant_a, ref_a, "queue", None, "QUEUED_FOR_HUMAN"),
                (tenant_b, ref_b, "queue", None, "QUEUED_FOR_HUMAN"),
                (tenant_a, ref_mine, "human", "agent", "HUMAN_ACTIVE"),
                (tenant_a, ref_waiting, "human", "agent", "HUMAN_WAITING_CUSTOMER"),
            ):
                session.add(
                    ConversationControlLease(
                        tenant_id=tenant,
                        conversation_ref_id=ref,
                        owner_type=owner,
                        owner_ref=owner_ref,
                        mode=mode,
                        lease_version=1,
                        updated_at=1,
                    )
                )
            await session.flush()
            rows, counts = await workbench_leases(
                session, tenant_id=tenant_a, actor_ref="agent", tab="queue", limit=50, offset=0
            )
            assert [row.conversation_ref_id for row in rows] == [ref_a]
            assert counts == {"queue": 1, "mine": 2, "waiting": 1}
            mine, counts = await workbench_leases(
                session, tenant_id=tenant_a, actor_ref="agent", tab="mine", limit=50, offset=0
            )
            assert {row.conversation_ref_id for row in mine} == {ref_mine, ref_waiting}
            assert counts == {"queue": 1, "mine": 2, "waiting": 1}
            waiting, counts = await workbench_leases(
                session,
                tenant_id=tenant_a,
                actor_ref="agent",
                tab="waiting",
                limit=50,
                offset=0,
            )
            assert [row.conversation_ref_id for row in waiting] == [ref_waiting]
            assert counts == {"queue": 1, "mine": 2, "waiting": 1}
            foreign, counts = await workbench_leases(
                session,
                tenant_id=tenant_a,
                actor_ref="agent",
                tab="queue",
                limit=50,
                offset=0,
                matching_refs={ref_b},
            )
            assert foreign == []
            assert counts == {"queue": 0, "mine": 0, "waiting": 0}
    finally:
        await engine.dispose()
