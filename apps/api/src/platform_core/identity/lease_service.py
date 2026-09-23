"""Lease service: acquire, transfer, and pre-send CAS check (ticket 9).

The pre-send check (assert_can_send) is the safety gate: it re-reads the
lease inside the same transaction that issues the outbound command and
refuses unless owner is still AI and version still equals the caller's
expected value. Used together with the outbound idempotency key, this
gives the human/AI race-condition guarantee required by
docs/testing-and-evaluation.md scenario 3.
"""

import time
import uuid

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from platform_core.identity.control_lease import (
    ControlLeaseError,
    ConversationControlLease,
    LeaseConflict,
)


async def acquire_or_get(
    session: AsyncSession, *, tenant_id: uuid.UUID, conversation_ref_id: uuid.UUID
) -> ConversationControlLease:
    """Idempotent acquire: insert-on-conflict, then read the row back.

    Reads happen inside the caller's current transaction, so the caller is
    responsible for having set app.tenant_id (RLS) in THIS transaction.
    A transaction-scoped set_config does not survive COMMIT.
    """
    stmt = (
        pg_insert(ConversationControlLease)
        .values(
            tenant_id=tenant_id,
            conversation_ref_id=conversation_ref_id,
            owner_type="ai",
            mode="AI_ACTIVE",
            lease_version=1,
            changed_reason="created",
            updated_at=int(time.time()),
        )
        .on_conflict_do_nothing(constraint="uq_lease_per_conversation")
        .returning(ConversationControlLease.id)
    )
    await session.execute(stmt)
    result = await session.execute(
        select(ConversationControlLease).where(
            ConversationControlLease.tenant_id == tenant_id,
            ConversationControlLease.conversation_ref_id == conversation_ref_id,
        )
    )
    return result.scalar_one()


async def transfer_to_human(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    conversation_ref_id: uuid.UUID,
    human_ref: str,
    reason: str,
) -> int:
    """Immediate human takeover. Unconditional override, bumps version.

    Returns the new lease_version so callers can track it.
    """
    now = int(time.time())
    stmt = (
        update(ConversationControlLease)
        .where(
            ConversationControlLease.tenant_id == tenant_id,
            ConversationControlLease.conversation_ref_id == conversation_ref_id,
        )
        .values(
            owner_type="human",
            owner_ref=human_ref,
            mode="HUMAN_ACTIVE",
            lease_version=ConversationControlLease.lease_version + 1,
            changed_reason=reason[:255],
            updated_at=now,
        )
        .returning(ConversationControlLease.lease_version)
    )
    result = (await session.execute(stmt)).scalar_one_or_none()
    if result is None:
        raise ControlLeaseError("lease row missing; acquire first")
    return int(result)


async def current_owner(
    session: AsyncSession, *, tenant_id: uuid.UUID, conversation_ref_id: uuid.UUID
) -> tuple[str, str]:
    """Who owns the conversation now: ``(owner_type, mode)``.

    Read-only, and deliberately the *same* question `assert_can_send` answers,
    asked earlier. A caller that is about to spend a model call on a
    conversation can find out first whether the answer would be allowed to
    reach anybody.

    Before this existed, the only way to discover that the owner was the queue
    was to generate a reply and watch the pre-send gate refuse it: the model
    was paid for, the draft was thrown away, and the customer was told nothing
    at all. Measured 2026-09-23 - a customer whose first question was handed off
    got silence for every question after it, indefinitely.

    A conversation with no lease row reads as owned by the AI: nobody has
    claimed it and `acquire_or_get` will create it as ``ai`` when the run
    starts. Reporting that as "unknown" would make every new conversation
    unanswerable.
    """
    row = (
        await session.execute(
            select(
                ConversationControlLease.owner_type,
                ConversationControlLease.mode,
                ConversationControlLease.expires_at,
            ).where(
                ConversationControlLease.tenant_id == tenant_id,
                ConversationControlLease.conversation_ref_id == conversation_ref_id,
            )
        )
    ).one_or_none()
    if row is None:
        return "ai", "AI_ACTIVE"
    owner_type, mode, expires_at = row
    if expires_at is not None and int(expires_at) <= int(time.time()):
        # Same reading as `assert_can_send`: an expired lease is not the AI's.
        return "expired", "LEASE_EXPIRED"
    return str(owner_type), str(mode)


async def assert_can_send(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    conversation_ref_id: uuid.UUID,
    expected_version: int,
) -> None:
    """Pre-send CAS gate. Raises LeaseConflict if the lease changed.

    Uses a fresh SELECT ... FOR SHARE inside the caller's transaction; the
    outbound worker must run this in the same transaction that records the
    send, so no interleaved takeover can slip between check and dispatch.
    """
    stmt = (
        select(
            ConversationControlLease.owner_type,
            ConversationControlLease.lease_version,
            ConversationControlLease.expires_at,
        )
        .where(
            ConversationControlLease.tenant_id == tenant_id,
            ConversationControlLease.conversation_ref_id == conversation_ref_id,
        )
        .with_for_update()
    )
    row = (await session.execute(stmt)).one_or_none()
    if row is None:
        raise LeaseConflict("no lease row")
    owner_type, version, expires_at = row
    if expires_at is not None and expires_at < int(time.time()):
        raise LeaseConflict("lease expired")
    if owner_type != "ai":
        raise LeaseConflict(f"owner is {owner_type}")
    if int(version) != int(expected_version):
        raise LeaseConflict(f"version moved {expected_version} -> {int(version)}")


async def release_to_queue(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    conversation_ref_id: uuid.UUID,
    reason: str,
) -> int:
    now = int(time.time())
    stmt = (
        update(ConversationControlLease)
        .where(
            ConversationControlLease.tenant_id == tenant_id,
            ConversationControlLease.conversation_ref_id == conversation_ref_id,
        )
        .values(
            owner_type="queue",
            owner_ref=None,
            mode="QUEUED_FOR_HUMAN",
            lease_version=ConversationControlLease.lease_version + 1,
            changed_reason=reason[:255],
            updated_at=now,
        )
        .returning(ConversationControlLease.lease_version)
    )
    result = (await session.execute(stmt)).scalar_one_or_none()
    if result is None:
        raise ControlLeaseError("lease row missing; acquire first")
    return int(result)
