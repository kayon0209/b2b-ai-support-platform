"""Custom-domain administration, and the public branding page they serve.

    GET    /v1/tenant/domains              this tenant's domains and their state
    POST   /v1/tenant/domains              claim a host (starts unverified)
    POST   /v1/tenant/domains/{id}/verify  attest that the claim is proven
    DELETE /v1/tenant/domains/{id}         release a host

    GET    /v1/public/branding             the branding for the request's Host

Why the public endpoint exists, and what bounds it
--------------------------------------------------
`Host` is caller-controlled, so it may never select a tenant for an
*authenticated* request - that would be "tenant id from the request", which
this platform forbids at every layer. What it is allowed to do is narrower:
pick which tenant's **public** branding to render. Everything on that page is
already public (a name, a logo, a colour, a contact address), the endpoint
returns no tenant id, no configuration and no counts, and it is the only place
`resolve_tenant_for_host` is called. No API route takes a tenant from it.

Unverified domains do not resolve - see `domains.py`. A 404 is returned for an
unknown host, an unverified claim and a suspended tenant alike, because
distinguishing them would turn the endpoint into a way to enumerate claims.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from platform_core.api import (
    error_response,
    get_context,
    new_trace_id,
    ok_response,
    require_policy,
    require_write_idempotency,
    tenant_session,
)
from platform_core.db import session_scope
from platform_core.identity import domains as domain_service
from platform_core.identity.models import Tenant, TenantDomain
from platform_core.identity.tenant_context import TenantContext
from platform_policy import Action

router = APIRouter(prefix="/v1/tenant/domains", tags=["tenant"])
public_router = APIRouter(prefix="/v1/public", tags=["public"])

DOMAIN_NOT_FOUND = "DOMAIN_NOT_FOUND"
# Returned for an unknown host, an unverified claim and a suspended tenant
# alike, so the endpoint cannot be used to enumerate claims.
UNKNOWN_HOST = "UNKNOWN_HOST"


class DomainIn(BaseModel):
    domain: str = Field(min_length=1, max_length=255)


def _unresolved() -> Any:
    return error_response("AUTH_UNRESOLVED", "tenant context not resolved", status_code=401)


def _out(row: TenantDomain) -> dict[str, Any]:
    """Public shape of a claim.

    `verification_token` is included: it is what the tenant publishes as a DNS
    TXT record, so withholding it would make verification impossible. It is not
    an authentication secret - holding it proves nothing on its own, an
    operator still attests.
    """
    return {
        "id": str(row.id),
        "domain": row.domain,
        "verified": row.verified_at is not None,
        "verified_at": row.verified_at,
        "verification_token": row.verification_token,
        "created_at": row.created_at,
    }


async def _load(
    session: AsyncSession, domain_id: uuid.UUID, ctx: TenantContext
) -> TenantDomain | None:
    """Load a domain belonging to the caller's tenant.

    The `tenant_id` predicate is redundant with RLS and stays anyway: RLS
    returns zero rows for a foreign domain, and a query that *looks*
    tenant-scoped is the difference between a reviewer confirming isolation and
    a reviewer having to prove it.
    """
    return (
        await session.execute(
            select(TenantDomain).where(
                TenantDomain.id == domain_id, TenantDomain.tenant_id == ctx.tenant_id
            )
        )
    ).scalar_one_or_none()


@router.get("")
async def list_domains(request: Request) -> Any:
    ctx = get_context(request)
    if ctx is None:
        return _unresolved()
    denied = require_policy(ctx, Action.TENANT_ADMIN)
    if denied is not None:
        return denied

    async with tenant_session(ctx) as session:
        rows = await domain_service.list_domains(session, tenant_id=ctx.tenant_id)
        return ok_response({"domains": [_out(r) for r in rows]})


@router.post("")
async def claim_domain(request: Request, body: DomainIn) -> Any:
    ctx = get_context(request)
    if ctx is None:
        return _unresolved()
    denied = require_policy(ctx, Action.TENANT_ADMIN)
    if denied is not None:
        return denied
    missing_idem = require_write_idempotency(request, Action.TENANT_ADMIN)
    if missing_idem is not None:
        return missing_idem

    trace_id = new_trace_id()
    async with tenant_session(ctx) as session:
        try:
            row = await domain_service.claim_domain(
                session, ctx=ctx, raw_domain=body.domain, trace_id=trace_id
            )
        except domain_service.DomainError as exc:
            status = 409 if exc.code.startswith("DOMAIN_ALREADY") else 400
            if exc.code == "DOMAIN_UNAVAILABLE":
                status = 409
            return error_response(
                exc.code,
                "the domain is already claimed by this tenant"
                if exc.code == "DOMAIN_ALREADY_CLAIMED"
                else "the domain cannot be claimed",
                status_code=status,
            )
        await session.commit()
        return ok_response({"domain": _out(row)}, trace_id=trace_id)


@router.post("/{domain_id}/verify")
async def verify_domain(request: Request, domain_id: uuid.UUID) -> Any:
    """Attest that the claim is proven.

    An attestation, not an automated DNS check: reading a TXT record needs a
    resolver dependency and its own failure-mode decisions, which this
    repository does not have. The endpoint says so rather than implying the
    check happened.
    """
    ctx = get_context(request)
    if ctx is None:
        return _unresolved()
    denied = require_policy(ctx, Action.TENANT_ADMIN)
    if denied is not None:
        return denied
    missing_idem = require_write_idempotency(request, Action.TENANT_ADMIN)
    if missing_idem is not None:
        return missing_idem

    trace_id = new_trace_id()
    async with tenant_session(ctx) as session:
        row = await _load(session, domain_id, ctx)
        if row is None:
            return error_response(DOMAIN_NOT_FOUND, "domain not found", status_code=404)

        changed = await domain_service.verify_domain(session, row, ctx=ctx, trace_id=trace_id)
        await session.commit()
        return ok_response({"domain": _out(row), "changed": changed}, trace_id=trace_id)


@router.delete("/{domain_id}")
async def remove_domain(request: Request, domain_id: uuid.UUID) -> Any:
    ctx = get_context(request)
    if ctx is None:
        return _unresolved()
    denied = require_policy(ctx, Action.TENANT_ADMIN)
    if denied is not None:
        return denied
    missing_idem = require_write_idempotency(request, Action.TENANT_ADMIN)
    if missing_idem is not None:
        return missing_idem

    trace_id = new_trace_id()
    async with tenant_session(ctx) as session:
        row = await _load(session, domain_id, ctx)
        if row is None:
            return error_response(DOMAIN_NOT_FOUND, "domain not found", status_code=404)
        await domain_service.remove_domain(session, row, ctx=ctx, trace_id=trace_id)
        await session.commit()
        return ok_response({"released": True}, trace_id=trace_id)


@public_router.get("/branding")
async def public_branding(request: Request) -> Any:
    """Branding for the tenant that owns this Host, or 404.

    Unauthenticated by design: this is the tenant's public page. It runs before
    a tenant is bound, so it reads through the resolver function and then
    fetches the tenant row - which is global reference data, not RLS-scoped.

    Returns branding only. No tenant id: the caller is anonymous, and an id
    would be a stable handle for probing the API.
    """
    host = request.headers.get("host") or ""
    if not host:
        return error_response(UNKNOWN_HOST, "no host", status_code=404)

    async with session_scope() as session:
        resolution = await domain_service.resolve_tenant_for_host(session, host)
        if resolution is None:
            return error_response(UNKNOWN_HOST, "unknown host", status_code=404)

        tenant = (
            await session.execute(select(Tenant).where(Tenant.id == resolution.tenant_id))
        ).scalar_one_or_none()
        if tenant is None:  # pragma: no cover - the resolver joins tenants
            return error_response(UNKNOWN_HOST, "unknown host", status_code=404)

        return ok_response(
            {
                "branding": {
                    "display_name": tenant.brand_display_name or tenant.name,
                    "logo_url": tenant.brand_logo_url,
                    "primary_color": tenant.brand_primary_color,
                    "support_email": tenant.support_email,
                }
            }
        )


__all__ = ["public_router", "router"]
