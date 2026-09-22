"""Quality dashboard API (ticket 35, docs/development-plan.md Phase 4).

    "Quality dashboard: supported resolution, wrong resolution, abstention,
     handoff, citation coverage."

Read-only aggregation over the caller's own AgentRun rows. Two authorization
properties matter here and both are enforced:

1. Role gate - quality data is operational telemetry, so it is gated on
   `Action.AUDIT_READ`, the same audience as the audit trail (auditor,
   security_admin, tenant_owner). A support agent can read cases but not the
   tenant-wide quality picture.
2. Tenant gate - the statement binds `tenant_id` server-side from the
   resolved context and the session carries the RLS binding. A dashboard is
   the classic place a leak hides, because it returns counts rather than
   rows: an unbound query does not look wrong, it just returns a bigger
   number.
"""

from typing import Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from platform_core.api import error_response, tenant_session
from platform_core.evaluation.metrics import (
    aggregate_intent_distribution,
    aggregate_quality_metrics,
    automation_candidates,
    gap_samples_by_reason,
)
from platform_core.identity import tenant_context
from platform_core.identity.tenant_context import TenantContext
from platform_policy import Action, Decision, PolicyEngine, Principal

router = APIRouter(prefix="/v1/quality", tags=["quality"])

# Bounded so a dashboard cannot be used to run an unbounded scan.
MAX_WINDOW_SECONDS = 30 * 24 * 3600


class QualityMetricsOut(BaseModel):
    window_seconds: int
    total_runs: int
    completed: int
    abstained: int
    handed_off: int
    failed: int
    untimed_runs: int
    # Queued but never executed, excluded from every count above. Exposed for
    # the same reason as `untimed_runs`: the operator should be able to see
    # what the totals leave out rather than reconciling them by hand.
    never_executed_runs: int
    abstention_rate: float
    handoff_rate: float
    citation_coverage: float
    route_counts: dict[str, int]
    latency_p50_ms: int | None
    latency_p95_ms: int | None
    # Resolution outcomes (docs/development-plan.md Phase 4 names both).
    cases_measured: int
    supported_resolution: int
    wrong_resolution: int
    open_cases: int
    supported_resolution_rate: float
    wrong_resolution_rate: float
    # Leak analysis (feature list 8.1): how many runs reached a person and
    # why, plus which of those reasons are ours to fix.
    handoff_reason_counts: dict[str, int] = Field(default_factory=dict)
    automation_candidates: list[dict[str, object]] = Field(default_factory=list)
    # reason -> the questions customers actually asked, so a candidate can be
    # acted on. Empty for policy reasons, which are not knowledge gaps and are
    # deliberately never queued for documentation.
    # Corrections awaiting review (7.8) - reviewed knowledge that has not been
    # written yet.
    pending_corrections: int = 0


def _principal_from_ctx(ctx: TenantContext) -> Principal:
    return Principal(
        tenant_id=str(ctx.tenant_id),
        actor_id=str(ctx.actor_id) if ctx.actor_id else "",
        role=ctx.role or "unknown",
    )


async def _aggregate(
    session: AsyncSession, *, tenant_id: Any, window_seconds: int
) -> QualityMetricsOut:
    metrics = await aggregate_quality_metrics(
        session, tenant_id=tenant_id, window_seconds=window_seconds
    )
    # Attach the questions behind each reason so the candidate list is a work
    # queue rather than a histogram. Done here rather than inside
    # `automation_candidates` so that function stays pure and testable.
    samples = await gap_samples_by_reason(session, tenant_id=tenant_id)
    candidates = []
    for item in automation_candidates(metrics):
        enriched = dict(item)
        enriched["sample_questions"] = samples.get(str(item["reason"]), [])
        candidates.append(enriched)

    # 7.8: how much reviewed knowledge is waiting to be written. A correction
    # that nobody knows about is a correction that never becomes an answer, so
    # it belongs on the same screen as the leak analysis - they are the two
    # halves of "what should we fix next".
    from platform_core.knowledge.correction_models import AnswerCorrection, CorrectionStatus

    pending = (
        await session.execute(
            select(func.count())
            .select_from(AnswerCorrection)
            .where(
                AnswerCorrection.tenant_id == tenant_id,
                AnswerCorrection.status == CorrectionStatus.PENDING.value,
            )
        )
    ).scalar_one()
    return QualityMetricsOut(
        window_seconds=metrics.window_seconds,
        total_runs=metrics.total_runs,
        completed=metrics.completed,
        abstained=metrics.abstained,
        handed_off=metrics.handed_off,
        failed=metrics.failed,
        untimed_runs=metrics.untimed_runs,
        never_executed_runs=metrics.never_executed_runs,
        abstention_rate=metrics.abstention_rate,
        handoff_rate=metrics.handoff_rate,
        citation_coverage=metrics.citation_coverage,
        route_counts=metrics.route_counts,
        latency_p50_ms=metrics.latency_p50_ms,
        latency_p95_ms=metrics.latency_p95_ms,
        cases_measured=metrics.cases_measured,
        supported_resolution=metrics.supported_resolution,
        wrong_resolution=metrics.wrong_resolution,
        open_cases=metrics.open_cases,
        supported_resolution_rate=metrics.supported_resolution_rate,
        wrong_resolution_rate=metrics.wrong_resolution_rate,
        handoff_reason_counts=metrics.handoff_reason_counts,
        automation_candidates=candidates,
        pending_corrections=int(pending),
    )


