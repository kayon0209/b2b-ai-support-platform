"""Per-agent performance and reply adoption (feature list 8.5, 5.x).

Two questions the platform could not answer, and both were answerable only
because the reply path now exists:

1. **How is each person doing?** Volume, first-response time, resolution time,
   reopen rate, and how full they are. `8.5 指标看板` names 人均处理量 and
   一次解决率 directly; without the directory from migration 0048 there was no
   "person" to attribute anything to.
2. **Is the copilot worth anything?** An AI suggestion that no agent ever sends
   is decoration. Adoption is measurable now only because a reply records where
   its text came from (migration 0051).

Four decisions worth stating, because each one is a place this report would lie:

- **An empty adoption rate is `None`, never `0.0`.** "Nobody used a suggestion
  because nobody replied" and "everybody replied from scratch" are opposite
  facts, and the second is the one an operator would act on. Same rule as CSAT
  response rate and category automation rate.
- **Response time is wall-clock from `opened_at`**, not the SLA's
  pause-adjusted `elapsed_running_seconds`. The question "how long did the
  customer wait" is about the customer's clock, and the pause-aware number
  answers a different one ("how much *work* time did we spend").
- **An open case assigned to a ref with no directory entry is counted, not
  dropped.** It is a real state - someone typed a name by hand, or an agent was
  removed from the directory - and hiding it would make the queue look smaller
  than it is.
- **A reply with no `origin` is counted as `unknown`, never as free-typed.**
  Rows written before the column existed are genuinely unknown, and folding them
  into either bucket would bias the number in a direction nobody chose.
"""

import uuid
from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from platform_core.agent_runtime.models import (
    ORIGIN_AI_SUGGESTION,
    ORIGIN_CANNED,
    ORIGIN_FREE,
    ORIGIN_UNKNOWN,
    ConversationTurn,
)
from platform_core.cases.agent_models import AgentProfile, AgentStatus
from platform_core.cases.assignment import OPEN_STATUSES
from platform_core.cases.models import Case, CaseStatus
from platform_core.evaluation.metrics import percentile

# Bounded so a dashboard cannot run an unbounded scan. Same discipline as
# `router.MAX_WINDOW_SECONDS` and `categories.MAX_RUNS_SCANNED`.
MAX_CASES_SCANNED = 20000


@dataclass
class AgentPerformance:
    user_ref: str
    display_name: str
    status: str = AgentStatus.ACTIVE.value
    max_concurrent: int = 0
    # Open right now (all time), not windowed: "how full is this person" is a
    # present-tense question and a window would make it move with the filter.
    open_cases: int = 0
    utilisation: float | None = None
    resolved_in_window: int = 0
    reopened_in_window: int = 0
    # None when the agent has no measured cases - see the module docstring.
    first_response_minutes_p50: int | None = None
    first_response_minutes_p95: int | None = None
    resolution_minutes_p50: int | None = None
    resolution_minutes_p95: int | None = None
    replies_sent: int = 0
    replies_from_ai_suggestion: int = 0
    replies_from_canned: int = 0
    replies_free: int = 0
    replies_unknown_origin: int = 0
    ai_suggestion_adoption: float | None = None
    canned_adoption: float | None = None

    @property
    def first_time_fix_rate(self) -> float | None:
        """Resolved without a reopen, over everything resolved.

        Named for what 8.5 asks for. A reopen is the only evidence the platform
        has that a resolution did not hold, so this is as close to 一次解决率 as
        the data supports - and it is `None`, not 1.0, when nothing resolved.
        """
        measured = self.resolved_in_window + self.reopened_in_window
        if not measured:
            return None
        return round(self.resolved_in_window / measured, 4)


@dataclass
class AgentReport:
    window_seconds: int
    agents: list[AgentPerformance] = field(default_factory=list)
    # Open cases with no assignee at all - the queue's backlog.
    unassigned_open_cases: int = 0
    # Open cases assigned to a ref the directory does not know.
    orphaned_open_cases: int = 0
    # True when the case scan hit its cap, so the numbers are a floor.
    truncated: bool = False

    @property
    def ai_suggestion_adoption(self) -> float | None:
        """Whole-team adoption, from the same rows as the per-agent figures."""
        measured = sum(a.replies_sent for a in self.agents)
        if not measured:
            return None
        return round(sum(a.replies_from_ai_suggestion for a in self.agents) / measured, 4)


