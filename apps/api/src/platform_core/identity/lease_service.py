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
from dataclasses import dataclass

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from platform_core.identity.control_lease import (
    ControlLeaseError,
    ConversationControlLease,
    LeaseConflict,
)


@dataclass(frozen=True)
class LeaseSnapshot:
    conversation_ref_id: uuid.UUID
    owner_type: str
    owner_ref: str | None
    mode: str
    lease_version: int
    updated_at: int


def _snapshot(row: ConversationControlLease) -> LeaseSnapshot:
    return LeaseSnapshot(
        conversation_ref_id=row.conversation_ref_id,
        owner_type=row.owner_type,
        owner_ref=row.owner_ref,
        mode=row.mode,
        lease_version=int(row.lease_version),
        updated_at=int(row.updated_at),
    )


async def workbench_leases(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    actor_ref: str,
    tab: str,
    limit: int,
    offset: int,
    matching_refs: set[uuid.UUID] | None = None,
) -> tuple[list[LeaseSnapshot], dict[str, int]]:
    """List actual human handoffs, including conversations with no Case."""
    base = select(ConversationControlLease).where(ConversationControlLease.tenant_id == tenant_id)
    search_clause = (
        ConversationControlLease.conversation_ref_id.in_(matching_refs)
        if matching_refs is not None
        else None
    )
    if search_clause is not None:
        base = base.where(search_clause)
    queued = ConversationControlLease.owner_type == "queue"
    mine = (ConversationControlLease.owner_type == "human") & (
        ConversationControlLease.owner_ref == actor_ref
    )
    waiting = mine & (ConversationControlLease.mode == "HUMAN_WAITING_CUSTOMER")
    conditions = {"queue": queued, "mine": mine, "waiting": waiting}
    if tab not in conditions:
        raise ValueError("unknown workbench tab")
    if matching_refs is None:
        # Separate filtered subqueries allow PostgreSQL to use the partial
        # queue/agent indexes for each exact tab count. The former aggregate
        # FILTER scanned every lease belonging to this tenant per queue poll.
        count_query = select(
            select(func.count())
            .select_from(ConversationControlLease)
            .where(
                ConversationControlLease.tenant_id == tenant_id,
                queued,
            )
            .scalar_subquery()
            .label("queue"),
            select(func.count())
            .select_from(ConversationControlLease)
            .where(
                ConversationControlLease.tenant_id == tenant_id,
                mine,
            )
            .scalar_subquery()
            .label("mine"),
            select(func.count())
            .select_from(ConversationControlLease)
            .where(
                ConversationControlLease.tenant_id == tenant_id,
                waiting,
            )
            .scalar_subquery()
            .label("waiting"),
        )
    else:
        count_query = (
            select(
                func.count().filter(queued).label("queue"),
                func.count().filter(mine).label("mine"),
                func.count().filter(waiting).label("waiting"),
            )
            .select_from(ConversationControlLease)
            .where(
                ConversationControlLease.tenant_id == tenant_id,
                ConversationControlLease.conversation_ref_id.in_(matching_refs),
            )
        )
    count_row = (await session.execute(count_query)).one()
    counts = {name: int(count_row._mapping[name]) for name in conditions}
    rows = (
        (
            await session.execute(
                base.where(conditions[tab])
                .order_by(
                    ConversationControlLease.updated_at.desc(), ConversationControlLease.id.desc()
                )
                .limit(limit)
                .offset(offset)
            )
        )
        .scalars()
        .all()
    )
    return [_snapshot(row) for row in rows], counts


async def lease_snapshot(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    conversation_ref_id: uuid.UUID,
    for_update: bool = False,
) -> LeaseSnapshot | None:
    stmt = select(ConversationControlLease).where(
        ConversationControlLease.tenant_id == tenant_id,
        ConversationControlLease.conversation_ref_id == conversation_ref_id,
    )
    if for_update:
        stmt = stmt.with_for_update()
    row = (await session.execute(stmt)).scalar_one_or_none()
    return _snapshot(row) if row is not None else None


