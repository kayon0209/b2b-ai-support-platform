"""FastAPI application entrypoint.

Exposes /healthz plus one router per bounded context:

- support_bridge: Chatwoot webhook intake (persist-before-enqueue)
- audit:          append-only audit read API
- cases:          case lifecycle and the command API
- retrieval:      authorized knowledge search (diagnostics, evaluations)
- agent_runtime:  queue an agent run for a conversation
- tool_gateway:   propose / confirm / execute business writes

`agent_runtime` and `tool_gateway` are registered here but their endpoints
carry their own policy gates (see each router), so mounting them does not
widen any principal's access.
"""

import sys

if sys.platform == "win32":
    # psycopg async requires a selector event loop; Windows defaults to
    # Proactor, which breaks every async DB path at runtime.
    import asyncio

    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from platform_core import db
from platform_core.agent_runtime.prompt_router import router as prompt_router
from platform_core.agent_runtime.router import router as agent_runtime_router
from platform_core.audit.router import router as audit_router
from platform_core.cases.router import router as cases_router
from platform_core.compliance.router import router as compliance_router
from platform_core.config import Settings, get_settings
from platform_core.evaluation.router import router as quality_router
from platform_core.http_metrics import HttpMetricsMiddleware
from platform_core.identity.branding import router as tenant_branding_router
from platform_core.identity.domain_router import (
    public_router as public_branding_router,
)
from platform_core.identity.domain_router import (
    router as tenant_domains_router,
)
from platform_core.identity.middleware import TenantContextMiddleware, build_resolver
from platform_core.identity.org_router import router as org_router
from platform_core.identity.router import router as identity_router
from platform_core.identity.saml_router import router as saml_router
from platform_core.identity.scim_router import router as scim_router
from platform_core.identity.usage import router as tenant_usage_router
from platform_core.integrations.router import (
    dead_letter_router,
)
from platform_core.integrations.router import (
    router as connectors_router,
)
from platform_core.integrations.webhook_router import router as connector_webhook_router
from platform_core.knowledge.flag_router import router as feature_flag_router
from platform_core.knowledge.gap_router import router as knowledge_gap_router
from platform_core.knowledge.router import router as knowledge_router
from platform_core.observability_router import router as observability_router
from platform_core.rate_limit import (
    RateLimitMiddleware,
    build_limiter,
    policies_from_settings,
)
from platform_core.retrieval.router import router as retrieval_router
from platform_core.support_bridge.router import router as support_bridge_router
from platform_core.tool_gateway.router import router as tool_gateway_router


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Release the connection pool on shutdown.

    Replaces `@app.on_event("shutdown")`, which Starlette deprecated in favour
    of lifespan handlers. Behaviour is unchanged - the engine is disposed once,
    after the server stops accepting requests - but the deprecation warning is
    gone, and lifespan runs under every server (uvicorn, TestClient, ASGI
    runners) rather than only the ones that emit the legacy events.
    """
    yield
    await db.dispose_engine()


app = FastAPI(title="B2B AI Support Platform", version="0.1.0", lifespan=lifespan)
app.include_router(support_bridge_router)
app.include_router(audit_router)
app.include_router(compliance_router)
app.include_router(cases_router)
app.include_router(retrieval_router)
app.include_router(agent_runtime_router)
app.include_router(tool_gateway_router)
app.include_router(quality_router)
app.include_router(prompt_router)
app.include_router(knowledge_gap_router)
app.include_router(knowledge_router)
app.include_router(feature_flag_router)
# Observability last: /metrics is an unauthenticated infrastructure endpoint
# and its own guard is inside the router (see observability_router).
app.include_router(identity_router)
# Public by necessity: a browser arriving from an IdP has no bearer token.
app.include_router(saml_router)
# Authenticated by its own bearer token, not by a user's credential.
app.include_router(scim_router)
app.include_router(org_router)
app.include_router(tenant_branding_router)
app.include_router(tenant_usage_router)
app.include_router(tenant_domains_router)
# Host-resolved and unauthenticated: a tenant's public branding page.
app.include_router(public_branding_router)
app.include_router(connectors_router)
app.include_router(dead_letter_router)
# Provider-signed inbound webhooks. Authenticated by HMAC, not by a bearer
# token, so the path is in the auth middleware's exempt list.
app.include_router(connector_webhook_router)
app.include_router(observability_router)
# Middleware runs in reverse registration order, so the outermost is the one
# added last. The intended request order is:
#
#   HttpMetrics -> TenantContext -> RateLimit -> routing
#
# RateLimit is inside TenantContext because a tenant-keyed bucket is what
# stops one noisy tenant from consuming another's budget; it cannot key on a
# tenant that has not been resolved yet. HttpMetrics stays outermost so that a
# 429 still appears in the request rate, and so does a 401 - registering them
# the other way round would hide authentication failures from the metric that
# is supposed to surface them.
if get_settings().rate_limit_enabled:
    _policies = policies_from_settings(get_settings())
    app.add_middleware(
        RateLimitMiddleware,
        limiter=build_limiter(redis_url=get_settings().redis_url),
        api_policy=_policies["api"],
        anonymous_policy=_policies["anonymous"],
        webhook_policy=_policies["webhook"],
    )
app.add_middleware(TenantContextMiddleware, resolver=build_resolver())
app.add_middleware(HttpMetricsMiddleware)


@app.get("/healthz")
def healthz() -> dict[str, str]:
    settings: Settings = get_settings()
    return {"status": "ok", "environment": settings.environment}


def run() -> None:
    """Launch the API with a psycopg-compatible event loop.

    `uvicorn platform_core.main:app` cannot be used on Windows: uvicorn's
    asyncio loop factory hardcodes `ProactorEventLoop` there, and psycopg
    async refuses to run on it - every DB-backed request would fail. Passing
    an explicit loop factory is the only reliable fix, because uvicorn builds
    its loop before the app is imported, so nothing the app sets at import
    time can influence it.
    """
    import os

    import uvicorn

    # uvicorn's `loop` takes a LoopFactoryType string or a dotted import path
    # ("module:attr") for a custom loop factory. A dotted string keeps the
    # declared type honest; passing the class object also happens to work only
    # because uvicorn's importer passes non-strings through unchanged, which is
    # an undocumented accident we should not depend on.
    loop_factory = (
        "asyncio:SelectorEventLoop" if sys.platform == "win32" else "asyncio:new_event_loop"
    )
    # Bind address is configurable because "all interfaces" is right inside a
    # container behind a service mesh and wrong on a developer laptop: on the
    # latter it exposes the API to the local network.
    host = os.environ.get("APP_API_HOST", "0.0.0.0")  # noqa: S104
    port = int(os.environ.get("APP_API_PORT", "8000"))
    uvicorn.run(
        app,
        host=host,
        port=port,
        log_level="info",
        loop=loop_factory,
    )


if __name__ == "__main__":
    run()
