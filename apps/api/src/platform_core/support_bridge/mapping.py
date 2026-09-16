"""Tenant resolution from Chatwoot connector configuration (ticket 4).

Chatwoot webhooks carry a Chatwoot account ID in the payload, NOT our
tenant ID. Resolution path (docs/api-contracts.md):
  webhook -> chatwoot_account_id -> ExternalResourceRef/connector config
          -> tenant_id (server-side, never client-supplied)
"""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from platform_core.support_bridge.models import ExternalResourceRef


async def resolve_tenant_from_account(
    session: AsyncSession, chatwoot_account_id: str
) -> uuid.UUID | None:
    """Look up the tenant that registered this Chatwoot account.

    The mapping row is written by tenant onboarding (connector setup), so
    the tenant_id it carries is trusted configuration, not client input.
    """
    stmt = select(ExternalResourceRef.tenant_id).where(
        ExternalResourceRef.system == "chatwoot",
        ExternalResourceRef.resource_type == "account",
        ExternalResourceRef.external_id == str(chatwoot_account_id),
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def register_account_mapping(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    chatwoot_account_id: str,
    external_url: str | None = None,
) -> ExternalResourceRef:
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    stmt = (
        pg_insert(ExternalResourceRef)
        .values(
            tenant_id=tenant_id,
            system="chatwoot",
            resource_type="account",
            external_id=str(chatwoot_account_id),
            external_url=external_url,
            last_synced_at=None,
        )
        .on_conflict_do_update(constraint="uq_external_ref", set_={"external_url": external_url})
        .returning(ExternalResourceRef)
    )
    return (await session.execute(stmt)).scalar_one()


async def upsert_conversation_mapping(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    chatwoot_conversation_id: str,
    source_version: str | None = None,
) -> ExternalResourceRef:
    import time

    from sqlalchemy.dialects.postgresql import insert as pg_insert

    stmt = (
        pg_insert(ExternalResourceRef)
        .values(
            tenant_id=tenant_id,
            system="chatwoot",
            resource_type="conversation",
            external_id=str(chatwoot_conversation_id),
            source_version=source_version,
            last_synced_at=int(time.time()),
        )
        .on_conflict_do_update(
            constraint="uq_external_ref",
            set_={"source_version": source_version, "last_synced_at": int(time.time())},
        )
        .returning(ExternalResourceRef)
    )
    return (await session.execute(stmt)).scalar_one()
