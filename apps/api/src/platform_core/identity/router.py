"""Tenant self-service identity API (Phase 5).

    GET    /v1/identity/members              list members in the tenant
    POST   /v1/identity/members/invite       invite a user (tenant_owner,
                                             security_admin)
    POST   /v1/identity/members/accept       consume an invite token
    POST   /v1/identity/members/{id}         update a member's role
    DELETE /v1/identity/members/{id}         remove a member

Authorization:
- listing and managing members requires TENANT_ADMIN (tenant_owner,
  security_admin). No other role can enumerate or modify the tenant's
  membership.
- accepting an invite requires nothing beyond a valid token: it is how
  someone joins the tenant in the first place. Because it runs before any
  tenant context exists, the token is resolved through the narrow
  SECURITY DEFINER function `resolve_invitation_by_token` (migration 0021)
  rather than a table-wide read.

Every write command requires an Idempotency-Key and records an audit event.
"""

import time
import uuid
from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from uuid6 import uuid7

from platform_core.api import (
    IDEMPOTENCY_KEY_REQUIRED,
    VALIDATION_FAILED,
    error_response,
    get_context,
    new_trace_id,
    ok_response,
    parse_uuid,
    require_idempotency_key,
    require_policy,
    tenant_session,
)
from platform_core.audit import service as audit_service
from platform_core.db import session_scope_with_url
from platform_core.identity import org
from platform_core.identity.models import (
    InvitationStatus,
    Membership,
    MembershipInvitation,
    MembershipRole,
    User,
)
from platform_core.identity.tenant_context import TenantContext
from platform_policy import Action

router = APIRouter(prefix="/v1/identity", tags=["identity"])

# Roles a tenant_owner or security_admin may assign. tenant_owner is not
# assignable via invite (it is the bootstrap role); the existing owner stays.
ASSIGNABLE_ROLES = frozenset(
    role.value for role in MembershipRole if role != MembershipRole.TENANT_OWNER
)

INVITE_TTL_SECONDS = 7 * 24 * 3600  # 7 days


class MemberOut(BaseModel):
    user_id: str
    membership_id: str
    email: str
    display_name: str
    role: str
    status: str


class InviteIn(BaseModel):
    email: str = Field(min_length=3, max_length=255)
    role: str = Field(default=MembershipRole.SUPPORT_VIEWER.value)


class AcceptInviteIn(BaseModel):
    token: uuid.UUID
    email: str = Field(min_length=3, max_length=255)
    display_name: str = Field(min_length=1, max_length=255)


class RoleUpdateIn(BaseModel):
    role: str
    # Optional department assignment. Absent means "leave it alone" - this
    # endpoint also changes the role, and a role change must not silently
    # detach the member from their department. Explicit `null` clears it.
    department_id: uuid.UUID | None = None


def _role_value(role: MembershipRole | str) -> str:
    """Normalize a role to its string value regardless of type."""
    if isinstance(role, MembershipRole):
        return role.value
    return role


def _member_out(user: User, membership: Membership) -> MemberOut:
    return MemberOut(
        user_id=str(user.id),
        # The membership PK is what POST/DELETE /v1/identity/members/{id}
        # address (identity/router.py resolves `session.get(Membership, ...)`).
        # The user id is a different key; handing the UI only the user id made
        # every role change and removal 404 with "membership not found".
        membership_id=str(membership.id),
        email=user.primary_email,
        display_name=user.display_name,
        role=_role_value(membership.role),
        status=membership.status,
    )


def _app_role_url() -> str:
    """The non-bypass application role's URL.

    Delegates to `db.app_role_url`, which is the single statement of which role
    a request connects as. This module used to compute it independently, and so
    did three others - five copies of the one expression whose duplication the
    function's own docstring warns about.
    """
    from platform_core.db import app_role_url

    return app_role_url()


async def _list_members(session: AsyncSession, tenant_id: uuid.UUID) -> list[MemberOut]:
    rows = await session.execute(
        select(User, Membership)
        .join(Membership, Membership.user_id == User.id)
        .where(Membership.tenant_id == tenant_id)
        .order_by(Membership.role, User.display_name)
    )
    return [_member_out(user, membership) for user, membership in rows]


