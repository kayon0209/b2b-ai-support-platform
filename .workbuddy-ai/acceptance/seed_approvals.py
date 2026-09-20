"""Register the platform tool catalog for a tenant, so the Approvals screen
has something to list.

`ensure_tool_definitions` is called by the orchestrator on its write path, so a
tenant that has never had a write run has no definitions and
`POST /v1/tool-proposals` answers TOOL_NOT_REGISTERED. This is the acceptance
harness's way of getting past that without driving a whole conversation.

Usage: python seed_approvals.py <tenant-uuid>
"""

from __future__ import annotations

import asyncio
import sys
import uuid

from platform_core.db import app_role_url, session_scope_with_url
from platform_core.identity.tenant_context import TenantContext, apply_rls_tenant
from platform_core.tool_gateway.registry import ensure_tool_definitions


async def main(tenant_id: uuid.UUID) -> None:
    async with session_scope_with_url(app_role_url()) as session:
        ctx = TenantContext(tenant_id=tenant_id, actor_id=None, actor_kind="system")
        await apply_rls_tenant(session, ctx)
        created = await ensure_tool_definitions(session, tenant_id=tenant_id)
    print(f"tool definitions created: {created}")


if __name__ == "__main__":
    # SelectorEventLoop on Windows: psycopg async refuses a Proactor loop.
    asyncio.run(main(uuid.UUID(sys.argv[1])), loop_factory=asyncio.SelectorEventLoop)
