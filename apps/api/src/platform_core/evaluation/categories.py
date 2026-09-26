"""Issue categories: what to automate next, and how to tell it worked.

Feature list 8.1 (漏点分析) and 8.2 (自动化率统计), and the closing link of the
loop the whole product is built around:

    线上会话 → 漏点 → 标记可自动化 → 补齐 → 上线 → 自动化率上升

`metrics.automation_candidates` answers "which *reasons* are we handing off
for". That is necessary and not sufficient: the reasons point at the *fix*, but
the operator's unit of work is a *kind of question* ("加急咨询"), and the two do
not line up. One category can produce three different reasons across a week, and
one reason covers a dozen unrelated categories. Without the category there is no
object to mark, work, or measure - which is exactly the last mile this module
closes.

Four decisions worth stating, because each one is a place a dashboard lies:

1. **The category is derived from the run's own snapshot, never stored on the
   run.** `derive_category` is a pure function of the intent classification
   already in `AgentRun.model_config` (feature 8.7 reads it the same way), so
   history is included from the moment the field existed and there is no
   backfill and no new write path.

2. **"Automated" is defined strictly, and the strictness is the point.** The
   vendors that bill per resolution converged on this the hard way: "the AI
   replied" is not a resolution. A run counts as automated only if it completed
   *and* recorded no abstention *and* the customer did not have to ask again in
   the same conversation. `run.status == COMPLETED` alone would score a
   customer's re-ask as two automations.

3. **A rate over an empty category is `None`, not `0.0`.** "Nobody asked" and
   "everybody had to be escalated" are opposite facts, and the second one is the
   one an operator would act on. Returning 0.0 for the first invites exactly the
   wrong action - this repository has the same rule for CSAT response rate.

4. **Nothing here promotes a category.** The ranking *proposes*; a person marks
   it. An automatically-promoted candidate list is one nobody reads, and the
   state it writes would not be attributable to anyone.
"""

import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from platform_core.agent_runtime.models import AgentRun, RunStatus, run_executed
from platform_core.evaluation.category_models import (
    NEVER_AUTOMATABLE,
    CategoryState,
    FixType,
    IssueCategory,
)
from platform_core.evaluation.metrics import (
    HUMAN_BY_POLICY_REASONS,
    KNOWLEDGE_SHAPED_REASONS,
    UNRECORDED,
)

# --- What is missing, from signals the run already carries ------------------
#
# Reusing `metrics`'s two frozensets rather than restating them is deliberate:
# two lists of "knowledge-shaped reasons" is how the leak analysis and the
# candidate list start disagreeing about what is fixable.

# The platform could not *reach* the data. Split out of the knowledge bucket
# because the fix is a connector or a credential, not a document - and telling
# an operator to "write a document" about an order that a misconfigured ERP
# would have answered is the fastest way to lose their trust in the list.
DATA_SHAPED_REASONS: frozenset[str] = frozenset(
    {
        "TOOL_UNAVAILABLE",
        "TOOL_NO_CANDIDATE",
        "TOOL_ARGUMENT_MISSING",
        "TOOL_EXECUTION_FAILED",
        "TOOL_EXECUTION_UNVERIFIED",
        "BUSINESS_READ_FLAG_OFF",
        "RETRIEVAL_UNAVAILABLE",
        "GENERATOR_UNAVAILABLE",
        "MODEL_UNAVAILABLE",
        # The customer had not proved the data was theirs. Folded in here
        # rather than given its own bucket: the run could not reach the data,
        # and the fix is a flow change (the verify card), not a document.
        "IDENTITY_REQUIRED",
        "IDENTITY_MISMATCH",
        "AMBIGUOUS_ACCOUNT_IDENTITY",
    }
)

# The answer exists; acting on it needs a tool, a confirmation and an approval
# path. Automatable, and the most valuable bucket to automate - these are the
# ones where the customer does not just want an answer, they want the thing
# done.
ACTION_SHAPED_REASONS: frozenset[str] = frozenset(
    {
        "ACTION_REQUEST",
        "WRITE_INTENT_UNCERTAIN",
        "EQ_CONFIRMATION_REQUIRES_HUMAN",
    }
)

