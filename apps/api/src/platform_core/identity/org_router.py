"""Org-structure administration: enterprise accounts and departments.

    GET    /v1/identity/accounts                 this tenant's accounts
    POST   /v1/identity/accounts                 create one
    GET    /v1/identity/accounts/{id}            one account
    PATCH  /v1/identity/accounts/{id}            rename / re-tier / re-parent
    GET    /v1/identity/departments              this tenant's departments
    POST   /v1/identity/departments              create one
    PATCH  /v1/identity/departments/{id}         rename / re-parent

Permissions, and why they are split this way
--------------------------------------------
Reads need `CASE_READ`. An account is reference data a support agent picks when
opening a Case, so every role that can touch a Case can read the list; gating
reads behind an admin action would mean an agent cannot see which account they
are filing against.

Writes need `TENANT_ADMIN`. A contract tier is what an SLA clock is derived
from (`cases.sla_policy_for_tier`), so "may edit the account" is "may shorten
or lengthen a customer's contractual response window". That is a commercial
decision, not an agent one, and it is the same action that already gates
branding and custom domains.

Both `PATCH` bodies distinguish an omitted field from an explicit `null` for
`parent_id`: omitting it leaves the parent alone, sending `null` detaches the
node. A single `parent_id: null` meaning "no change" is how a hierarchy edit
silently does nothing.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from platform_core.api import (
    error_response,
    get_context,
    new_trace_id,
    ok_response,
    require_policy,
    require_write_idempotency,
    tenant_session,
)
from platform_core.identity import org
from platform_core.identity.models import Department, EnterpriseAccount
from platform_policy import Action

router = APIRouter(prefix="/v1/identity", tags=["identity"])

VALIDATION_FAILED = "VALIDATION_FAILED"
ORG_NOT_FOUND = "ORG_NOT_FOUND"
ORG_PARENT_NOT_FOUND = "ORG_PARENT_NOT_FOUND"
ORG_CYCLE = "ORG_CYCLE"
ORG_PARENT_IS_SELF = "ORG_PARENT_IS_SELF"
ORG_DEPTH_EXCEEDED = "ORG_DEPTH_EXCEEDED"
TIER_INVALID = "TIER_INVALID"
CONTRACT_STATUS_INVALID = "CONTRACT_STATUS_INVALID"
SLUG_INVALID = "SLUG_INVALID"
SLUG_REQUIRED = "SLUG_REQUIRED"
SLUG_TOO_LONG = "SLUG_TOO_LONG"
NAME_REQUIRED = "NAME_REQUIRED"
SLUG_TAKEN = "SLUG_TAKEN"
ACCOUNT_CRM_REF_TAKEN = "ACCOUNT_CRM_REF_TAKEN"

# Every code `org.py` can raise, mapped to the status it deserves. `ORG_CYCLE`
# and the parent codes are 409: the request is well-formed, and it conflicts
# with the current shape of the hierarchy rather than being malformed.
_STATUS_BY_CODE: dict[str, int] = {
    ORG_NOT_FOUND: 404,
    ORG_PARENT_NOT_FOUND: 409,
    ORG_CYCLE: 409,
    ORG_PARENT_IS_SELF: 409,
    ORG_DEPTH_EXCEEDED: 409,
    SLUG_TAKEN: 409,
    ACCOUNT_CRM_REF_TAKEN: 409,
    TIER_INVALID: 400,
    CONTRACT_STATUS_INVALID: 400,
    SLUG_INVALID: 400,
    SLUG_REQUIRED: 400,
    SLUG_TOO_LONG: 400,
    NAME_REQUIRED: 400,
}


class AccountIn(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    tier: str = Field(default="standard", max_length=31)
    contract_status: str = Field(default="active", max_length=31)
    parent_id: uuid.UUID | None = None
    external_crm_ref: str | None = Field(default=None, max_length=255)
    attributes: dict[str, Any] = Field(default_factory=dict)


class ContactIn(BaseModel):
    """A Chatwoot contact id. Bounded to the column's width, and never empty:

    an empty contact id would bind *every* unmatched contact to this account,
    since the lookup is an equality match on the stored value.
    """

    external_contact_id: str = Field(min_length=1, max_length=255)


class AccountPatchIn(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    tier: str | None = Field(default=None, max_length=31)
    contract_status: str | None = Field(default=None, max_length=31)
    # Absent means "leave the parent alone"; present-and-null means "detach".
    parent_id: uuid.UUID | None = None
    external_crm_ref: str | None = Field(default=None, max_length=255)
    attributes: dict[str, Any] | None = None


class DepartmentIn(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    slug: str = Field(min_length=1, max_length=63)
    parent_id: uuid.UUID | None = None
    external_ref: str | None = Field(default=None, max_length=255)


class DepartmentPatchIn(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    parent_id: uuid.UUID | None = None
    external_ref: str | None = Field(default=None, max_length=255)


def _account_out(row: EnterpriseAccount) -> dict[str, Any]:
    return {
        "account_id": str(row.id),
        "name": row.name,
        "tier": str(row.tier),
        "contract_status": str(row.contract_status),
        "parent_id": str(row.parent_id) if row.parent_id else None,
        "external_crm_ref": row.external_crm_ref,
        "attributes": row.attributes,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def _department_out(row: Department) -> dict[str, Any]:
    return {
        "department_id": str(row.id),
        "name": row.name,
        "slug": row.slug,
        "parent_id": str(row.parent_id) if row.parent_id else None,
        "external_ref": row.external_ref,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def _org_error(exc: org.OrgError) -> Any:
    code = exc.code
    return error_response(
        code,
        str(exc).split(": ", 1)[-1] or code,
        status_code=_STATUS_BY_CODE.get(code, 400),
    )


def _unresolved() -> Any:
    return error_response("AUTH_UNRESOLVED", "tenant context not resolved", status_code=401)


# --- enterprise accounts ----------------------------------------------------


@router.get("/accounts")
async def list_accounts(request: Request) -> Any:
    ctx = get_context(request)
    if ctx is None:
        return _unresolved()
    denied = require_policy(ctx, Action.CASE_READ)
    if denied is not None:
        return denied

    trace_id = new_trace_id()
    async with tenant_session(ctx) as session:
        rows = await org.list_accounts(session, tenant_id=ctx.tenant_id)
        return ok_response({"accounts": [_account_out(r) for r in rows]}, trace_id=trace_id)


@router.post("/accounts")
async def create_account(request: Request, body: AccountIn) -> Any:
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
            row = await org.create_account(
                session,
                ctx=ctx,
                name=body.name,
                tier=body.tier,
                contract_status=body.contract_status,
                parent_id=body.parent_id,
                external_crm_ref=body.external_crm_ref,
                attributes=body.attributes,
                trace_id=trace_id,
            )
        except org.OrgError as exc:
            return _org_error(exc)
        payload = _account_out(row)
        await session.commit()
    return ok_response({"account": payload}, trace_id=trace_id)


@router.get("/accounts/{account_id}/contacts")
async def list_account_contacts(request: Request, account_id: uuid.UUID) -> Any:
    """The Chatwoot contacts bound to this account."""
    ctx = get_context(request)
    if ctx is None:
        return _unresolved()
    denied = require_policy(ctx, Action.CASE_READ)
    if denied is not None:
        return denied

    trace_id = new_trace_id()
    async with tenant_session(ctx) as session:
        contacts = await org.list_contacts(session, tenant_id=ctx.tenant_id, account_id=account_id)
    return ok_response({"contacts": contacts}, trace_id=trace_id)


@router.post("/accounts/{account_id}/contacts")
async def bind_account_contact(request: Request, account_id: uuid.UUID, body: ContactIn) -> Any:
    """Bind a Chatwoot contact to this account.

    This is the write side of tier-driven routing: without a binding the
    platform cannot tell which contract a conversation is under, so tier never
    reaches the handoff decision.
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
        try:
            binding_id = await org.bind_contact(
                session,
                ctx=ctx,
                account_id=account_id,
                external_contact_id=body.external_contact_id,
                actor_id=str(ctx.actor_id) if ctx.actor_id else None,
            )
        except org.OrgError as exc:
            return _org_error(exc)
        await session.commit()
    return ok_response(
        {"contact": body.external_contact_id, "binding_id": str(binding_id)},
        trace_id=trace_id,
    )


