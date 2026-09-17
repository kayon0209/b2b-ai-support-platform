"""Prometheus scrape endpoint.

Placement and authorization
---------------------------
`/metrics` is inherently an infrastructure endpoint: Prometheus scrapes it
on a timer with no bearer token, from inside the cluster. Two ways to make
that safe, and this module takes the second:

1. **Authenticate the scrape.** Requires every scraper to carry a token,
   which means a credential in Prometheus config and a rotation path for
   it. It also breaks the common sidecar/service-mesh scraping patterns.
2. **Make the payload non-sensitive by construction, and let the operator
   decide exposure.** This is what we do:
   - Every metric in `observability_metrics` is labelled with code-owned
     vocabularies only. There is no `tenant_id`, `user_id`,
     `conversation_id` or `document_id` label anywhere, so the payload
     cannot answer "how is tenant X doing".
   - `AGGREGATE_ONLY_METRICS_NOTE` documents that invariant at the point a
     reader would look for it.
   The residual exposure is business volume (how many runs happened), which
   is why the endpoint is additionally gated by `APP_METRICS_ENABLED`
   and defaults to local/test only. In staging and production an operator
   must opt in deliberately, having decided whether that footprint is
   acceptable on their network.

`APP_METRICS_ENABLED` is therefore deliberately *not* a security
mechanism - it is an exposure decision. The security mechanism is the
absence of identifying labels.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from observability_metrics import render_metrics
from platform_core.config import get_settings

router = APIRouter(tags=["observability"])

AGGREGATE_ONLY_METRICS_NOTE = (
    "Metrics are aggregate-only: no tenant, user, conversation or document "
    "identifier is ever a label value (docs/security.md objective 1)."
)

# Prometheus text exposition format, version 0.0.4.
_CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"


def metrics_enabled() -> bool:
    """Whether this deployment exposes /metrics.

    Default keeps it on in local/test (so a developer and CI can scrape) and
    off elsewhere until an operator enables it. Off is the default for an
    unauthenticated endpoint that reveals request volume.
    """
    settings = get_settings()
    explicit = getattr(settings, "metrics_enabled", None)
    if explicit is not None:
        return bool(explicit)
    return settings.environment in ("local", "test")


@router.get("/metrics")
async def metrics(request: Request) -> Response:
    """Render the process registry in Prometheus exposition format.

    Served from the *API process only*. The worker runs its own registry and
    is scraped separately: merging them would double-count every event that
    both sides observe, and a counter that counts twice is worse than one
    that counts once in the wrong place.
    """
    if not metrics_enabled():
        # 404 rather than 403: an endpoint that says "forbidden" confirms it
        # exists, and there is no reason to tell an unauthenticated caller
        # what this deployment does or does not run.
        return JSONResponse(status_code=404, content={"error": {"code": "NOT_FOUND"}})

    body = render_metrics()
    response = Response(content=body, media_type=_CONTENT_TYPE)
    # Prometheus ignores the app's JSON envelope; keep any proxy from
    # caching the sample set, which would freeze a dashboard at a moment in
    # time while looking live.
    response.headers["Cache-Control"] = "no-store"
    return response