# A control decided, and there is no fix. These are the two reasons that must
# never appear in a candidate list, and they are the ones most tempting to
# "automate" when the escalation rate is the only thing being watched.
POLICY_SHAPED_REASONS: frozenset[str] = frozenset(
    {
        "REDLINE_COMMERCIAL_COMMITMENT",
        "STRATEGIC_ACCOUNT_REQUIRES_HUMAN",
    }
)

# Everything else in `HUMAN_BY_POLICY_REASONS` is routing: a person with the
# right skill has to take it, which is a routing-rule problem rather than an
# answer problem. Derived rather than listed, so adding a policy reason to
# `metrics` cannot silently fall through to "unclassified".
ROUTING_SHAPED_REASONS: frozenset[str] = (
    HUMAN_BY_POLICY_REASONS - POLICY_SHAPED_REASONS - ACTION_SHAPED_REASONS
) | frozenset({"RESTRICTED_REQUEST", "EMOTION_ESCALATION"})

# Ranking order when volume alone does not separate two categories. Cheapest
# and most durable first: a document outlives a rule change, a connector
# outlives a document, and a tool is the most work.
_FIX_ORDER: tuple[FixType, ...] = (
    FixType.CONTENT,
    FixType.DATA,
    FixType.ACTION,
    FixType.ROUTING,
    FixType.POLICY,
)


def fix_type_for(*, abstain_reason: str | None, route: str | None = None) -> FixType | None:
    """What a run's own signals say was missing, or None if it was answered.

    Returns None for a run that produced a customer-visible answer - there is
    no gap to classify, and inventing one would put answered categories on the
    work list.
    """
    reason = (abstain_reason or "").strip()
    if not reason:
        return None
    if reason in POLICY_SHAPED_REASONS:
        return FixType.POLICY
    if reason in ACTION_SHAPED_REASONS:
        return FixType.ACTION
    if reason in DATA_SHAPED_REASONS:
        return FixType.DATA
    if reason in KNOWLEDGE_SHAPED_REASONS:
        return FixType.CONTENT
    if reason in ROUTING_SHAPED_REASONS:
        return FixType.ROUTING
    # Unclassified is reported as routing, not as content. Guessing "content"
    # would put an unknown reason on the cheap-to-fix list and produce a
    # document nobody needed; routing sends it to a human to look at, which is
    # what an unknown reason deserves.
    return FixType.ROUTING


# --- The category key -------------------------------------------------------


def derive_category(intent: dict[str, Any] | None) -> str:
    """`{business_line}|{scene}|{primary_kind}` from the run's own snapshot.

    Three axes because two are not enough to be actionable and four is not
    stable. The business line separates "同一句话在不同线含义不同" (feature
    3.2) - a PCB delivery question and a components delivery question are
    different work. The scene and the primary intent kind separate "where is my
    order" from "change my order", which need opposite fixes.

    A run with no snapshot (queued and never executed, or written before the
    field existed) derives the `unrecorded` key rather than being dropped.
    A category that silently disappears is one nobody can fix, and the count of
    unrecorded runs is surfaced on the report so the exclusion is visible.
    """
    if not isinstance(intent, dict) or not intent:
        return f"{UNRECORDED}|{UNRECORDED}|{UNRECORDED}"
    parts = [
        _axis(intent.get("business_line")),
        _axis(intent.get("scene")),
        _axis(intent.get("primary_kind")),
    ]
    return "|".join(parts)


