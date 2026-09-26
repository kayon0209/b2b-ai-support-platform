"""Per-agent performance API.

    GET /v1/quality/agents?window_seconds=...

Gated on `AUDIT_READ`, the same audience as the rest of `/v1/quality` - agent
performance is operational telemetry, and a support agent can read cases without
being able to read a league table of their colleagues.

Deliberately a read-only view. There is no "set a target" endpoint: a target the
platform cannot enforce is a number that only ever gets argued about, and the
one thing this report is for - "who is overloaded and is the copilot helping" -
needs no configuration to answer.
"""

from typing import Any

from fastapi import APIRouter, Query, Request

from platform_core.api import error_response, get_context, require_policy, tenant_session
from platform_core.evaluation.agent_metrics import (
    AgentPerformance,
    agent_performance_report,
)
from platform_core.evaluation.router import MAX_WINDOW_SECONDS
from platform_policy import Action

router = APIRouter(prefix="/v1/quality/agents", tags=["quality"])


def _agent_out(stat: AgentPerformance) -> dict[str, Any]:
    return {
        "user_ref": stat.user_ref,
        "display_name": stat.display_name,
        "status": stat.status,
        "max_concurrent": stat.max_concurrent,
        "open_cases": stat.open_cases,
        "utilisation": stat.utilisation,
        "resolved_in_window": stat.resolved_in_window,
        "reopened_in_window": stat.reopened_in_window,
        "first_time_fix_rate": stat.first_time_fix_rate,
        "first_response_minutes_p50": stat.first_response_minutes_p50,
        "first_response_minutes_p95": stat.first_response_minutes_p95,
        "resolution_minutes_p50": stat.resolution_minutes_p50,
        "resolution_minutes_p95": stat.resolution_minutes_p95,
        "replies_sent": stat.replies_sent,
        "replies_from_ai_suggestion": stat.replies_from_ai_suggestion,
        "replies_from_canned": stat.replies_from_canned,
        "replies_free": stat.replies_free,
        # Named separately rather than folded into `replies_free`: a client that
        # does not report provenance is not evidence that its agents type
        # everything by hand.
        "replies_unknown_origin": stat.replies_unknown_origin,
        "ai_suggestion_adoption": stat.ai_suggestion_adoption,
        "canned_adoption": stat.canned_adoption,
    }


@router.get("")
async def get_agent_performance(
    request: Request,
    window_seconds: int = Query(default=7 * 24 * 3600, ge=60, le=MAX_WINDOW_SECONDS),
) -> Any:
    """Volume, timings and reply adoption per agent.

    Every rate is `None` rather than `0.0` when it has no denominator - see
    `agent_metrics` for why that distinction is load-bearing.
    """
    ctx = get_context(request)
    if ctx is None:
        return error_response("AUTH_UNRESOLVED", "tenant context not resolved", status_code=401)
    denied = require_policy(ctx, Action.AUDIT_READ)
    if denied is not None:
        return denied

    async with tenant_session(ctx) as session:
        report = await agent_performance_report(
            session, tenant_id=ctx.tenant_id, window_seconds=window_seconds
        )
    return {
        "window_seconds": report.window_seconds,
        "truncated": report.truncated,
        "ai_suggestion_adoption": report.ai_suggestion_adoption,
        "unassigned_open_cases": report.unassigned_open_cases,
        "orphaned_open_cases": report.orphaned_open_cases,
        "agents": [_agent_out(a) for a in report.agents],
    }


__all__ = ["router"]
