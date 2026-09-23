"""Quality metrics aggregation (ticket 35, docs/testing-and-evaluation.md).

Deterministic aggregation over AgentRun + Citation + Case rows (no LLM
judging here — rubric scoring plugs into the evaluation runner instead).
Produces the five dashboard numbers docs/development-plan.md Phase 4 names:
supported resolution, wrong resolution, abstention, handoff, citation
coverage — plus route distribution and latency percentiles.

Supported vs wrong resolution is derived from the Case, not from the run:
a Case that was resolved and never reopened is a resolution that held; one
that was reopened is a resolution that did not. Deriving it from
`CaseStatus.RESOLVED` alone would count both as wins.
"""

import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from platform_core.agent_runtime.models import AgentRun, RunStatus, run_executed
from platform_core.cases.models import Case, CaseStatus
from platform_core.evaluation.gates import ReadToolOutcome


@dataclass
class QualityMetrics:
    window_seconds: int
    total_runs: int = 0
    completed: int = 0
    abstained: int = 0
    handed_off: int = 0
    failed: int = 0
    runs_with_citations: int = 0
    route_counts: dict[str, int] = field(default_factory=dict)
    # Rows this tenant owns that carry no `started_at` (written before
    # migration 0012). Reported so a shrinking window is explainable.
    untimed_runs: int = 0
    # Rows in the window that were queued and never executed. Excluded from
    # every count above - they carry the queue's default route and no intent,
    # so including them reported route traffic that never happened. Reported
    # here for the same reason as `untimed_runs`: the exclusion should be
    # visible, not inferred from a total that does not add up.
    never_executed_runs: int = 0
    latency_p50_ms: int | None = None
    latency_p95_ms: int | None = None
    # Resolution outcomes over Cases touched in the window.
    cases_measured: int = 0
    supported_resolution: int = 0
    wrong_resolution: int = 0
    open_cases: int = 0
    # Ratios derived after aggregation
    abstention_rate: float = 0.0
    handoff_rate: float = 0.0
    citation_coverage: float = 1.0
    supported_resolution_rate: float = 0.0
    wrong_resolution_rate: float = 0.0
    # Why runs reached a person, by reason code. This is the leak analysis
    # (feature list 8.1): "which questions still go to a human, and how many
    # of them" is the question that decides what to automate next. A single
    # handoff rate cannot answer it - 30% handoffs that are all complaints and
    # 30% that are all missing documents need opposite responses.
    handoff_reason_counts: dict[str, int] = field(default_factory=dict)

    def finalize(self) -> "QualityMetrics":
        if self.total_runs:
            self.abstention_rate = round(self.abstained / self.total_runs, 4)
            self.handoff_rate = round(self.handed_off / self.total_runs, 4)
            completed = max(self.completed, 1)
            self.citation_coverage = round(self.runs_with_citations / completed, 4)
        # Denominator is resolved-or-reopened Cases only: an open Case has
        # not yet had the chance to be a wrong resolution, and counting it
        # as either would make the rate move with backlog rather than quality.
        if self.cases_measured:
            self.supported_resolution_rate = round(
                self.supported_resolution / self.cases_measured, 4
            )
            self.wrong_resolution_rate = round(self.wrong_resolution / self.cases_measured, 4)
        return self


# --- What a handoff means ---------------------------------------------------
#
# A handoff count alone tells an operator nothing actionable, because the
# reasons point in opposite directions. Sorting them is the whole value of a
# leak analysis (feature list 8.1), and getting it wrong is worse than not
# having it: a dashboard that says "automate 200 handoffs" without saying
# which kind will get the red lines automated.

# The platform had the answer and chose not to - or the corpus simply does
# not contain it. Adding a document, or fixing a conflicting one, turns these
# into answered questions. These are the automation candidates.
KNOWLEDGE_SHAPED_REASONS = frozenset(
    {
        "NO_AUTHORIZED_EVIDENCE",
        "EVIDENCE_BELOW_THRESHOLD",
        "CONFLICTING_SOURCES",
        "CLARIFICATION_LIMIT",
        "OUT_OF_SCOPE",
        "UNSUPPORTED_CLAIM",
    }
)