def _denied(reason: str) -> JSONResponse:
    """403, not a 200 with an error body (see prompt_router for the why)."""
    return error_response(
        "QUALITY_ACCESS_DENIED",
        reason or "quality access denied",
        status_code=403,
    )


@router.get("/metrics")
async def get_quality_metrics(
    request: Request,
    window_seconds: int = Query(default=3600, ge=60, le=MAX_WINDOW_SECONDS),
) -> Any:
    ctx = getattr(request.state, "tenant_context", None)
    if ctx is None:
        ctx = tenant_context.get_tenant_context()

    gate = PolicyEngine().check(_principal_from_ctx(ctx), Action.AUDIT_READ)
    if gate.decision != Decision.ALLOW.value:
        return _denied(gate.reason_code)

    async with tenant_session(ctx) as session:
        payload = await _aggregate(session, tenant_id=ctx.tenant_id, window_seconds=window_seconds)
    return payload.model_dump()


@router.get("/routes")
async def get_route_distribution(
    request: Request,
    window_seconds: int = Query(default=3600, ge=60, le=MAX_WINDOW_SECONDS),
) -> Any:
    """Route mix on its own.

    Split out because answering "did traffic shift from knowledge_qa to
    human_required after the last prompt change?" should not require reading
    a full metrics payload, and because it is the signal most likely to be
    polled by an alerting rule.
    """
    ctx = getattr(request.state, "tenant_context", None)
    if ctx is None:
        ctx = tenant_context.get_tenant_context()

    gate = PolicyEngine().check(_principal_from_ctx(ctx), Action.AUDIT_READ)
    if gate.decision != Decision.ALLOW.value:
        return _denied(gate.reason_code)

    async with tenant_session(ctx) as session:
        payload = await _aggregate(session, tenant_id=ctx.tenant_id, window_seconds=window_seconds)
    return {
        "window_seconds": window_seconds,
        "route_counts": payload.route_counts,
        "total_runs": payload.total_runs,
    }


@router.get("/intent-distribution")
async def get_intent_distribution(
    request: Request,
    window_seconds: int = Query(default=86400, ge=60, le=MAX_WINDOW_SECONDS),
    bucket_seconds: int = Query(default=3600, ge=60, le=86400),
) -> Any:
    """Feature list 8.7: what customers are asking about, and how it moves.

    Separate from `/routes` because route is *which machinery answered*, while
    these axes are *what the question was*. A tenant whose handoffs are rising
    needs to know whether the rise is in complaints or in a product line, and
    no route count can tell them that.

    The trend is bucketed rather than returned as one total because the point
    of a trend is the direction: "PCB questions doubled after the price-list
    change" is a decision, while "there are 40 PCB questions" is not.

    Counts are per-run and include runs whose classification predates a given
    axis - those land under `unrecorded` rather than being dropped, so a
    partly-measured window is visibly partial. Runs that were queued and never
    executed are excluded, and their count is returned as
    `never_executed_runs`: they have no classification at all, so including
    them made `unrecorded` mean "predates the field, plus everything that
    never ran" - which is not a state any operator can act on.
    """
    ctx = getattr(request.state, "tenant_context", None)
    if ctx is None:
        ctx = tenant_context.get_tenant_context()

    gate = PolicyEngine().check(_principal_from_ctx(ctx), Action.AUDIT_READ)
    if gate.decision != Decision.ALLOW.value:
        return _denied(gate.reason_code)

    async with tenant_session(ctx) as session:
        dist = await aggregate_intent_distribution(
            session,
            tenant_id=ctx.tenant_id,
            window_seconds=window_seconds,
            bucket_seconds=bucket_seconds,
        )
    return {
        "window_seconds": dist.window_seconds,
        "bucket_seconds": dist.bucket_seconds,
        "total_runs": dist.total_runs,
        "never_executed_runs": dist.never_executed_runs,
        "by_scene": dist.by_scene,
        "by_kind": dist.by_kind,
        "by_business_line": dist.by_business_line,
        "trend": [
            {
                "bucket_start": bucket.bucket_start,
                "total": bucket.total,
                "by_business_line": bucket.by_business_line,
            }
            for bucket in dist.trend
        ],
    }


__all__ = ["router", "MAX_WINDOW_SECONDS", "QualityMetricsOut"]
