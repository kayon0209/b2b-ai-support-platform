"""Taking exclusive right to finish a run.

The problem
-----------
A run used to be finalized by mutating its ORM object and flushing. That is a
blind write. Two workers on the same run - a lease expiry, a retry, a queue
delivered twice - both reach a terminal state and the second silently overwrites
the first. The row looks healthy afterwards: one answer's `output_hash`, one
`status`, and no indication that another answer ever existed.

The ordering is the part that makes it a customer-visible bug rather than a
statistical curiosity. The reply is dispatched to the channel *before* the
terminal write, so guarding the write would detect the conflict only after both
workers had already sent. The claim has to be taken before the send.

What the claim is
-----------------
Not the terminal state - that is not known until the dispatch has happened. It
is the exclusive right to *decide* it, taken by an atomic
`UPDATE ... WHERE status IN (...)` that also bumps `version`. Exactly one
concurrent caller can match the predicate, so exactly one proceeds to send.

Why `version` and not just the status predicate
-----------------------------------------------
The predicate alone stops the overwrite. The counter is what leaves a mark: a
run whose version moved without a terminal status change is a run a worker
claimed and then did not finish, which is the signature of a crash between the
two and is otherwise indistinguishable from a run nobody ever touched.

Relationship to the retention sweep
-----------------------------------
`abandoned.py` also writes a terminal status, and it can fire on a run a worker
is still executing. Both are guarded by the same rule: whichever lands second
observes that the status is no longer one it expected, and loses. That test
runs in both orders, because "the sweep cannot overwrite a claimed run" is a
different claim from "a worker cannot claim an abandoned run".
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from platform_core.agent_runtime.models import AgentRun


async def claim_terminal(
    session: AsyncSession,
    *,
    run_id: uuid.UUID,
    expected_status: str,
    expected_version: int,
) -> int | None:
    """Take exclusive right to finish `run_id`, or return None if taken.

    `expected_version` is the version the caller observed when it loaded the
    run, and it is the clause that makes this a compare-and-set rather than an
    increment.

    That detail was the first version's bug and it is worth stating, because it
    looks correct: a statement that checks only the status and bumps `version`
    grants the claim to *every* caller. The status does not change, so the next
    worker's predicate matches the same row again - measured here as two winners
    claiming one run. Consuming a claim requires testing the thing the claim
    moved. Hence `WHERE version = :expected_version`, which the first successful
    claim has already invalidated.

    `expected_status` is the single status this caller still considers workable.
    A set was tempting and is wrong: two statuses means two workers each
    believing the run is theirs.

    Returns the new `version` on success and `None` on failure, so the loser has
    something to log. There is no partial success, and a caller that ignores
    the `None` is the bug this function exists to make impossible to hide.
    """
    from sqlalchemy import update

    result = await session.execute(
        update(AgentRun)
        .where(
            AgentRun.id == run_id,
            AgentRun.status == expected_status,
            AgentRun.version == expected_version,
        )
        .values(version=AgentRun.version + 1)
        .returning(AgentRun.version)
    )
    # With an ORM construct + RETURNING, `first()` is not a coroutine - the
    # opposite of the raw-SQL path in `visitor_revocation`, where it is. Both
    # shapes exist in this codebase; mypy is the authority for which is which.
    row = result.first()
    return int(row[0]) if row is not None else None


async def is_claimed_since(
    session: AsyncSession,
    *,
    run_id: uuid.UUID,
    at_least: int,
) -> bool:
    """Whether a run has been claimed at least `at_least` times.

    For operators asking "did a worker ever take this run on, and then stop?"
    - the version moved but the status never became terminal, which is what a
    crash between claim and finalize looks like from outside.
    """
    from sqlalchemy import select

    version = await session.scalar(select(AgentRun.version).where(AgentRun.id == run_id).limit(1))
    return version is not None and int(version) >= at_least
