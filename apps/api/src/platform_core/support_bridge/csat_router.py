"""Satisfaction API.

    GET /v1/quality/csat?window_seconds=...

Gated on `AUDIT_READ`, the same audience as the rest of `/v1/quality`: a
satisfaction average is operational telemetry, not something a support agent
needs to read a case.

Why this file exists
--------------------
`csat.py` was written, migrated (`0044_csat_responses`) and unit-tested, and
**nothing in production called it** - so the platform asked no customer how it
did, and the number every operations dashboard is judged on did not exist. The
customer half was added on 2026-09-23 (`POST /v1/support/rating`); this is the
half that makes the data worth collecting.

Three numbers, not one
----------------------
`average` alone is the number that misleads. `distribution` is returned beside
it because 3.0 from everyone and 3.0 from half fives and half ones are the same
mean and completely different problems - `csat.py` says so, and this endpoint
passes the whole summary through rather than flattening it.

`response_rate` is the denominator that stops a 4.8 from three responses reading
as a healthy platform. It is `None`, not 0.0, when nothing was asked: a rate over
no conversations is undefined, and reporting it as zero would look like every
customer ignored the survey.
"""

from typing import Any

from fastapi import APIRouter, Query, Request

from platform_core.api import error_response, get_context, require_policy, tenant_session
from platform_core.evaluation.router import MAX_WINDOW_SECONDS
from platform_core.support_bridge import csat
from platform_policy import Action

router = APIRouter(prefix="/v1/quality/csat", tags=["quality"])


@router.get("")
async def get_csat(
    request: Request,
    window_seconds: int = Query(default=30 * 24 * 3600, ge=1, le=MAX_WINDOW_SECONDS),
) -> Any:
    ctx = get_context(request)
    if ctx is None:
        return error_response("UNAUTHENTICATED", "no tenant context", status_code=401)

    denied = require_policy(ctx, Action.AUDIT_READ)
    if denied is not None:
        return denied

    async with tenant_session(ctx) as session:
        summary = await csat.csat_summary(
            session, tenant_id=ctx.tenant_id, window_seconds=window_seconds
        )
        rate = await csat.response_rate(
            session, tenant_id=ctx.tenant_id, window_seconds=window_seconds
        )

    return {
        "csat": {
            "responses": summary.responses,
            "average": summary.average,
            # Keys are ints; JSON turns them into strings, which is fine for a
            # chart and is stated here so nobody "fixes" it into a list.
            "distribution": {str(k): v for k, v in sorted(summary.distribution.items())},
            "response_rate": rate,
            "window_seconds": window_seconds,
        }
    }
