"""Assignment: which agent should take a case, and what taking it means.

Three operations, and each is guarded because each has a way to go quietly
wrong:

- **`pick_agent`** - skills first, then least-loaded, then capacity. It returns
  `None` when everyone is full rather than overloading the least-bad agent:
  silently exceeding a stated capacity is how a directory stops meaning
  anything, and an unassigned case is visible while an overloaded agent is not.
- **`claim_case`** - a volunteer. Refused if the agent is at capacity, so the
  ceiling is a ceiling and not a suggestion.
- **`release_case`** - back to the queue. This must exist: without it an agent
  who goes on leave takes their cases with them.

All three read case load in **one grouped query**, not one query per agent. An
N-query implementation would be correct and would quietly become the slowest
thing an operator does, because the number of agents is exactly what grows.

What this module does not do: assign automatically on a timer. Picking is
separate from scheduling on purpose - a scheduler belongs in a worker (it has
retries, backoff and idempotency already), and putting it here would make one
HTTP request responsible for a decision that should survive a crash.
"""

from __future__ import annotations

import time
import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from platform_core.cases.agent_models import (
    MAX_CONCURRENT_CEILING,
    MAX_NAME,
    MAX_REF,
    AgentProfile,
    AgentStatus,
)
from platform_core.cases.models import Case, CaseStatus

# Statuses that still occupy an agent. `RESOLVED` and `CLOSED` do not, and
# `REOPENED` does - a reopened case is work again, and counting it as closed
# would let an agent's real load stay invisible.
OPEN_STATUSES: frozenset[str] = frozenset(
    {
        CaseStatus.NEW.value,
        CaseStatus.TRIAGED.value,
        CaseStatus.IN_PROGRESS.value,
        CaseStatus.WAITING_CUSTOMER.value,
        CaseStatus.WAITING_INTERNAL.value,
        CaseStatus.WAITING_VENDOR.value,
        CaseStatus.REOPENED.value,
    }
)


class AssignmentError(ValueError):
    """A refused claim or release. Mapped to 409 by the router."""


def _clean(value: str, max_len: int) -> str:
    return (value or "").strip()[:max_len]


async def list_agents(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    status: str | None = AgentStatus.ACTIVE.value,
) -> list[AgentProfile]:
    stmt = select(AgentProfile).where(AgentProfile.tenant_id == tenant_id)
    if status:
        stmt = stmt.where(AgentProfile.status == status)
    return list(
        (await session.execute(stmt.order_by(AgentProfile.display_name, AgentProfile.user_ref)))
        .scalars()
        .all()
    )


async def upsert_agent(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    user_ref: str,
    display_name: str,
    skills: list[str] | None = None,
    max_concurrent: int = 5,
    status: str = AgentStatus.ACTIVE.value,
) -> AgentProfile:
    """Create or update an agent. The caller owns the transaction."""
    ref = _clean(user_ref, MAX_REF)
    name = _clean(display_name, MAX_NAME)
    if not ref:
        raise AssignmentError("an agent needs a user_ref")
    if not name:
        raise AssignmentError("an agent needs a display_name")
    if max_concurrent < 1:
        raise AssignmentError("max_concurrent must be at least 1")

    row = (
        await session.execute(
            select(AgentProfile).where(
                AgentProfile.tenant_id == tenant_id, AgentProfile.user_ref == ref
            )
        )
    ).scalar_one_or_none()
    now = int(time.time())
    if row is None:
        row = AgentProfile(
            tenant_id=tenant_id,
            user_ref=ref,
            display_name=name,
            created_at=now,
            updated_at=now,
        )
        session.add(row)
    row.display_name = name
    row.skills = [str(s).strip() for s in (skills or []) if str(s).strip()]
    # Clamped, not rejected: a ceiling of 5000 is a typo, not a policy, and the
    # difference matters only because unclamped it makes "least loaded" a
    # constant - one agent would absorb the whole queue.
    row.max_concurrent = min(int(max_concurrent), MAX_CONCURRENT_CEILING)
    row.status = status
    row.updated_at = now
    await session.flush()
    return row