def _axis(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        return UNRECORDED
    # The separator is part of the key's grammar, so a value containing it
    # would let two different categories collide onto one row.
    return value.strip().replace("|", "/")[:63]


def _intent_snapshot(run: AgentRun) -> dict[str, Any]:
    config = getattr(run, "model_config", None)
    if not isinstance(config, dict):
        return {}
    intent = config.get("intent")
    return intent if isinstance(intent, dict) else {}


# --- The strict definition of "automated" -----------------------------------

# How long after an answer a repeat question still counts as "the answer did not
# land". Long enough to catch "what?" and "you didn't answer me", short enough
# that a genuinely new question the next morning is not held against the run.
FOLLOW_UP_SECONDS = 900


def _is_automated(run: AgentRun, *, followed_up: bool) -> bool:
    """Did this run resolve the question, rather than merely reply to it?

    Three ways to fail, and each one has been the definition somewhere:

    - the run did not complete (abstained, handed off, failed, abandoned);
    - it completed but recorded an abstention reason;
    - the customer asked again in the same conversation soon after.

    The third is the one that makes this definition worth having. Without it,
    "automation rate" measures how often the platform *spoke*, and a bot that
    answers every question twice scores 100%.
    """
    if run.status != RunStatus.COMPLETED.value:
        return False
    if (run.abstain_reason or "").strip():
        return False
    return not followed_up


def automated_run_ids(runs: list[AgentRun]) -> set[uuid.UUID]:
    """The runs that resolved their question, by the strict definition.

    Public so a *second* report can use the same rule rather than restating it.
    Two automation rates computed two ways is worse than one rate: the whole
    value of the number is that it means the same thing everywhere it appears,
    and a per-channel figure that disagreed with the per-category figure would
    be reconciled by trusting neither.
    """
    keys = {run.id: derive_category(_intent_snapshot(run)) for run in runs}
    followed = _followed_up(runs, keys)
    return {run.id for run in runs if _is_automated(run, followed_up=run.id in followed)}


def _followed_up(runs: list[AgentRun], keys: dict[uuid.UUID, str]) -> set[uuid.UUID]:
    """Run ids whose conversation asked the same category again soon after.

    Walks each conversation in time order and compares a run with its successor,
    which is the only ordering that answers "did they have to ask again" - a
    later run on a *different* category is a new question, not a failure.
    """
    by_conversation: dict[uuid.UUID, list[AgentRun]] = {}
    for run in runs:
        by_conversation.setdefault(run.conversation_ref_id, []).append(run)

    followed: set[uuid.UUID] = set()
    for conversation_runs in by_conversation.values():
        conversation_runs.sort(key=lambda r: (int(r.started_at or 0), str(r.id)))
        for current, nxt in zip(conversation_runs, conversation_runs[1:], strict=False):
            if keys.get(current.id) != keys.get(nxt.id):
                continue
            gap = int(nxt.started_at or 0) - int(current.started_at or 0)
            if 0 <= gap <= FOLLOW_UP_SECONDS:
                followed.add(current.id)
    return followed


# --- The report -------------------------------------------------------------


@dataclass
class CategoryStat:
    """One category over the window, with its operational state attached."""

    category_key: str
    business_line: str
    scene: str
    primary_kind: str
    state: str = CategoryState.OBSERVED.value
    # What a human confirmed the gap was; "" until someone says.
    confirmed_fix_type: str = ""
    note: str = ""
    marked_at: int | None = None
    automated_at: int | None = None
    runs: int = 0
    automated: int = 0
    escalated: int = 0
    # Gaps by what they need. Empty for a category that never failed.
    fix_type_counts: dict[str, int] = field(default_factory=dict)
    # None when the category has no runs in the window - see the module
    # docstring, decision 3.
    automation_rate: float | None = None
    # `runs * (1 - rate)`: the number of questions this category would stop
    # sending to a person. This is the ranking key, and it is deliberately the
    # *count* rather than the rate - a 0% category asked twice a year is not
    # the most valuable thing to fix, and a rate-only ranking says it is.
    leak_volume: int = 0
    # Set only for a category that has been automated: the rate before and
    # after the change, so the loop closes with a number rather than a claim.
    rate_before: float | None = None
    rate_after: float | None = None

    @property
    def proposed_fix_type(self) -> str:
        """What the run signals say is missing, most frequent first."""
        if not self.fix_type_counts:
            return ""
        return max(self.fix_type_counts.items(), key=lambda kv: (-kv[1], kv[0]))[0]

    @property
    def is_proposable(self) -> bool:
        """Whether this category may appear on the candidate list at all.

        `human_only` is excluded by state; `routing`/`policy` are excluded by
        what is missing. The second exclusion is the load-bearing one - without
        it the list proposes automating complaints every week, and the operator
        learns to ignore the list (the trap the reference products name:
        追 100% 自助解决率).
        """
        if self.state == CategoryState.HUMAN_ONLY.value:
            return False
        fix = self.confirmed_fix_type or self.proposed_fix_type
        if not fix:
            return False
        try:
            return FixType(fix) not in NEVER_AUTOMATABLE
        except ValueError:
            # An unrecognised stored value is not evidence that it is fixable.
            return False


@dataclass
class CategoryReport:
    window_seconds: int
    total_runs: int = 0
    unrecorded_runs: int = 0
    never_executed_runs: int = 0
    categories: list[CategoryStat] = field(default_factory=list)
    # True when the run fetch hit its cap, so the caller knows the numbers are
    # a floor rather than the whole window.
    truncated: bool = False

    @property
    def candidates(self) -> list[CategoryStat]:
        """The work list, ranked. Highest leak volume first, then cheapest fix.

        This is the "该补什么" list. It is a *proposal*: nothing here changes a
        category's state.
        """
        proposable = [c for c in self.categories if c.is_proposable and c.leak_volume > 0]
        return sorted(
            proposable,
            key=lambda c: (
                -c.leak_volume,
                _FIX_ORDER.index(FixType(c.confirmed_fix_type or c.proposed_fix_type))
                if (c.confirmed_fix_type or c.proposed_fix_type)
                and (c.confirmed_fix_type or c.proposed_fix_type) in {f.value for f in _FIX_ORDER}
                else len(_FIX_ORDER),
                c.category_key,
            ),
        )

    @property
    def automation_rate(self) -> float | None:
        """Whole-tenant rate over the window, or None with no runs.

        Named for the headline number the reference products put first
        (Intercom's automation rate). Computed from the same per-run rule as
        the per-category rates, so the headline and the breakdown cannot
        disagree.
        """
        measured = sum(c.runs for c in self.categories)
        if not measured:
            return None
        return round(sum(c.automated for c in self.categories) / measured, 4)


# Bounded so a dashboard cannot run an unbounded scan. Same discipline as
# `router.MAX_WINDOW_SECONDS`.
MAX_RUNS_SCANNED = 20000


async def category_report(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    window_seconds: int = 7 * 24 * 3600,
) -> CategoryReport:
    """Per-category volume, automation rate, gap attribution and state.

    RLS context must be set by the caller, as elsewhere in this package.

    Reads `agent_runs` and `issue_categories` only. The run scan is widened to
    cover a category's own `automated_at` so the before/after comparison has
    the data it needs without a second query per category.
    """
    states = {
        row.category_key: row
        for row in (
            await session.execute(select(IssueCategory).where(IssueCategory.tenant_id == tenant_id))
        )
        .scalars()
        .all()
    }

    now = _now()
    cutoff = now - window_seconds
    # The widest range any part of this report needs: the window, extended back
    # to just before the earliest automation so `rate_before` has a baseline.
    earliest_automation = min(
        (row.automated_at for row in states.values() if row.automated_at), default=cutoff
    )
    scan_from = min(cutoff, earliest_automation - window_seconds)

    rows = (
        (
            await session.execute(
                select(AgentRun)
                .where(
                    AgentRun.tenant_id == tenant_id,
                    AgentRun.started_at.is_not(None),
                    AgentRun.started_at >= scan_from,
                    run_executed(),
                )
                .order_by(AgentRun.started_at)
                .limit(MAX_RUNS_SCANNED + 1)
            )
        )
        .scalars()
        .all()
    )
    truncated = len(rows) > MAX_RUNS_SCANNED
    runs = list(rows[:MAX_RUNS_SCANNED])

    keys = {run.id: derive_category(_intent_snapshot(run)) for run in runs}
    followed = _followed_up(runs, keys)

    report = CategoryReport(window_seconds=window_seconds, truncated=truncated)
    stats: dict[str, CategoryStat] = {}
    # Before/after accumulators, kept separately because the two halves come
    # from different time ranges than the window.
    before: dict[str, list[int]] = {}
    after: dict[str, list[int]] = {}

    for run in runs:
        started = int(run.started_at or 0)
        if started < cutoff:
            # Outside the window, but it may still be the baseline for a
            # category that has since been automated.
            key = keys[run.id]
            row = states.get(key)
            if row is not None and row.automated_at:
                bucket = before if started < row.automated_at else after
                counts = bucket.setdefault(key, [0, 0])
                counts[0] += 1
                counts[1] += 1 if _is_automated(run, followed_up=run.id in followed) else 0
            continue

        report.total_runs += 1
        key = keys[run.id]
        stat = stats.get(key)
        if stat is None:
            line, scene, kind = (key.split("|") + ["", "", ""])[:3]
            stat = CategoryStat(
                category_key=key,
                business_line=line,
                scene=scene,
                primary_kind=kind,
            )
            stats[key] = stat
        stat.runs += 1

        automated = _is_automated(run, followed_up=run.id in followed)
        if automated:
            stat.automated += 1
        else:
            if run.status in (RunStatus.ABSTAINED.value, RunStatus.HANDED_OFF.value):
                stat.escalated += 1
            fix = fix_type_for(
                abstain_reason=run.abstain_reason,
                route=run.route,
            )
            if fix is not None:
                stat.fix_type_counts[fix.value] = stat.fix_type_counts.get(fix.value, 0) + 1

        row = states.get(key)
        if row is not None and row.automated_at:
            # Both halves of the before/after comparison can fall *inside* the
            # window: the baseline run is outside it only when the automation
            # happened more than one window ago. Bucketing on `automated_at`
            # rather than on the window boundary is what makes the comparison
            # right for a category automated this week, which is the common
            # case and the one an operator is looking at.
            bucket = before if started < row.automated_at else after
            counts = bucket.setdefault(key, [0, 0])
            counts[0] += 1
            counts[1] += 1 if automated else 0

        if key == f"{UNRECORDED}|{UNRECORDED}|{UNRECORDED}":
            report.unrecorded_runs += 1

    for key, stat in stats.items():
        row = states.get(key)
        if row is not None:
            stat.state = row.state
            stat.confirmed_fix_type = row.fix_type
            stat.note = row.note
            stat.marked_at = row.marked_at
            stat.automated_at = row.automated_at
        # A rate over an empty category is None, never 0.0 - see decision 3.
        stat.automation_rate = round(stat.automated / stat.runs, 4) if stat.runs else None
        stat.leak_volume = stat.runs - stat.automated
        if stat.automated_at:
            stat.rate_before = _rate(before.get(key))
            stat.rate_after = _rate(after.get(key))

    # A category that has been marked but has no runs in the window still
    # belongs on the report: "we automated this and nobody asked" is a fact the
    # operator needs, and dropping it would make the state invisible.
    for key, row in states.items():
        if key in stats:
            continue
        line, scene, kind = (key.split("|") + ["", "", ""])[:3]
        stats[key] = CategoryStat(
            category_key=key,
            business_line=line,
            scene=scene,
            primary_kind=kind,
            state=row.state,
            confirmed_fix_type=row.fix_type,
            note=row.note,
            marked_at=row.marked_at,
            automated_at=row.automated_at,
            rate_before=_rate(before.get(key)),
            rate_after=_rate(after.get(key)),
        )

    report.never_executed_runs = int(
        (
            await session.execute(
                select(AgentRun.id).where(
                    AgentRun.tenant_id == tenant_id,
                    AgentRun.started_at.is_not(None),
                    AgentRun.started_at >= cutoff,
                    ~run_executed(),
                )
            )
        )
        .scalars()
        .all()
        .__len__()
    )

    report.categories = sorted(stats.values(), key=lambda c: (-c.leak_volume, c.category_key))
    return report


def _rate(counts: list[int] | None) -> float | None:
    if not counts or not counts[0]:
        return None
    return round(counts[1] / counts[0], 4)


def _now() -> int:
    import time

    return int(time.time())
