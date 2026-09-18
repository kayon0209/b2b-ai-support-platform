"""SLA escalation: the ladder, and the scan that walks it.

The gap this closes
-------------------
`cases/models.py` could compute a deadline and say whether it had passed, and
**nothing called `is_breached`**. A breached Case therefore stayed breached with
no event, no notification and no record - the platform's entire response to a
missed commitment was that the number was still wrong the next time somebody
looked at it.

Two decisions, both chosen so that the platform can explain itself afterwards

**Escalation never rewrites the priority.** It is tempting - a breached Case
"should" become p0 - but the deadline is computed from the priority, so bumping
it rewrites the very obligation that was missed, and "why is this p0" stops
having a human answer. Priority stays a triage decision made by a person. What
escalation does instead is notify, and at level 2 **route**: set `team_ref` to
the escalation queue.

**Level 2 fills a routing gap; it does not overrule a routing decision.**
`team_ref` is only set when it is still null. A person who already assigned the
Case to their own team has made a decision the ladder has no basis to undo, and
silently re-routing their work is how an escalation system gets turned off.

The ladder is measured from the **deadline**, not from the previous level, so a
scanner that was down for two hours still lands on the right level rather than
walking the ladder one rung per poll.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from platform_core.audit import service as audit_service
from platform_core.cases.models import Case, CaseEscalation, CaseStatus
from platform_core.identity.tenant_context import TenantContext
from platform_core.outbox_service import enqueue

# Seconds past the deadline at which each level fires. Index 0 is level 1, so
# `offset 0` means "as soon as it is breached" and the seconds are measured
# from the deadline rather than from the level below it.
ESCALATION_LADDER_SECONDS: tuple[int, ...] = (0, 3600)

CLOCK_FIRST_RESPONSE = "first_response"
CLOCK_RESOLUTION = "resolution"

# The single escalation queue this deployment routes level-2 breaches to.
#
# A module constant rather than a per-tenant setting, deliberately: there is no
# way for a tenant to set it, and adding a column nobody can write is the
# defect this repository keeps finding (`enterprise_account_id`,
# `DeadLetterItem`, `NEEDS_REAUTH`). When a tenant-admin surface exists to set
# it, this becomes a lookup.
DEFAULT_ESCALATION_TEAM = "sla-escalations"

# Statuses whose clocks are stopped. `WAITING_*` states pause rather than stop
# (see `DEFAULT_SLA.running_states`), so they are NOT terminal and remain
# scannable - a Case waiting on a vendor past its resolution deadline is
# exactly the one worth escalating.
TERMINAL_STATUSES = frozenset({CaseStatus.RESOLVED.value, CaseStatus.CLOSED.value})

AUDIT_ESCALATED = "case.sla_escalated"


@dataclass(frozen=True)
class EscalationTarget:
    """A Case and the clock that has run out on it."""

    case_id: uuid.UUID
    clock: str
    level: int
    deadline: int
    breach_seconds: int
    reason_code: str


@dataclass
class EscalationStats:
    scanned: int = 0
    escalated: int = 0
    skipped_already_escalated: int = 0

    @property
    def changed_rows(self) -> int:
        return self.escalated


def level_for(*, deadline: int, now: int) -> int:
    """The highest ladder level that has come due, or 0 for not yet breached.

    Level *n* fires at `deadline + ESCALATION_LADDER_SECONDS[n-1]`. Deriving it
    from the deadline rather than from the previously recorded level means a
    scanner that missed a poll still reports the correct rung, and a Case that
    breaches overnight does not need two scans to reach level 2.

    `now > deadline` rather than `>=`: matching `cases.models.is_breached`, so
    there is one definition of "breached" in the codebase and not two that
    differ by a second.
    """
    overdue = now - deadline
    if overdue <= 0:
        return 0
    level = 0
    for index, offset in enumerate(ESCALATION_LADDER_SECONDS, start=1):
        if overdue >= offset:
            level = index
    return level


def targets_for_case(
    case: Case, *, now: int, already: set[tuple[str, int]]
) -> list[EscalationTarget]:
    """The escalations this Case is due, minus the ones already recorded.

    `already` is the ledger read for this Case. Filtering here rather than in
    SQL keeps the decision in one readable place, and the UNIQUE constraint
    remains the authority: two workers racing produce one row and one skipped
    insert, which `escalate` reports as `skipped`.
    """
    due: list[EscalationTarget] = []
    if case.status in TERMINAL_STATUSES:
        # A resolved Case that was resolved late is history, not an open
        # commitment. Reopening moves it out of this set and the clocks are
        # live again.
        return due

    clocks: list[tuple[str, int | None, bool]] = [
        # (clock, deadline, already-answered)
        (CLOCK_FIRST_RESPONSE, case.first_response_due_at, case.first_responded_at is not None),
        (CLOCK_RESOLUTION, case.resolution_due_at, case.resolved_at is not None),
    ]
    for clock, deadline, answered in clocks:
        if deadline is None or answered:
            # `answered` is checked here and not in SQL because the two clocks
            # answer differently: a first response is satisfied by
            # `first_responded_at`, a resolution by `resolved_at`, and a Case
            # can have one without the other.
            continue
        level = level_for(deadline=int(deadline), now=now)
        if level == 0:
            continue
        for rung in range(1, level + 1):
            if (clock, rung) in already:
                continue
            due.append(
                EscalationTarget(
                    case_id=case.id,
                    clock=clock,
                    level=rung,
                    deadline=int(deadline),
                    breach_seconds=now - int(deadline),
                    reason_code=f"SLA_{clock.upper()}_BREACHED_L{rung}",
                )
            )
    return due


def route_for(target: EscalationTarget) -> tuple[str | None, str | None]:
    """Where an escalation should be routed, as `(team_ref, note)`.

    Level 1 notifies only. Level 2 additionally nominates the escalation queue.
    `None` for the team means "leave the Case's routing alone" - see the module
    docstring for why a person's routing decision is not overridden here.
    """
    if target.level >= 2:
        return DEFAULT_ESCALATION_TEAM, None
    return None, None


async def _ledger(
    session: AsyncSession, *, tenant_id: uuid.UUID, case_id: uuid.UUID
) -> set[tuple[str, int]]:
    rows = await session.execute(
        select(CaseEscalation.clock, CaseEscalation.level).where(
            CaseEscalation.tenant_id == tenant_id, CaseEscalation.case_id == case_id
        )
    )
    return {(str(clock), int(level)) for clock, level in rows.all()}


async def escalate(
    session: AsyncSession,
    *,
    ctx: TenantContext,
    case: Case,
    target: EscalationTarget,
    trace_id: str | None = None,
) -> bool:
    """Record one escalation. Returns False when it was already recorded.

    The audit event, the outbox event and the ledger row are written in the
    caller's transaction, so "we escalated" and "we said we escalated" cannot
    diverge.
    """
    team_ref, _ = route_for(target)
    applied_team: str | None = None
    if team_ref is not None and case.team_ref is None:
        applied_team = team_ref
        case.team_ref = team_ref

    # `ON CONFLICT DO NOTHING`, not insert-then-catch. Catching the integrity
    # error means rolling back, and a rollback undoes **the whole
    # transaction** - so a sweep that recorded rung 1 and then lost the race on
    # rung 2 would silently discard rung 1, which is worse than the duplicate it
    # was avoiding. A savepoint would fix that, and this is simpler than a
    # savepoint: one statement, no exception, and the loser is told so by the
    # absence of a returned row.
    inserted = (
        await session.execute(
            pg_insert(CaseEscalation)
            .values(
                tenant_id=ctx.tenant_id,
                case_id=case.id,
                clock=target.clock,
                level=target.level,
                reason_code=target.reason_code,
                breach_seconds=target.breach_seconds,
                assignee_ref=case.assignee_ref,
                team_ref=applied_team or case.team_ref,
                escalated_at=int(time.time()),
            )
            .on_conflict_do_nothing(constraint="uq_case_escalation_once")
            .returning(CaseEscalation.id)
        )
    ).one_or_none()
    if inserted is None:
        # Another worker recorded this rung first: the expected way to lose the
        # race, not a failure.
        return False

    await audit_service.record(
        session,
        ctx=ctx,
        action=AUDIT_ESCALATED,
        resource_type="case",
        resource_id=case.id,
        decision="completed",
        reason_code=target.reason_code,
        after={
            "clock": target.clock,
            "level": target.level,
            "breach_seconds": target.breach_seconds,
            "team_ref": applied_team,
        },
        trace_id=trace_id,
    )
    await enqueue(
        session,
        tenant_id=ctx.tenant_id,
        event_type="case.sla_breached",
        aggregate_type="case",
        aggregate_id=str(case.id),
        payload={
            "case_id": str(case.id),
            "clock": target.clock,
            "level": target.level,
            "breach_seconds": target.breach_seconds,
            "routed_to": applied_team,
        },
        trace_id=trace_id,
    )
    return True


async def escalate_due_cases(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    ctx: TenantContext,
    now: int | None = None,
    limit: int = 200,
) -> EscalationStats:
    """One tenant's sweep. The caller owns the transaction and the commit.

    Not committing here is the same contract the ingestion drain follows: a
    scan that fails halfway must not leave half an escalation ladder committed,
    and the unit of work belongs to whoever opened it.
    """
    now = now or int(time.time())
    stats = EscalationStats()

    rows = (
        await session.execute(
            select(Case)
            .where(
                Case.tenant_id == tenant_id,
                Case.status.notin_(tuple(TERMINAL_STATUSES)),
                # Narrow in SQL so the scan does not walk every open Case in
                # the tenant: at least one clock must already be past due.
                (Case.first_response_due_at < now) | (Case.resolution_due_at < now),
            )
            .order_by(Case.opened_at)
            .limit(limit)
        )
    ).scalars()

    for case in rows:
        stats.scanned += 1
        already = await _ledger(session, tenant_id=tenant_id, case_id=case.id)
        for target in targets_for_case(case, now=now, already=already):
            if await escalate(session, ctx=ctx, case=case, target=target, trace_id=None):
                stats.escalated += 1
            else:
                stats.skipped_already_escalated += 1
    return stats


__all__: list[str] = [
    "AUDIT_ESCALATED",
    "CLOCK_FIRST_RESPONSE",
    "CLOCK_RESOLUTION",
    "DEFAULT_ESCALATION_TEAM",
    "ESCALATION_LADDER_SECONDS",
    "EscalationStats",
    "EscalationTarget",
    "TERMINAL_STATUSES",
    "escalate",
    "escalate_due_cases",
    "level_for",
    "route_for",
    "targets_for_case",
]
