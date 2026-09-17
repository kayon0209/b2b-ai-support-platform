"""HTTP request metrics middleware.

Cardinality is the whole problem here. A naive middleware labels on
`request.url.path`, which for this API contains UUIDs - every document, case
and conversation id becomes a distinct label value, and the label map grows
without bound in a long-running process. Two consequences, and the second is
the dangerous one:

1. Memory and scrape size grow with traffic.
2. A scrape starts returning per-resource breakdowns, which is a cross-tenant
   disclosure surface that no reviewer would have signed off on, because it
   arrives by accident rather than by intent.

So we label on the **route template** (`/v1/cases/{case_id}`), which comes
from the matched route rather than the raw URL, and we fall back to a fixed
`unmatched` bucket when no route matched. The fallback matters: a 404 on a
random path is exactly the request an attacker controls, so it must not
become a label value.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

from observability_metrics import get_metrics

# Methods whose metrics we record. HEAD/OPTIONS are skipped: they are
# liveness probes and CORS preflights, and including them makes the request
# rate look several times higher than the traffic the service actually
# serves.
_TRACKED_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE"})

# Bucket for a request that matched no route (404 on an arbitrary path).
UNMATCHED_ROUTE = "unmatched"


def route_template(request: Request) -> str:
    """Return the matched route template, never the concrete path.

    `request.scope["route"]` is populated by Starlette *after* routing, so a
    middleware that runs before routing cannot see it. We read it in the
    `finally` block of `dispatch`, by which point the inner app has resolved
    the route and written it back onto the scope.
    """
    route = request.scope.get("route")
    path = getattr(route, "path", None)
    if isinstance(path, str) and path:
        return path
    return UNMATCHED_ROUTE


class HttpMetricsMiddleware(BaseHTTPMiddleware):
    """Record platform_http_requests_total for every completed request."""

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        method = request.method.upper()
        started = time.monotonic()
        try:
            response = await call_next(request)
        except Exception:
            # The request produced no response at all: still counted, because
            # a 500-rate metric that silently omits the worst failures is
            # actively misleading. Re-raised so error handling is unchanged.
            self._record(method, request, status=500, outcome="exception", started=started)
            raise
        self._record(
            method,
            request,
            status=response.status_code,
            outcome="ok" if response.status_code < 400 else "error",
            started=started,
        )
        return response

    @staticmethod
    def _record(
        method: str, request: Request, *, status: int, outcome: str, started: float
    ) -> None:
        if method not in _TRACKED_METHODS:
            return
        get_metrics().http_requests_total.labels(
            method=method,
            route=route_template(request),
            status=str(status),
            outcome=outcome,
        ).inc()
        # Latency is not recorded per route: the histogram would multiply the
        # bucket count by the route count for a signal that traces already
        # carry at full fidelity.
        _ = time.monotonic() - started