def _auth_or_denied(request: Request) -> tuple[Any, Any]:
    """Return (ctx, None) when authorized, else (None, denial_response).

    Collapses the repeated 401/403 preamble. Kept explicit rather than a
    decorator so the returned envelope stays visible at each call site.
    """
    ctx = get_context(request)
    if ctx is None:
        return None, error_response(
            "AUTH_UNRESOLVED", "tenant context not resolved", status_code=401
        )
    denied = require_policy(ctx, Action.TENANT_ADMIN)
    if denied is not None:
        return None, denied
    return ctx, None


# --- Read ------------------------------------------------------------------


@router.get("/members")
async def list_members(request: Request) -> Any:
    ctx, denied = _auth_or_denied(request)
    if denied is not None:
        return denied

    async with tenant_session(ctx) as session:
        members = await _list_members(session, ctx.tenant_id)
    return ok_response({"items": [m.model_dump() for m in members], "total": len(members)})


# --- Invite ----------------------------------------------------------------


@router.post("/members/invite")
async def invite_member(request: Request, body: InviteIn) -> Any:
    ctx, denied = _auth_or_denied(request)
    if denied is not None:
        return denied

    idem = require_idempotency_key(request)
    if not idem:
        return error_response(
            IDEMPOTENCY_KEY_REQUIRED,
            "every write command must carry an Idempotency-Key header",
            status_code=400,
        )

    if body.role not in ASSIGNABLE_ROLES:
        return error_response(
            VALIDATION_FAILED,
            f"role must be one of {sorted(ASSIGNABLE_ROLES)}",
            status_code=400,
        )

    trace_id = new_trace_id()
    now = int(time.time())
    expires_at = now + INVITE_TTL_SECONDS

    async with tenant_session(ctx) as session:
        # Already a member? Say so instead of minting a token that would fail.
        existing = await session.execute(
            select(Membership, User)
            .join(User, User.id == Membership.user_id)
            .where(
                Membership.tenant_id == ctx.tenant_id,
                func.lower(User.primary_email) == body.email.lower(),
            )
        )
        row = existing.one_or_none()
        if row is not None:
            membership, user = row
            await audit_service.record(
                session,
                ctx=ctx,
                action="identity.invite.already_member",
                resource_type="membership",
                resource_id=membership.id,
                after={
                    "email": body.email,
                    "role": _role_value(membership.role),
                    "idempotency_key": idem,
                },
                trace_id=trace_id,
            )
            await session.commit()
            return ok_response(
                {
                    "message": "user already a member",
                    "user_id": str(user.id),
                    "role": _role_value(membership.role),
                },
                trace_id=trace_id,
            )

        # A pending, unexpired invite is returned as-is (idempotent re-invite).
        prior = (
            await session.execute(
                select(MembershipInvitation).where(
                    MembershipInvitation.tenant_id == ctx.tenant_id,
                    MembershipInvitation.email == body.email,
                )
            )
        ).scalar_one_or_none()

        if (
            prior is not None
            and prior.status == InvitationStatus.PENDING.value
            and prior.expires_at >= now
        ):
            await audit_service.record(
                session,
                ctx=ctx,
                action="identity.invite.reused",
                resource_type="membership_invitation",
                resource_id=prior.id,
                after={"email": body.email, "role": body.role, "idempotency_key": idem},
                trace_id=trace_id,
            )
            await session.commit()
            return ok_response(
                {"invitation_token": str(prior.token), "status": prior.status},
                trace_id=trace_id,
            )

        token = uuid7()
        if prior is not None:
            # Re-issue in place: UNIQUE(tenant_id, email) means a consumed or
            # expired invitation for this address must be recycled, not
            # inserted alongside.
            prior.token = token
            prior.role = MembershipRole(body.role)
            prior.status = InvitationStatus.PENDING.value
            prior.created_at = now
            prior.expires_at = expires_at
            prior.accepted_at = None
            prior.created_by = ctx.actor_id
            invite = prior
        else:
            invite = MembershipInvitation(
                tenant_id=ctx.tenant_id,
                email=body.email,
                role=MembershipRole(body.role),
                token=token,
                created_by=ctx.actor_id,
                created_at=now,
                expires_at=expires_at,
                status=InvitationStatus.PENDING.value,
            )
            session.add(invite)

        await session.flush()
        await audit_service.record(
            session,
            ctx=ctx,
            action="identity.invite.created",
            resource_type="membership_invitation",
            resource_id=invite.id,
            after={
                "email": body.email,
                "role": body.role,
                "expires_at": expires_at,
                "idempotency_key": idem,
            },
            trace_id=trace_id,
        )
        await session.commit()
        result = {"invitation_token": str(invite.token), "status": invite.status}

    return ok_response(result, trace_id=trace_id)