# A person must decide, and no amount of documentation changes that. The
# complaint gate, the commercial-commitment red line, sensitive requests and
# EQ confirmations are controls - routing them to automation would be undoing
# the control that produced them.
HUMAN_BY_POLICY_REASONS = frozenset(
    {
        "COMPLAINT_REQUIRES_HUMAN",
        "STRATEGIC_ACCOUNT_REQUIRES_HUMAN",
        "REDLINE_COMMERCIAL_COMMITMENT",
        "SENSITIVE_REQUEST",
        "HUMAN_REQUIRED",
        "EQ_CONFIRMATION_REQUIRES_HUMAN",
    }
)

# A clarification is not a handoff: the run kept the conversation and asked
# the customer, who is still there. Counting it as "sent to a human" would
# inflate the very number someone is about to act on.
CLARIFICATION_REASONS = frozenset({"NEEDS_CLARIFICATION", "WRITE_INTENT_UNCERTAIN"})

_NO_REASON = "(none)"


async def gap_samples_by_reason(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    per_reason: int = 3,
) -> dict[str, list[str]]:
    """The actual unanswered questions behind each handoff reason.

    A candidate that says "NO_AUTHORIZED_EVIDENCE x 42" tells an operator how
    big the gap is and not what it is, and "go document something" is not an
    instruction anyone can act on. The gap queue already holds the questions
    that produced those handoffs, so this reads them back by reason - which is
    what turns the leak analysis into a work queue.

    Ordered by frequency: the question customers asked most is the one worth
    answering first. Resolved gaps are excluded - they already have an answer.
    """
    from platform_core.knowledge.gap_models import GapStatus, KnowledgeGap

    rows = (
        await session.execute(
            select(KnowledgeGap.reason_code, KnowledgeGap.sample_question)
            .where(
                KnowledgeGap.tenant_id == tenant_id,
                KnowledgeGap.status != GapStatus.RESOLVED.value,
            )
            .order_by(KnowledgeGap.frequency.desc(), KnowledgeGap.last_seen_at.desc())
        )
    ).all()

    samples: dict[str, list[str]] = {}
    for reason_code, question in rows:
        bucket = samples.setdefault(str(reason_code), [])
        if len(bucket) < per_reason and question:
            bucket.append(str(question))
    return samples


def automation_candidates(metrics: "QualityMetrics") -> list[dict[str, object]]:
    """Handoff reasons ranked by volume, each saying whether it is ours to fix.

    `automatable` is the field that makes this a leak analysis rather than a
    histogram. `True` means the gap is in the corpus and writing a document
    closes it; `False` means a control decided, and the honest response is
    capacity planning, not automation. An unknown reason is reported as
    neither - classifying it by guesswork is how a red line gets automated.
    """
    items: list[dict[str, object]] = []
    for reason, count in sorted(
        metrics.handoff_reason_counts.items(), key=lambda kv: (-kv[1], kv[0])
    ):
        if reason in HUMAN_BY_POLICY_REASONS:
            automatable, rationale = False, "policy: a person must decide"
        elif reason in KNOWLEDGE_SHAPED_REASONS:
            automatable, rationale = True, "evidence gap: a document can close it"
        else:
            automatable, rationale = False, "unclassified: review before acting"
        items.append(
            {"reason": reason, "count": count, "automatable": automatable, "rationale": rationale}
        )
    return items


def percentile(sorted_values: list[int], pct: float) -> int | None:
    if not sorted_values:
        return None
    idx = min(int(len(sorted_values) * pct), len(sorted_values) - 1)
    return sorted_values[idx]