@router.delete("/accounts/{account_id}/contacts/{external_contact_id}")
async def unbind_account_contact(
    request: Request, account_id: uuid.UUID, external_contact_id: str
) -> Any:
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
        removed = await org.unbind_contact(
            session,
            tenant_id=ctx.tenant_id,
            account_id=account_id,
            external_contact_id=external_contact_id,
        )
        await session.commit()
    if not removed:
        return error_response(ORG_NOT_FOUND, "contact binding not found", status_code=404)
    return ok_response({"removed": True}, trace_id=trace_id)


@router.get("/accounts/{account_id}")
async def get_account(request: Request, account_id: uuid.UUID) -> Any:
    ctx = get_context(request)
    if ctx is None:
        return _unresolved()
    denied = require_policy(ctx, Action.CASE_READ)
    if denied is not None:
        return denied

    trace_id = new_trace_id()
    async with tenant_session(ctx) as session:
        row = await org.get_account(session, tenant_id=ctx.tenant_id, account_id=account_id)
        if row is None:
            return error_response(ORG_NOT_FOUND, "account not found", status_code=404)
        return ok_response({"account": _account_out(row)}, trace_id=trace_id)


@router.patch("/accounts/{account_id}")
async def patch_account(request: Request, account_id: uuid.UUID, body: AccountPatchIn) -> Any:
    ctx = get_context(request)
    if ctx is None:
        return _unresolved()
    denied = require_policy(ctx, Action.TENANT_ADMIN)
    if denied is not None:
        return denied
    missing_idem = require_write_idempotency(request, Action.TENANT_ADMIN)
    if missing_idem is not None:
        return missing_idem

    supplied = body.model_dump(exclude_unset=True)
    trace_id = new_trace_id()
    async with tenant_session(ctx) as session:
        row = await org.get_account(session, tenant_id=ctx.tenant_id, account_id=account_id)
        if row is None:
            return error_response(ORG_NOT_FOUND, "account not found", status_code=404)
        try:
            changed = await org.update_account(
                session,
                ctx=ctx,
                account=row,
                name=body.name,
                tier=body.tier,
                contract_status=body.contract_status,
                parent_id=body.parent_id,
                parent_set="parent_id" in supplied,
                external_crm_ref=body.external_crm_ref,
                external_crm_ref_set="external_crm_ref" in supplied,
                attributes=body.attributes,
                trace_id=trace_id,
            )
        except org.OrgError as exc:
            return _org_error(exc)
        payload = _account_out(row)
        await session.commit()
    return ok_response({"account": payload, "changed": changed}, trace_id=trace_id)


