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
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from platform_core.api import error_response
from platform_core.config import get_settings
from platform_core.db import session_scope_with_url
from platform_core.evaluation.metrics import aggregate_quality_metrics
from platform_core.identity import tenant_context
from platform_core.identity.tenant_context import TenantContext, apply_rls_tenant
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
    return QualityMetricsOut(
        window_seconds=metrics.window_seconds,
        total_runs=metrics.total_runs,
        completed=metrics.completed,
        abstained=metrics.abstained,
        handed_off=metrics.handed_off,
        failed=metrics.failed,
        untimed_runs=metrics.untimed_runs,
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

    settings = get_settings()
    app_url = settings.database_url.replace("platform:platform@", "platform_app:platform_app@")
    async with session_scope_with_url(app_url) as session:
        await apply_rls_tenant(session, ctx)
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

    settings = get_settings()
    app_url = settings.database_url.replace("platform:platform@", "platform_app:platform_app@")
    async with session_scope_with_url(app_url) as session:
        await apply_rls_tenant(session, ctx)
        payload = await _aggregate(session, tenant_id=ctx.tenant_id, window_seconds=window_seconds)
    return {
        "window_seconds": window_seconds,
        "route_counts": payload.route_counts,
        "total_runs": payload.total_runs,
    }


__all__ = ["router", "MAX_WINDOW_SECONDS", "QualityMetricsOut"]