async def aggregate_quality_metrics(
    session: AsyncSession, *, tenant_id: uuid.UUID, window_seconds: int = 3600
) -> QualityMetrics:
    """Aggregate AgentRun rows in the trailing window.

    RLS context must be set on the session by the caller (same convention as
    other services).

    The window filters on `started_at`, which is nullable: rows written
    before migration 0012 have no timestamp. They are excluded rather than
    assigned `now`, because counting an old run as current would inflate the
    dashboard exactly when an operator is looking at it for an incident.
    The count of excluded rows is reported so the gap is visible.
    """
    cutoff = _now() - window_seconds
    stmt = select(AgentRun).where(
        AgentRun.tenant_id == tenant_id,
        AgentRun.started_at.is_not(None),
        AgentRun.started_at >= cutoff,
        run_executed(),
    )
    rows = (await session.execute(stmt)).scalars().all()

    metrics = QualityMetrics(window_seconds=window_seconds)
    latencies: list[int] = []
    for run in rows:
        metrics.total_runs += 1
        metrics.route_counts[run.route] = metrics.route_counts.get(run.route, 0) + 1
        if run.status == RunStatus.COMPLETED.value:
            metrics.completed += 1
        elif run.status == RunStatus.ABSTAINED.value:
            metrics.abstained += 1
            reason = (run.abstain_reason or _NO_REASON).strip() or _NO_REASON
            # A clarification did not send the customer anywhere, so it is not
            # a leak and must not appear in the automation queue.
            if reason not in CLARIFICATION_REASONS:
                metrics.handoff_reason_counts[reason] = (
                    metrics.handoff_reason_counts.get(reason, 0) + 1
                )
        elif run.status == RunStatus.HANDED_OFF.value:
            metrics.handed_off += 1
            reason = (run.abstain_reason or _NO_REASON).strip() or _NO_REASON
            metrics.handoff_reason_counts[reason] = metrics.handoff_reason_counts.get(reason, 0) + 1
        elif run.status == RunStatus.FAILED.value:
            metrics.failed += 1
        if run.latency_ms is not None:
            latencies.append(int(run.latency_ms))

    # Observability: how many rows the window could not place in time.
    metrics.untimed_runs = int(
        (
            await session.execute(
                select(func.count())
                .select_from(AgentRun)
                .where(AgentRun.tenant_id == tenant_id, AgentRun.started_at.is_(None))
            )
        ).scalar_one()
    )

    # Observability: rows in the window that never executed, excluded above.
    # Windowed, unlike `untimed_runs` - that one counts the tenant's lifetime
    # untimed rows, which is a different question, and this one is about the
    # window the operator is looking at.
    metrics.never_executed_runs = int(
        (
            await session.execute(
                select(func.count())
                .select_from(AgentRun)
                .where(
                    AgentRun.tenant_id == tenant_id,
                    AgentRun.started_at.is_not(None),
                    AgentRun.started_at >= cutoff,
                    ~run_executed(),
                )
            )
        ).scalar_one()
    )

    # Citation coverage: runs whose status is completed must carry citations.
    if rows:
        run_ids = [r.id for r in rows if r.status == RunStatus.COMPLETED.value]
        if run_ids:
            from platform_core.agent_runtime.models import Citation

            counted = (
                (
                    await session.execute(
                        select(Citation.agent_run_id)
                        .where(
                            Citation.tenant_id == tenant_id,
                            Citation.agent_run_id.in_(run_ids),
                        )
                        .group_by(Citation.agent_run_id)
                    )
                )
                .scalars()
                .all()
            )
            metrics.runs_with_citations = len(set(counted))

    latencies.sort()
    metrics.latency_p50_ms = percentile(latencies, 0.50)
    metrics.latency_p95_ms = percentile(latencies, 0.95)
    await _aggregate_resolution(session, tenant_id=tenant_id, cutoff=cutoff, metrics=metrics)
    return metrics.finalize()