# --- Accept (no prior tenant context; token is the credential) -------------


@router.post("/members/accept")
async def accept_invite(request: Request, body: AcceptInviteIn) -> Any:
    idem = require_idempotency_key(request)
    if not idem:
        return error_response(
            IDEMPOTENCY_KEY_REQUIRED,
            "every write command must carry an Idempotency-Key header",
            status_code=400,
        )

    trace_id = new_trace_id()
    now = int(time.time())

    async with session_scope_with_url(_app_role_url()) as session:
        row = (
            (
                await session.execute(
                    text(
                        "SELECT id, tenant_id, email, role, status, expires_at "
                        "FROM resolve_invitation_by_token(:t)"
                    ),
                    {"t": str(body.token)},
                )
            )
            .mappings()
            .one_or_none()
        )

        # A wrong token, or a token presented with the wrong email, is a 404
        # in both cases so the endpoint is not an invitation-existence oracle.
        if row is None or row["email"].lower() != body.email.lower():
            return error_response("INVITATION_NOT_FOUND", "invitation not found", status_code=404)
        if row["status"] == InvitationStatus.ACCEPTED.value:
            return error_response(
                "INVITATION_ALREADY_USED",
                "invitation has already been accepted",
                status_code=409,
            )
        if row["status"] != InvitationStatus.PENDING.value or row["expires_at"] < now:
            return error_response(
                "INVITATION_EXPIRED", "invitation is no longer valid", status_code=409
            )

        tenant_id: uuid.UUID = row["tenant_id"]
        await session.execute(
            text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant_id)}
        )

        user = (
            await session.execute(
                select(User).where(func.lower(User.primary_email) == body.email.lower())
            )
        ).scalar_one_or_none()
        if user is None:
            user = User(
                primary_email=body.email,
                display_name=body.display_name,
                is_service_account=False,
            )
            session.add(user)
            await session.flush()

        membership = (
            await session.execute(
                select(Membership).where(
                    Membership.tenant_id == tenant_id,
                    Membership.user_id == user.id,
                )
            )
        ).scalar_one_or_none()
        if membership is None:
            membership = Membership(
                tenant_id=tenant_id,
                user_id=user.id,
                role=MembershipRole(row["role"]),
                status="active",
            )
            session.add(membership)

        invitation = (
            await session.execute(
                select(MembershipInvitation).where(MembershipInvitation.id == row["id"])
            )
        ).scalar_one_or_none()
        if invitation is None:  # pragma: no cover - resolver returned it above
            return error_response("INVITATION_NOT_FOUND", "invitation not found", status_code=404)
        invitation.status = InvitationStatus.ACCEPTED.value
        invitation.accepted_at = now

        # No middleware context here, so the audit actor is the system acting
        # on a verified capability.
        system_ctx = TenantContext(
            tenant_id=tenant_id, actor_id=None, actor_kind="system", role=None
        )
        await audit_service.record(
            session,
            ctx=system_ctx,
            action="identity.invite.accepted",
            resource_type="membership_invitation",
            resource_id=invitation.id,
            after={
                "email": body.email,
                "role": _role_value(membership.role),
                "idempotency_key": idem,
            },
            trace_id=trace_id,
        )
        await session.commit()
        result = {
            "user_id": str(user.id),
            "role": _role_value(membership.role),
            "tenant_id": str(tenant_id),
        }

    return ok_response(result, trace_id=trace_id)


