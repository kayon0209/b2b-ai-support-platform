"""FastAPI application entrypoint.

Exposes /healthz and the support-bridge webhook; routers are attached per
milestone.
"""

import sys

if sys.platform == "win32":
    # psycopg async requires a selector event loop; Windows defaults to
    # Proactor, which breaks every async DB path at runtime.
    import asyncio

    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from fastapi import FastAPI

from platform_core import db
from platform_core.audit.router import router as audit_router
from platform_core.config import Settings, get_settings
from platform_core.identity.middleware import (
    TenantContextMiddleware,
    bootstrap_token_resolver,
)
from platform_core.support_bridge.router import router as support_bridge_router

app = FastAPI(title="B2B AI Support Platform", version="0.1.0")
app.include_router(support_bridge_router)
app.include_router(audit_router)
app.add_middleware(TenantContextMiddleware, resolver=bootstrap_token_resolver)


@app.get("/healthz")
def healthz() -> dict[str, str]:
    settings: Settings = get_settings()
    return {"status": "ok", "environment": settings.environment}


@app.on_event("shutdown")
async def shutdown() -> None:
    await db.dispose_engine()
