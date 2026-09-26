"""Serving the product's own interface from the API.

Why the API, and not a separate frontend deployment
---------------------------------------------------
Three options existed: a static host, an nginx sidecar, or the API. The
last one was chosen deliberately. It adds **no new infrastructure component** -
`AGENTS.md` forbids that without a benchmark and an ADR - and it collapses one
certificate, one origin, one CORS policy and one ingress rule into the ones
that already exist. The cost is that a large static file is served by a Python
process; at this product's size the shell is a few hundred kilobytes and the
real cost was never the serving, it was that the product had no way to ship its
own interface at all: `GET /support` answered 401 and neither the Kubernetes
manifests nor the Compose file carried a frontend workload.

What is mounted
---------------
`/assets` is served as real files, and everything else under the browser
router's prefixes returns `index.html`. The distinction matters: a missing
hashed asset must be a 404, because serving HTML in its place produces a MIME
error in the browser that is far harder to diagnose than a clean miss. The
shell is only the fallback for paths the *client* router owns.

The two things that could go wrong, and how they are prevented
-------------------------------------------------------------
1. **The catch-all swallows the API.** An unknown `/v1/*` path must not come
   back as HTML with a 200, or a typo in a client URL surfaces as a JSON parse
   error somewhere unrelated. `_is_api_path` refuses those explicitly, and the
   route is registered after every API router so real routes always win first.
2. **Exempting the shell from authentication grants something.** It grants
   nothing: the shell is a static JavaScript bundle and every datum in it comes
   from an authenticated `/v1/*` call. The exemption exists only so the browser
   can *fetch* the bundle before it has a token - the same reason the SAML
   endpoints are exempt, and with the same obligation attached: whatever lives
   under these prefixes must authenticate itself.

And the case that decides whether this is safe to ship: a checkout with no
`npm run build` is the normal state of a backend test run. When the directory
is absent, nothing is mounted and the exemption does not apply, so `/support`
keeps answering exactly what it answered before - 401 - instead of a blank
page that looks like a broken product.
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from starlette.staticfiles import StaticFiles

logger = logging.getLogger("platform.spa")

# Where `api.Dockerfile` leaves the build, and the default a local run falls
# back to. Both are overridable so a test can point at a fixture without
# building anything.
DEFAULT_SPA_DIST = "/app/apps/admin-web/dist"

# The prefixes the browser router owns, plus the asset mount it references.
# Exact-match or slash-prefix, never a bare `startswith`: "/supportXYZ" is not
# a support route, and exempting it would hand a prefix-matching decision to a
# typo.
SPA_PREFIXES = ("/support", "/admin", "/auth", "/assets")

# The landing page. Exact, because "/" as a *prefix* would match every path in
# the application and exempt the entire API from bearer resolution.
SPA_EXACT_PATHS = frozenset({"/"})

# Prefixes and exact paths that must never be answered with the shell, even
# though the catch-all sees them. The API has its own authentication, error
# shape and content negotiation; the shell would flatten all three.
API_PATH_PREFIXES = ("/v1/", "/scim/", "/assets/")
API_EXACT_PATHS = frozenset(
    {"/healthz", "/metrics", "/openapi.json", "/docs", "/redoc", "/favicon.ico"}
)


def is_spa_route(path: str) -> bool:
    """Whether the browser router owns this path."""
    if path in SPA_EXACT_PATHS:
        return True
    return any(path == prefix or path.startswith(f"{prefix}/") for prefix in SPA_PREFIXES)


def _is_api_path(path: str) -> bool:
    return path in API_EXACT_PATHS or path.startswith(API_PATH_PREFIXES)


def resolve_dist(configured: str | None) -> Path | None:
    """The directory to serve, or None when there is nothing to serve.

    An absent directory is a legitimate state, not an error: it is what a
    backend-only checkout looks like. It is logged once, by name, because the
    alternative - a blank page with no explanation - is the failure this whole
    change exists to remove.
    """
    dist = Path(configured or DEFAULT_SPA_DIST)
    if (dist / "index.html").is_file():
        return dist
    logger.warning(
        "spa_not_served: no built frontend at %s; /support and /admin keep "
        "answering as they did before. Build it with `npm run build` in "
        "apps/admin-web, or use the image, which does it during the build.",
        dist,
    )
    return None


def mount_spa(app: FastAPI, configured: str | None) -> Path | None:
    """Mount the built frontend's assets. Returns the directory served.

    Only the assets are mounted here. The catch-all is registered separately
    by `register_spa_fallback`, at the very end of the application's route
    table - a catch-all registered too early wins paths that a real route
    defined further down would have matched, and `/healthz` is exactly that
    case: it is defined after the middleware block, so a combined mount put
    the shell in front of it and health checks started answering 404.
    """
    dist = resolve_dist(configured)
    if dist is None:
        return None

    assets = dist / "assets"
    if assets.is_dir():
        # Hashed filenames, so they can be cached hard. `StaticFiles` answers a
        # missing file with a plain 404, which is exactly what a stale cached
        # index.html needs to see.
        app.mount("/assets", StaticFiles(directory=assets), name="spa-assets")
    return dist


def register_spa_fallback(app: FastAPI, dist: Path | None) -> None:
    """Add the catch-all. Call this after every real route is registered."""
    if dist is None:
        return
    index = dist / "index.html"

    @app.get("/{spa_path:path}", include_in_schema=False)
    async def spa_fallback(spa_path: str, request: Request):  # type: ignore[no-untyped-def]
        if _is_api_path(f"/{spa_path}"):
            # Reached only when no API route matched, because those are all
            # registered before this. Answering JSON keeps a client-side typo a
            # client-side error.
            return JSONResponse({"error": {"code": "NOT_FOUND"}}, status_code=404)
        return FileResponse(index, media_type="text/html")