async def locked_owner(
    session: AsyncSession, *, tenant_id: uuid.UUID, conversation_ref_id: uuid.UUID
) -> tuple[str, str]:
    """Read ownership while holding the row through a customer message write."""
    row = (
        await session.execute(
            select(ConversationControlLease)
            .where(
                ConversationControlLease.tenant_id == tenant_id,
                ConversationControlLease.conversation_ref_id == conversation_ref_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if row is None:
        return "ai", "AI_ACTIVE"
    if row.expires_at is not None and row.expires_at <= int(time.time()):
        return "expired", "LEASE_EXPIRED"
    return row.owner_type, row.mode


async def workbench_transition(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    conversation_ref_id: uuid.UUID,
    actor_ref: str,
    expected_version: int,
    operation: str,
    target_ref: str | None = None,
) -> LeaseSnapshot:
    """CAS for claim, release, transfer and resolution.

    Human-to-human transfer is deliberate; taking a colleague's active thread
    through the normal Claim button is refused. All changes are transactionally
    serialized on the lease row and attributed to the authenticated actor.
    """
    row = (
        await session.execute(
            select(ConversationControlLease)
            .where(
                ConversationControlLease.tenant_id == tenant_id,
                ConversationControlLease.conversation_ref_id == conversation_ref_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if row is None:
        raise LeaseConflict("conversation is not in the workbench")
    if row.expires_at is not None and row.expires_at <= int(time.time()):
        raise LeaseConflict("conversation lease has expired")
    if int(row.lease_version) != expected_version:
        raise LeaseConflict("conversation ownership changed; refresh and retry")
    if operation == "claim":
        if row.owner_type not in ("queue", "ai"):
            raise LeaseConflict("conversation is already owned")
        row.owner_type, row.owner_ref, row.mode = "human", actor_ref, "HUMAN_ACTIVE"
    elif operation in ("release", "transfer", "close"):
        if row.owner_type != "human" or row.owner_ref != actor_ref:
            raise LeaseConflict("only the current agent may change this conversation")
        if operation == "release":
            row.owner_type, row.owner_ref, row.mode = "queue", None, "QUEUED_FOR_HUMAN"
        elif operation == "transfer":
            if not target_ref or target_ref == actor_ref:
                raise LeaseConflict("select another active agent")
            row.owner_type, row.owner_ref, row.mode = "human", target_ref, "HUMAN_ACTIVE"
        else:
            row.owner_type, row.owner_ref, row.mode = "closed", actor_ref, "RESOLVED"
    else:
        raise ValueError("unknown workbench operation")
    row.lease_version += 1
    row.changed_reason = f"workbench:{operation}"
    row.updated_at = int(time.time())
    await session.flush()
    return _snapshot(row)


async def mark_waiting_for_customer(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    conversation_ref_id: uuid.UUID,
    actor_ref: str,
) -> None:
    await session.execute(
        update(ConversationControlLease)
        .where(
            ConversationControlLease.tenant_id == tenant_id,
            ConversationControlLease.conversation_ref_id == conversation_ref_id,
            ConversationControlLease.owner_type == "human",
            ConversationControlLease.owner_ref == actor_ref,
        )
        .values(mode="HUMAN_WAITING_CUSTOMER", updated_at=int(time.time()))
    )


async def mark_customer_replied(
    session: AsyncSession, *, tenant_id: uuid.UUID, conversation_ref_id: uuid.UUID
) -> None:
    await session.execute(
        update(ConversationControlLease)
        .where(
            ConversationControlLease.tenant_id == tenant_id,
            ConversationControlLease.conversation_ref_id == conversation_ref_id,
            ConversationControlLease.owner_type == "human",
        )
        .values(mode="HUMAN_ACTIVE", updated_at=int(time.time()))
    )


async def recently_closed_refs(
    session: AsyncSession, *, tenant_id: uuid.UUID, since: int
) -> set[uuid.UUID]:
    rows = (
        (
            await session.execute(
                select(ConversationControlLease.conversation_ref_id).where(
                    ConversationControlLease.tenant_id == tenant_id,
                    ConversationControlLease.owner_type == "closed",
                    ConversationControlLease.updated_at >= since,
                )
            )
        )
        .scalars()
        .all()
    )
    return set(rows)


async def active_human_workload(
    session: AsyncSession, *, tenant_id: uuid.UUID, actor_ref: str
) -> int:
    return int(
        (
            await session.execute(
                select(func.count())
                .select_from(ConversationControlLease)
                .where(
                    ConversationControlLease.tenant_id == tenant_id,
                    ConversationControlLease.owner_type == "human",
                    ConversationControlLease.owner_ref == actor_ref,
                )
            )
        ).scalar_one()
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