# --- Update / remove -------------------------------------------------------


@router.post("/members/{member_id}")
async def update_member_role(request: Request, member_id: str, body: RoleUpdateIn) -> Any:
    ctx, denied = _auth_or_denied(request)
    if denied is not None:
        return denied

    idem = require_idempotency_key(request)
    if not idem:
        return error_response(
            IDEMPOTENCY_KEY_REQUIRED,
            "every write command must carry an Idempotency-Key header",
            status_code=400,
        )

    if body.role not in ASSIGNABLE_ROLES:
        return error_response(
            VALIDATION_FAILED,
            f"role must be one of {sorted(ASSIGNABLE_ROLES)}",
            status_code=400,
        )

    try:
        mid = parse_uuid(member_id, field="member_id")
    except ValueError as exc:
        return error_response(VALIDATION_FAILED, str(exc), status_code=400)

    trace_id = new_trace_id()
    async with tenant_session(ctx) as session:
        membership = await session.get(Membership, mid)
        if membership is None:
            return error_response("MEMBERSHIP_NOT_FOUND", "membership not found", status_code=404)
        before_role = _role_value(membership.role)
        membership.role = MembershipRole(body.role)

        supplied = body.model_dump(exclude_unset=True)
        department_changed = False
        if "department_id" in supplied:
            if body.department_id is not None and not await org.department_exists(
                session, tenant_id=ctx.tenant_id, department_id=body.department_id
            ):
                # Also the answer for another tenant's department: RLS cannot
                # see it, and saying "that exists but is not yours" would
                # confirm the existence of another tenant's rows.
                return error_response(
                    "DEPARTMENT_NOT_FOUND",
                    "department not found in this tenant",
                    status_code=404,
                )
            department_changed = membership.department_id != body.department_id
            membership.department_id = body.department_id

        await audit_service.record(
            session,
            ctx=ctx,
            action="identity.member.role_changed",
            resource_type="membership",
            resource_id=membership.id,
            before={"role": before_role},
            after={
                "role": body.role,
                "idempotency_key": idem,
                **(
                    {"department_id": str(body.department_id) if body.department_id else None}
                    if department_changed
                    else {}
                ),
            },
            trace_id=trace_id,
        )
        await session.commit()
        result = {
            "user_id": str(membership.user_id),
            "role": _role_value(membership.role),
            "department_id": (str(membership.department_id) if membership.department_id else None),
        }

    return ok_response(result, trace_id=trace_id)


@router.delete("/members/{member_id}")
async def remove_member(request: Request, member_id: str) -> Any:
    ctx, denied = _auth_or_denied(request)
    if denied is not None:
        return denied

    idem = require_idempotency_key(request)
    if not idem:
        return error_response(
            IDEMPOTENCY_KEY_REQUIRED,
            "every write command must carry an Idempotency-Key header",
            status_code=400,
        )

    try:
        mid = parse_uuid(member_id, field="member_id")
    except ValueError as exc:
        return error_response(VALIDATION_FAILED, str(exc), status_code=400)

    trace_id = new_trace_id()
    async with tenant_session(ctx) as session:
        membership = await session.get(Membership, mid)
        if membership is None:
            return error_response("MEMBERSHIP_NOT_FOUND", "membership not found", status_code=404)

        if _role_value(membership.role) == MembershipRole.TENANT_OWNER.value:
            owner_count = (
                await session.execute(
                    select(func.count())
                    .select_from(Membership)
                    .where(
                        Membership.tenant_id == ctx.tenant_id,
                        Membership.role == MembershipRole.TENANT_OWNER,
                    )
                )
            ).scalar_one()
            if owner_count <= 1:
                return error_response(
                    VALIDATION_FAILED,
                    "cannot remove the last tenant_owner of a tenant",
                    status_code=400,
                )

        await session.delete(membership)
        await audit_service.record(
            session,
            ctx=ctx,
            action="identity.member.removed",
            resource_type="membership",
            resource_id=mid,
            after={"idempotency_key": idem},
            trace_id=trace_id,
        )
        await session.commit()

    return ok_response({"removed": str(mid)}, trace_id=trace_id)
