"""Membership resolution repository.

Tenant resolution is server-side only: slug/token -> DB lookup -> TenantContext.
No code path may construct TenantContext from request payloads.
"""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from platform_core.identity.models import Membership, MembershipRole, Tenant, TenantStatus
from platform_core.identity.tenant_context import TenantContext, TenantContextError


async def resolve_by_slug(
    session: AsyncSession, tenant_slug: str, user_id: uuid.UUID
) -> tuple[Tenant, Membership]:
    tenant = (
        await session.execute(select(Tenant).where(Tenant.slug == tenant_slug))
    ).scalar_one_or_none()
    if tenant is None or tenant.status != TenantStatus.ACTIVE:
        raise TenantContextError("tenant not found or inactive")

    membership = (
        await session.execute(
            select(Membership).where(
                Membership.tenant_id == tenant.id, Membership.user_id == user_id
            )
        )
    ).scalar_one_or_none()
    if membership is None or membership.status != "active":
        raise TenantContextError("membership not found or inactive")

    return tenant, membership


async def load_context(
    session: AsyncSession, tenant_slug: str, user_id: uuid.UUID
) -> TenantContext:
    tenant, membership = await resolve_by_slug(session, tenant_slug, user_id)
    return TenantContext(
        tenant_id=tenant.id,
        actor_id=user_id,
        actor_kind="user",
        role=membership.role.value
        if isinstance(membership.role, MembershipRole)
        else str(membership.role),
    )