async def _aggregate_resolution(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    cutoff: int,
    metrics: QualityMetrics,
) -> None:
    """Roll Case outcomes into the report.

    "Touched in the window" means the Case was opened, resolved or reopened
    inside it, so a Case resolved this week shows up in this week's numbers
    even if it was opened last month. The two counters are disjoint:

    - `supported_resolution`  resolved or closed, and never reopened;
    - `wrong_resolution`      ever reached RESOLVED and later reopened.

    A Case that is currently REOPENED counts as a wrong resolution: it did
    resolve once, and that resolution did not hold. A Case closed without
    ever being resolved (e.g. spam, duplicate) is deliberately excluded from
    both rather than scored as a win.
    """
    rows = (
        await session.execute(
            select(Case.status, Case.resolved_at, Case.closed_at).where(
                Case.tenant_id == tenant_id,
                (Case.opened_at >= cutoff)
                | (Case.resolved_at >= cutoff)
                | (Case.closed_at >= cutoff)
                | (Case.status == CaseStatus.REOPENED.value),
            )
        )
    ).all()

    for status, resolved_at, closed_at in rows:
        if status == CaseStatus.REOPENED.value:
            # Resolved at least once (the transition is one-way into it) and
            # since reopened: the resolution did not hold.
            metrics.cases_measured += 1
            metrics.wrong_resolution += 1
        elif status == CaseStatus.RESOLVED.value and resolved_at is not None:
            # Resolved and still resolved. `resolved_at >= cutoff` is not
            # re-checked because the query above already windowed on it.
            metrics.cases_measured += 1
            metrics.supported_resolution += 1
        elif status == CaseStatus.CLOSED.value and resolved_at is not None:
            # Closed *after* being resolved, and never reopened (a reopen is
            # excluded above because the status would be REOPENED or have
            # come back through RESOLVED again).
            metrics.cases_measured += 1
            metrics.supported_resolution += 1
        elif status == CaseStatus.CLOSED.value and closed_at is not None and closed_at >= cutoff:
            # Closed without resolution: neither a win nor a failure.
            continue
        else:
            metrics.open_cases += 1


async def aggregate_read_tool_outcomes(
    session: AsyncSession, *, tenant_id: uuid.UUID, window_seconds: int = 3600
) -> ReadToolOutcome:
    """Read-tool success over the trailing window, from real executions.

    Feeds the documented release gate ("read-tool success excluding
    third-party outage: >= 99%"). Reads `tool_executions` joined to
    `tool_definitions` so only `risk = 'read'` tools are counted - a write
    that failed authorization is a policy outcome, not a read availability
    signal, and folding it in would let a tightening of policy look like a
    broken tool.

    Status mapping:
      verified/executed  -> succeeded
      failed             -> failed, unless error_code marks a provider fault
      unknown/executing  -> not counted (ambiguous is not evidence either way)
    """
    from platform_core.tool_gateway.gateway import THIRD_PARTY_ERROR_CODES
    from platform_core.tool_gateway.models import ToolDefinition, ToolExecution

    cutoff = _now() - window_seconds
    rows = (
        await session.execute(
            select(ToolExecution.status, ToolExecution.error_code)
            .join(ToolDefinition, ToolDefinition.id == ToolExecution.tool_definition_id)
            .where(
                ToolExecution.tenant_id == tenant_id,
                ToolDefinition.risk == "read",
                ToolExecution.started_at >= cutoff,
            )
        )
    ).all()

    outcome = ReadToolOutcome()
    succeeded = outcome.succeeded
    failed = outcome.failed
    third_party = outcome.third_party_failures
    for status, error_code in rows:
        if status in ("executed", "verified"):
            succeeded += 1
        elif status == "failed":
            if error_code in THIRD_PARTY_ERROR_CODES:
                third_party += 1
            else:
                failed += 1
    # `ReadToolOutcome` is frozen on purpose (it is a value object handed to
    # the gate, not a mutable accumulator), so the totals are built here and
    # frozen once at the end.
    return ReadToolOutcome(succeeded=succeeded, failed=failed, third_party_failures=third_party)


def _now() -> int:
    import time

    return int(time.time())


# --- Feature list 8.7: intent distribution and trend -----------------------
#
# The distribution is the operational question "what are customers actually
# asking about", which a handoff rate cannot answer: two tenants with the same
# 30% handoff rate need opposite responses if one is all PCB quoting and the
# other is all missing documents.
#
# It reads the intent snapshot each run already stores in `model_config`
# (written by `_model_config`), so it needs no new write path and no migration:
# every historical run is included from the moment the field existed. Runs
# written before a dimension existed - or under a route that never classified
# - are counted under `UNRECORDED` rather than dropped, because a dashboard
# that silently omits what it could not measure looks complete while hiding
# exactly the gap an operator needs to see.
UNRECORDED = "unrecorded"