# --- departments ------------------------------------------------------------


@router.get("/departments")
async def list_departments(request: Request) -> Any:
    ctx = get_context(request)
    if ctx is None:
        return _unresolved()
    denied = require_policy(ctx, Action.CASE_READ)
    if denied is not None:
        return denied

    trace_id = new_trace_id()
    async with tenant_session(ctx) as session:
        rows = await org.list_departments(session, tenant_id=ctx.tenant_id)
        return ok_response({"departments": [_department_out(r) for r in rows]}, trace_id=trace_id)


@router.post("/departments")
async def create_department(request: Request, body: DepartmentIn) -> Any:
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
            row = await org.create_department(
                session,
                ctx=ctx,
                name=body.name,
                slug=body.slug,
                parent_id=body.parent_id,
                external_ref=body.external_ref,
                trace_id=trace_id,
            )
        except org.OrgError as exc:
            return _org_error(exc)
        payload = _department_out(row)
        await session.commit()
    return ok_response({"department": payload}, trace_id=trace_id)


@router.patch("/departments/{department_id}")
async def patch_department(
    request: Request, department_id: uuid.UUID, body: DepartmentPatchIn
) -> Any:
    ctx = get_context(request)
    if ctx is None:
        return _unresolved()
    denied = require_policy(ctx, Action.TENANT_ADMIN)
    if denied is not None:
        return denied
    missing_idem = require_write_idempotency(request, Action.TENANT_ADMIN)
    if missing_idem is not None:
        return missing_idem

    supplied = body.model_dump(exclude_unset=True)
    trace_id = new_trace_id()
    async with tenant_session(ctx) as session:
        row = await org.get_department(
            session, tenant_id=ctx.tenant_id, department_id=department_id
        )
        if row is None:
            return error_response(ORG_NOT_FOUND, "department not found", status_code=404)
        try:
            changed = await org.update_department(
                session,
                ctx=ctx,
                department=row,
                name=body.name,
                parent_id=body.parent_id,
                parent_set="parent_id" in supplied,
                external_ref=body.external_ref,
                external_ref_set="external_ref" in supplied,
                trace_id=trace_id,
            )
        except org.OrgError as exc:
            return _org_error(exc)
        payload = _department_out(row)
        await session.commit()
    return ok_response({"department": payload, "changed": changed}, trace_id=trace_id)


__all__ = ["router"]
