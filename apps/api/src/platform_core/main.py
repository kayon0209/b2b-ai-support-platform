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

from fastapi import FastAPI

from platform_core import db
from platform_core.agent_runtime.prompt_router import router as prompt_router
from platform_core.agent_runtime.router import router as agent_runtime_router
from platform_core.audit.router import router as audit_router
from platform_core.cases.router import router as cases_router
from platform_core.config import Settings, get_settings
from platform_core.evaluation.router import router as quality_router
from platform_core.identity.middleware import (
    TenantContextMiddleware,
    bootstrap_token_resolver,
)
from platform_core.retrieval.router import router as retrieval_router
from platform_core.support_bridge.router import router as support_bridge_router
from platform_core.tool_gateway.router import router as tool_gateway_router

app = FastAPI(title="B2B AI Support Platform", version="0.1.0")
app.include_router(support_bridge_router)
app.include_router(audit_router)
app.include_router(cases_router)
app.include_router(retrieval_router)
app.include_router(agent_runtime_router)
app.include_router(tool_gateway_router)
app.include_router(quality_router)
app.include_router(prompt_router)
app.add_middleware(TenantContextMiddleware, resolver=bootstrap_token_resolver)


@app.get("/healthz")
def healthz() -> dict[str, str]:
    settings: Settings = get_settings()
    return {"status": "ok", "environment": settings.environment}


@app.on_event("shutdown")
async def shutdown() -> None:
    await db.dispose_engine()