@dataclass
class TrendBucket:
    """One time bucket of the trend.

    Mutable on purpose: unlike `ReadToolOutcome`, which is frozen because it is
    a value handed to a gate, this is an accumulator that is filled as runs are
    walked. Freezing it turned the increment into a FrozenInstanceError the
    first time a second run landed in the same bucket.
    """

    bucket_start: int
    total: int
    by_business_line: dict[str, int]


@dataclass
class IntentDistribution:
    """Counts per intent axis, plus a time trend (feature list 8.7)."""

    window_seconds: int = 0
    bucket_seconds: int = 0
    total_runs: int = 0
    by_scene: dict[str, int] = field(default_factory=dict)
    by_kind: dict[str, int] = field(default_factory=dict)
    by_business_line: dict[str, int] = field(default_factory=dict)
    trend: list[TrendBucket] = field(default_factory=list)
    # Queued but never executed, excluded from every count above. Reported so
    # the size of the exclusion is visible instead of having to be inferred.
    never_executed_runs: int = 0


def _intent_snapshot(run: Any) -> dict[str, Any]:
    """The run's recorded intent classification, or {} if it has none."""
    config = getattr(run, "model_config", None)
    if not isinstance(config, dict):
        return {}
    intent = config.get("intent")
    return intent if isinstance(intent, dict) else {}


def _bump(counter: dict[str, int], key: str | None) -> None:
    name = _clean(key) if key else UNRECORDED
    counter[name] = counter.get(name, 0) + 1


def _clean(key: str) -> str:
    return key.strip() or UNRECORDED


async def aggregate_intent_distribution(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    window_seconds: int = 86400,
    bucket_seconds: int = 3600,
) -> IntentDistribution:
    """Intent counts per axis, bucketed over time.

    RLS context must be set by the caller, as elsewhere in this module. The
    window filters on `started_at` and excludes null-timestamped rows for the
    same reason `aggregate_quality_metrics` does.
    """
    cutoff = _now() - window_seconds
    stmt = select(AgentRun).where(
        AgentRun.tenant_id == tenant_id,
        AgentRun.started_at.is_not(None),
        AgentRun.started_at >= cutoff,
        run_executed(),
    )
    rows = (await session.execute(stmt)).scalars().all()

    result = IntentDistribution(window_seconds=window_seconds, bucket_seconds=bucket_seconds)
    buckets: dict[int, TrendBucket] = {}
    for run in rows:
        result.total_runs += 1
        intent = _intent_snapshot(run)
        _bump(result.by_scene, intent.get("scene"))
        _bump(result.by_kind, intent.get("primary_kind"))
        line = _clean(intent.get("business_line") or UNRECORDED)
        _bump(result.by_business_line, intent.get("business_line"))

        started = int(run.started_at or 0)
        bucket_start = (started // bucket_seconds) * bucket_seconds
        bucket = buckets.get(bucket_start)
        if bucket is None:
            bucket = TrendBucket(bucket_start=bucket_start, total=0, by_business_line={})
            buckets[bucket_start] = bucket
        bucket.total += 1
        bucket.by_business_line[line] = bucket.by_business_line.get(line, 0) + 1

    # Rows in the window that never executed. Without this the `unrecorded`
    # bucket becomes part fiction: a placeholder has no intent snapshot at
    # all, so every one of them landed there and the bucket that is supposed
    # to mean "this run predates the field" instead meant "plus everything
    # that never ran".
    result.never_executed_runs = int(
        (
            await session.execute(
                select(func.count())
                .select_from(AgentRun)
                .where(
                    AgentRun.tenant_id == tenant_id,
                    AgentRun.started_at.is_not(None),
                    AgentRun.started_at >= cutoff,
                    ~run_executed(),
                )
            )
        ).scalar_one()
    )

    result.trend = [buckets[key] for key in sorted(buckets)]
    return result