async def update_agent(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    user_ref: str,
    display_name: str | None = None,
    skills: list[str] | None = None,
    max_concurrent: int | None = None,
    status: str | None = None,
) -> AgentProfile:
    """Change only the fields supplied.

    Separate from `upsert_agent` because an upsert that fills unspecified fields
    with defaults is a trap on a *partial* update: changing an agent's status
    would silently reset their skills to empty and their capacity to 5, which is
    the kind of edit that takes a week to notice.
    """
    row = (
        await session.execute(
            select(AgentProfile).where(
                AgentProfile.tenant_id == tenant_id,
                AgentProfile.user_ref == _clean(user_ref, MAX_REF),
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise AssignmentError("no such agent")

    if display_name is not None:
        name = _clean(display_name, MAX_NAME)
        if not name:
            raise AssignmentError("an agent needs a display_name")
        row.display_name = name
    if skills is not None:
        row.skills = [str(s).strip() for s in skills if str(s).strip()]
    if max_concurrent is not None:
        if max_concurrent < 1:
            raise AssignmentError("max_concurrent must be at least 1")
        row.max_concurrent = min(int(max_concurrent), MAX_CONCURRENT_CEILING)
    if status is not None:
        row.status = status

    row.updated_at = int(time.time())
    await session.flush()
    return row


async def set_agent_status(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    user_ref: str,
    status: str,
) -> AgentProfile:
    row = (
        await session.execute(
            select(AgentProfile).where(
                AgentProfile.tenant_id == tenant_id,
                AgentProfile.user_ref == _clean(user_ref, MAX_REF),
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise AssignmentError("no such agent")
    row.status = status
    row.updated_at = int(time.time())
    await session.flush()
    return row


async def load_map(session: AsyncSession, *, tenant_id: uuid.UUID) -> dict[str, int]:
    """Open cases per assignee, in one grouped query.

    Counts every open case with an assignee, including ones assigned to a ref
    with no directory entry. An orphaned assignment is a real state (someone
    typed a name by hand) and hiding it would under-report that agent's load.
    """
    rows = (
        await session.execute(
            select(Case.assignee_ref, func.count())
            .where(
                Case.tenant_id == tenant_id,
                Case.status.in_(sorted(OPEN_STATUSES)),
                Case.assignee_ref.is_not(None),
            )
            .group_by(Case.assignee_ref)
        )
    ).all()
    return {str(ref): int(count) for ref, count in rows if ref}


def _wanted(*, business_line: str, team_ref: str) -> frozenset[str]:
    return frozenset(value for value in (business_line, team_ref) if value)


def _eligible(agents: list[AgentProfile], *, wanted: frozenset[str]) -> list[AgentProfile]:
    """Who may take this, **skilled first**.

    The ordering here is a correction, not a detail. An agent with no skill tags
    matches everything, so a naive "collect everyone who matches" pool puts the
    unskilled alongside the skilled and then breaks the tie alphabetically -
    which hands a PCB question to a generalist while a PCB specialist sits
    idle. Skill match has to be a *separating* key, and the unskilled are a
    fallback consulted only when nobody is tagged for the work.
    """
    if not wanted:
        # Nothing was asked for: everyone is equally eligible.
        return agents
    skilled = [a for a in agents if wanted & set(a.skills or [])]
    if skilled:
        return skilled
    return [a for a in agents if not (a.skills or [])]


async def pick_agent(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    business_line: str = "",
    team_ref: str = "",
) -> AgentProfile | None:
    """The agent who should take this next, or None when nobody can.

    Ranked by **load ratio** rather than raw load: an agent with capacity 10
    carrying 3 is less busy than one with capacity 4 carrying 3, and comparing
    raw counts would keep handing work to whoever is personally slowest.
    """
    agents = await list_agents(session, tenant_id=tenant_id, status=AgentStatus.ACTIVE.value)
    if not agents:
        return None
    loads = await load_map(session, tenant_id=tenant_id)
    eligible = _eligible(agents, wanted=_wanted(business_line=business_line, team_ref=team_ref))
    if not eligible:
        # Nobody tagged for it and nobody general: leave it queued. An
        # unassigned case is visible; a mis-assigned one is not.
        return None

    ranked = sorted(
        eligible,
        key=lambda a: (
            loads.get(a.user_ref, 0) / max(int(a.max_concurrent), 1),
            -int(a.max_concurrent),
            a.user_ref,
        ),
    )
    for agent in ranked:
        if loads.get(agent.user_ref, 0) < int(agent.max_concurrent):
            return agent
    # Everyone is at capacity. None, not the least-bad - see the module
    # docstring.
    return None


async def queue_cases(
    session: AsyncSession, *, tenant_id: uuid.UUID, limit: int = 50
) -> list[Case]:
    """Unassigned open cases, oldest first.

    Unassigned means *no assignee*, not "assigned to nobody in particular" -
    an empty string is treated as assigned-to-nobody-on-purpose elsewhere in
    this codebase, so it is excluded here too and old work cannot hide behind
    a blank.
    """
    return list(
        (
            await session.execute(
                select(Case)
                .where(
                    Case.tenant_id == tenant_id,
                    Case.status.in_(sorted(OPEN_STATUSES)),
                    Case.assignee_ref.is_(None),
                )
                .order_by(Case.opened_at, Case.id)
                .limit(max(1, min(limit, 200)))
            )
        )
        .scalars()
        .all()
    )


async def claim_case(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    case_id: uuid.UUID,
    user_ref: str,
) -> Case:
    """Assign a case to an agent who asked for it.

    Refused when the agent is at capacity. The ceiling is checked here and not
    only in `pick_agent` because a volunteer is exactly the path that would
    otherwise walk straight past it.
    """
    ref = _clean(user_ref, MAX_REF)
    case = (
        await session.execute(select(Case).where(Case.tenant_id == tenant_id, Case.id == case_id))
    ).scalar_one_or_none()
    if case is None:
        raise AssignmentError("no such case")

    agent = (
        await session.execute(
            select(AgentProfile).where(
                AgentProfile.tenant_id == tenant_id, AgentProfile.user_ref == ref
            )
        )
    ).scalar_one_or_none()
    if agent is None:
        raise AssignmentError("no such agent")
    if agent.status != AgentStatus.ACTIVE.value:
        raise AssignmentError("that agent is not active")

    loads = await load_map(session, tenant_id=tenant_id)
    if loads.get(ref, 0) >= int(agent.max_concurrent):
        raise AssignmentError(f"{ref} is at capacity ({int(agent.max_concurrent)})")

    case.assignee_ref = ref
    await session.flush()
    return case


async def release_case(session: AsyncSession, *, tenant_id: uuid.UUID, case_id: uuid.UUID) -> Case:
    """Return a case to the queue.

    Idempotent on purpose: releasing an already-unassigned case is a no-op, not
    an error. An operator clicking release twice should not get a 409.
    """
    case = (
        await session.execute(select(Case).where(Case.tenant_id == tenant_id, Case.id == case_id))
    ).scalar_one_or_none()
    if case is None:
        raise AssignmentError("no such case")
    case.assignee_ref = None
    await session.flush()
    return case