def _minutes(start: int | None, end: int | None) -> int | None:
    if start is None or end is None or end < start:
        return None
    return int((end - start) // 60)


async def agent_performance_report(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    window_seconds: int = 7 * 24 * 3600,
) -> AgentReport:
    """Volume, timings and reply adoption per agent.

    RLS context must be set by the caller, as elsewhere in this package.
    """
    now = _now()
    cutoff = now - window_seconds

    directory = {
        row.user_ref: row
        for row in (
            await session.execute(select(AgentProfile).where(AgentProfile.tenant_id == tenant_id))
        )
        .scalars()
        .all()
    }

    report = AgentReport(window_seconds=window_seconds)
    stats: dict[str, AgentPerformance] = {
        ref: AgentPerformance(
            user_ref=ref,
            display_name=row.display_name,
            status=row.status,
            max_concurrent=int(row.max_concurrent),
        )
        for ref, row in directory.items()
    }

    # One scan of the tenant's cases in the window, plus a separate present-tense
    # count of what is open. Two queries rather than one because "open now" is
    # deliberately not windowed.
    rows = (
        await session.execute(
            select(
                Case.assignee_ref,
                Case.status,
                Case.opened_at,
                Case.first_responded_at,
                Case.resolved_at,
                Case.closed_at,
            )
            .where(
                Case.tenant_id == tenant_id,
                (Case.opened_at >= cutoff)
                | (Case.resolved_at >= cutoff)
                | (Case.closed_at >= cutoff)
                | (Case.status == CaseStatus.REOPENED.value),
            )
            .limit(MAX_CASES_SCANNED + 1)
        )
    ).all()
    report.truncated = len(rows) > MAX_CASES_SCANNED

    first_responses: dict[str, list[int]] = {}
    resolutions: dict[str, list[int]] = {}
    for assignee, status, opened_at, first_responded_at, resolved_at, _closed_at in rows[
        :MAX_CASES_SCANNED
    ]:
        ref = str(assignee or "")
        if not ref:
            continue
        stat = stats.get(ref)
        if stat is None:
            # An assignment the directory does not know. Counted, not dropped -
            # see the module docstring.
            stat = AgentPerformance(user_ref=ref, display_name=ref)
            stats[ref] = stat
        if status == CaseStatus.REOPENED.value:
            stat.reopened_in_window += 1
        elif status in (CaseStatus.RESOLVED.value, CaseStatus.CLOSED.value):
            if resolved_at is not None and resolved_at >= cutoff:
                stat.resolved_in_window += 1
        if first_responded_at is not None and first_responded_at >= cutoff:
            minutes = _minutes(opened_at, first_responded_at)
            if minutes is not None:
                first_responses.setdefault(ref, []).append(minutes)
        if resolved_at is not None and resolved_at >= cutoff:
            minutes = _minutes(opened_at, resolved_at)
            if minutes is not None:
                resolutions.setdefault(ref, []).append(minutes)

    # Present tense: what is open, and to whom.
    open_rows = (
        await session.execute(
            select(Case.assignee_ref, func.count())
            .where(Case.tenant_id == tenant_id, Case.status.in_(sorted(OPEN_STATUSES)))
            .group_by(Case.assignee_ref)
        )
    ).all()
    for assignee, count in open_rows:
        if assignee is None:
            report.unassigned_open_cases = int(count)
            continue
        ref = str(assignee)
        stat = stats.get(ref)
        if stat is None:
            stat = AgentPerformance(user_ref=ref, display_name=ref)
            stats[ref] = stat
        if ref not in directory:
            # Not in the directory at all: someone typed a name by hand, or an
            # agent was removed. Keyed off the directory rather than off "this
            # loop just created the stat", because the windowed case scan above
            # creates one for every assigned case it sees.
            report.orphaned_open_cases += int(count)
        stat.open_cases = int(count)

    # Replies, by author and by how the text was composed. One grouped query.
    reply_rows = (
        await session.execute(
            select(ConversationTurn.author_ref, ConversationTurn.origin, func.count())
            .where(
                ConversationTurn.tenant_id == tenant_id,
                ConversationTurn.source == "agent",
                ConversationTurn.ts >= cutoff,
                ConversationTurn.author_ref.is_not(None),
            )
            .group_by(ConversationTurn.author_ref, ConversationTurn.origin)
        )
    ).all()
    for author, origin, count in reply_rows:
        ref = str(author)
        stat = stats.get(ref)
        if stat is None:
            stat = AgentPerformance(user_ref=ref, display_name=ref)
            stats[ref] = stat
        stat.replies_sent += int(count)
        if origin == ORIGIN_AI_SUGGESTION:
            stat.replies_from_ai_suggestion += int(count)
        elif origin == ORIGIN_CANNED:
            stat.replies_from_canned += int(count)
        elif origin == ORIGIN_FREE:
            stat.replies_free += int(count)
        elif origin == ORIGIN_UNKNOWN or not origin:
            stat.replies_unknown_origin += int(count)
        else:
            # An unrecognised value. Counted as unknown rather than as free:
            # guessing would put a number in the denominator that nothing
            # measured. The service refuses such values, so this is only
            # reachable from a row written outside it.
            stat.replies_unknown_origin += int(count)

    for ref, stat in stats.items():
        stat.first_response_minutes_p50 = _p50(first_responses.get(ref))
        stat.first_response_minutes_p95 = _p95(first_responses.get(ref))
        stat.resolution_minutes_p50 = _p50(resolutions.get(ref))
        stat.resolution_minutes_p95 = _p95(resolutions.get(ref))
        # Adoption is over replies *whose origin is known*. Counting the
        # unknown-origin rows in the denominator would make the rate fall every
        # time a client that does not report provenance is used, which is not a
        # fact about the agents.
        known = stat.replies_sent - stat.replies_unknown_origin
        if known:
            stat.ai_suggestion_adoption = round(stat.replies_from_ai_suggestion / known, 4)
            stat.canned_adoption = round(stat.replies_from_canned / known, 4)
        if stat.max_concurrent:
            stat.utilisation = round(stat.open_cases / stat.max_concurrent, 4)

    report.agents = sorted(stats.values(), key=lambda a: (-a.replies_sent, a.user_ref))
    return report


def _p50(values: list[int] | None) -> int | None:
    return percentile(sorted(values), 0.50) if values else None


def _p95(values: list[int] | None) -> int | None:
    return percentile(sorted(values), 0.95) if values else None


def _now() -> int:
    import time

    return int(time.time())
