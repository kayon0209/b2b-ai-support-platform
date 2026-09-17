"""Membership resolution repository.

Tenant resolution is server-side only: slug/token -> DB lookup -> TenantContext.
No code path may construct TenantContext from request payloads.

Ordering matters here. `tenants` is global reference data and is not under
RLS, so the slug lookup works with no context bound. `memberships` IS under
FORCE RLS, so a membership row is only visible inside a transaction that has
already bound `app.tenant_id` to a tenant id. Reading memberships first would
match the policy predicate against NULL and return zero rows, which is
indistinguishable from "no such membership".

When the tenant id is already known (token names a real id, resource mapping,
connector config) the two-step path below is used. When only the slug is known
- the bootstrap-token case - the lookup goes through
`resolve_active_membership`, a SECURITY DEFINER function scoped to one
(slug, user_id) equality probe. It is read-only, returns at most one row, and
grants EXECUTE but not SELECT, so it cannot be used to read the table.
"""

import uuid
from dataclasses import dataclass

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from platform_core.identity.models import Membership, MembershipRole, Tenant, TenantStatus
from platform_core.identity.tenant_context import (
    TenantContext,
    TenantContextError,
    apply_rls_tenant,
)

BOOTSTRAP_LOOKUP = text(
    "SELECT tenant_id, user_id, role, status FROM resolve_active_membership(:slug, :user_id)"
)


@dataclass(frozen=True)
class ResolvedIdentity:
    """Outcome of an auth lookup, with the tenant id resolved server-side."""

    tenant_id: uuid.UUID
    user_id: uuid.UUID
    role: MembershipRole


async def resolve_by_slug(
    session: AsyncSession, tenant_slug: str, user_id: uuid.UUID
) -> tuple[Tenant, Membership]:
    """Two-step lookup: slug -> tenant (global), then membership under RLS.

    This form hands back the ORM rows so callers can read tenant settings
    (timezone, data region) alongside the role.
    """
    # Step 1: global reference data, no RLS context needed.
    tenant = (
        await session.execute(select(Tenant).where(Tenant.slug == tenant_slug))
    ).scalar_one_or_none()
    if tenant is None or tenant.status != TenantStatus.ACTIVE:
        raise TenantContextError("tenant not found or inactive")

    # Step 2: bind the discovered tenant so the RLS policy on `memberships`
    # can match. This is the only place context is set before it is known,
    # and the value comes from the tenants table - never from the request.
    await apply_rls_tenant(
        session,
        TenantContext(tenant_id=tenant.id, actor_id=None, actor_kind="system"),
    )

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


async def resolve_identity(
    session: AsyncSession, tenant_slug: str, user_id: uuid.UUID
) -> ResolvedIdentity:
    """Single-probe auth lookup via the SECURITY DEFINER bootstrap function.

    Preferred over `resolve_by_slug` when the caller only needs the identity:
    one round trip instead of two, no RLS binding at all, and nothing that
    could accidentally widen visibility - the function returns the single
    membership row matching both the slug and the user, or nothing.
    """
    row = (
        await session.execute(BOOTSTRAP_LOOKUP, {"slug": tenant_slug, "user_id": user_id})
    ).one_or_none()
    if row is None:
        # Deliberately one message for every failure mode: unknown slug,
        # suspended tenant, missing membership and inactive membership are
        # indistinguishable to an unauthenticated caller. Splitting them
        # would turn login into a tenant/user enumeration oracle.
        raise TenantContextError("identity not found or inactive")

    tenant_id, resolved_user_id, role, status = row
    if status != "active":
        raise TenantContextError("identity not found or inactive")

    return ResolvedIdentity(
        tenant_id=tenant_id,
        user_id=resolved_user_id,
        role=MembershipRole(role),
    )


async def load_context(
    session: AsyncSession, tenant_slug: str, user_id: uuid.UUID
) -> TenantContext:
    """Build the request TenantContext from a resolved identity."""
    identity = await resolve_identity(session, tenant_slug, user_id)
    return TenantContext(
        tenant_id=identity.tenant_id,
        actor_id=identity.user_id,
        actor_kind="user",
        role=identity.role.value,
    )
