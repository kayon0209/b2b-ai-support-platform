"""Tenant branding API (Phase 5: custom domains and branding).

    GET /v1/tenant/branding    the caller's tenant branding
    PUT /v1/tenant/branding    replace it (tenant_owner, security_admin)

Authorization:
- reading branding needs only an authenticated member: it is public-facing
  configuration (a name, a logo, a colour, a contact address), not operational
  telemetry, and every member sees it in the product UI.
- writing it requires TENANT_ADMIN and an Idempotency-Key, and is audited.

Both routes operate on the caller's *server-resolved* tenant. There is no
route that reads or writes branding for a tenant id supplied by the client,
which would be a cross-tenant configuration oracle.
"""

import re
from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field
from sqlalchemy import select

from platform_core.api import (
    VALIDATION_FAILED,
    error_response,
    get_context,
    new_trace_id,
    ok_response,
    require_policy,
    require_write_idempotency,
    tenant_session,
)
from platform_core.audit import service as audit_service
from platform_core.identity.models import Tenant
from platform_policy import Action

router = APIRouter(prefix="/v1/tenant", tags=["tenant"])

# #abc or #aabbcc. Anchored, so "javascript:..." cannot pass as a colour.
_HEX_COLOR = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_ALLOWED_URL_SCHEMES = ("https://", "http://")
# A display name is a label. `&`, quotes and any script or language are fine -
# only angle brackets and control characters are refused, so "Acme & Co" and
# "华秋电子" keep working.
_MARKUP = re.compile(r"[<>]")
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


class BrandingIn(BaseModel):
    display_name: str | None = Field(default=None, max_length=255)
    logo_url: str | None = Field(default=None, max_length=1024)
    primary_color: str | None = Field(default=None, max_length=31)
    support_email: str | None = Field(default=None, max_length=255)


def _out(tenant: Tenant) -> dict[str, Any]:
    return {
        "tenant_id": str(tenant.id),
        "slug": tenant.slug,
        "display_name": tenant.brand_display_name,
        "logo_url": tenant.brand_logo_url,
        "primary_color": tenant.brand_primary_color,
        "support_email": tenant.support_email,
    }


def _validate(body: BrandingIn) -> str:
    """Return a human-readable problem, or "" when valid.

    Validated here rather than only in the UI: the admin web renders
    `logo_url` into an `<img src>` and `primary_color` into a style, so a
    `javascript:` URL or a CSS-injection payload must be refused at the API.
    """
    if body.primary_color is not None and not _HEX_COLOR.match(body.primary_color):
        return "primary_color must be a hex colour like #1a2b3c"
    if body.logo_url is not None and not body.logo_url.startswith(_ALLOWED_URL_SCHEMES):
        return "logo_url must be an http(s) URL"
    if body.support_email is not None and not _EMAIL.match(body.support_email):
        return "support_email must be an email address"
    if body.display_name is not None:
        # The one field of the four that had no rule, and a display name is a
        # label - it never needs markup. Today every render of it goes through
        # React, which escapes it, so this is not an exploitable XSS here; but
        # the same value would be executed by any consumer that renders HTML
        # (an email, a PDF, a Chatwoot inbox name), and a name that *is* a
        # script tag is a defect regardless of who escapes it. A tenant really
        # did store `<img src=x onerror=alert(2)>` as its name.
        if not body.display_name.strip():
            return "display_name must not be blank"
        if _MARKUP.search(body.display_name):
            return "display_name must not contain markup characters (< or >)"
        if _CONTROL_CHARS.search(body.display_name):
            return "display_name must not contain control characters"
    return ""


def _unresolved() -> Any:
    return error_response("AUTH_UNRESOLVED", "tenant context not resolved", status_code=401)


@router.get("/branding")
async def get_branding(request: Request) -> Any:
    ctx = get_context(request)
    if ctx is None:
        return _unresolved()

    async with tenant_session(ctx) as session:
        tenant = (
            await session.execute(select(Tenant).where(Tenant.id == ctx.tenant_id))
        ).scalar_one_or_none()
        if tenant is None:
            return error_response("TENANT_NOT_FOUND", "tenant not found", status_code=404)
        return ok_response({"branding": _out(tenant)})


@router.put("/branding")
async def update_branding(request: Request, body: BrandingIn) -> Any:
    ctx = get_context(request)
    if ctx is None:
        return _unresolved()

    denied = require_policy(ctx, Action.TENANT_ADMIN)
    if denied is not None:
        return denied
    missing_idem = require_write_idempotency(request, Action.TENANT_ADMIN)
    if missing_idem is not None:
        return missing_idem

    problem = _validate(body)
    if problem:
        return error_response(VALIDATION_FAILED, problem, status_code=400)

    trace_id = new_trace_id()
    async with tenant_session(ctx) as session:
        tenant = (
            await session.execute(select(Tenant).where(Tenant.id == ctx.tenant_id))
        ).scalar_one_or_none()
        if tenant is None:
            return error_response("TENANT_NOT_FOUND", "tenant not found", status_code=404)

        before = _out(tenant)
        # PUT replaces the whole branding: an omitted field clears it, which is
        # how a tenant removes a logo it no longer wants.
        tenant.brand_display_name = body.display_name
        tenant.brand_logo_url = body.logo_url
        tenant.brand_primary_color = body.primary_color
        tenant.support_email = body.support_email

        await audit_service.record(
            session,
            ctx=ctx,
            action="tenant.branding.updated",
            resource_type="tenant",
            resource_id=tenant.id,
            before=before,
            after=_out(tenant),
            trace_id=trace_id,
        )
        await session.commit()
        result = _out(tenant)

    return ok_response({"branding": result}, trace_id=trace_id)


__all__ = ["router"]
