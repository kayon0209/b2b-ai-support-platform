"""Quality metrics aggregation (ticket 35, docs/testing-and-evaluation.md).

Deterministic aggregation over AgentRun + Citation rows (no LLM judging
here — rubric scoring plugs into the evaluation runner instead). Produces
the dashboard numbers: citation coverage, abstention rate, handoff rate,
route distribution, latency percentiles.
"""

import uuid
from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from platform_core.agent_runtime.models import AgentRun, RunStatus


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
    latency_p50_ms: int | None = None
    latency_p95_ms: int | None = None
    # Ratios derived after aggregation
    abstention_rate: float = 0.0
    handoff_rate: float = 0.0
    citation_coverage: float = 1.0

    def finalize(self) -> "QualityMetrics":
        if self.total_runs:
            self.abstention_rate = round(self.abstained / self.total_runs, 4)
            self.handoff_rate = round(self.handed_off / self.total_runs, 4)
            completed = max(self.completed, 1)
            self.citation_coverage = round(self.runs_with_citations / completed, 4)
        return self


def _percentile(sorted_values: list[int], pct: float) -> int | None:
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
        elif run.status == RunStatus.HANDED_OFF.value:
            metrics.handed_off += 1
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
    metrics.latency_p50_ms = _percentile(latencies, 0.50)
    metrics.latency_p95_ms = _percentile(latencies, 0.95)
    return metrics.finalize()


def _now() -> int:
    import time

    return int(time.time())
