"""Tenant-scoped connector capability projection for read-only planning UI."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from platform_core.integrations.models import Connector, ConnectorStatus


async def active_connector_capabilities(
    session: AsyncSession, *, tenant_id: uuid.UUID
) -> frozenset[str]:
    """Return bounded capability names declared by active tenant connectors.

    This is planning evidence only. The Tool Gateway still re-resolves the
    connector, credentials, authorization, timeout, and provider result before
    every execution.
    """
    rows = (
        await session.execute(
            select(Connector.capabilities).where(
                Connector.tenant_id == tenant_id,
                Connector.status == ConnectorStatus.ACTIVE.value,
            )
        )
    ).scalars()
    return frozenset(
        capability
        for declared in rows
        for capability in declared
        if isinstance(capability, str) and capability
    )


__all__ = ["active_connector_capabilities"]
