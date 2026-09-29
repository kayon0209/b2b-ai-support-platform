"""Read-only Tool Gateway catalog application queries.

Callers receive immutable projections rather than importing gateway ORM
models. Tenant overrides shadow the global catalog by highest version.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from platform_core.tool_gateway.models import ToolDefinition


@dataclass(frozen=True)
class ToolCatalogEntry:
    name: str
    risk: str
    input_schema: dict[str, Any]
    required_permissions: tuple[str, ...]


async def list_tenant_tools(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
) -> list[ToolCatalogEntry]:
    """Return the effective highest-version tool for each name."""
    rows = (
        await session.execute(
            select(
                ToolDefinition.name,
                ToolDefinition.risk,
                ToolDefinition.input_schema,
                ToolDefinition.required_permissions,
            )
            .where((ToolDefinition.tenant_id == tenant_id) | ToolDefinition.tenant_id.is_(None))
            .order_by(ToolDefinition.name, ToolDefinition.version.desc())
        )
    ).all()
    tools: list[ToolCatalogEntry] = []
    seen: set[str] = set()
    for row in rows:
        if row.name in seen:
            continue
        seen.add(row.name)
        tools.append(
            ToolCatalogEntry(
                name=row.name,
                risk=row.risk,
                input_schema=row.input_schema if isinstance(row.input_schema, dict) else {},
                required_permissions=tuple(row.required_permissions or ()),
            )
        )
    return tools


async def tool_risk(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    tool_name: str,
) -> str | None:
    for tool in await list_tenant_tools(session, tenant_id=tenant_id):
        if tool.name == tool_name:
            return tool.risk
    return None
