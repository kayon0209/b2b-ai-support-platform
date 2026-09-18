"""SCIM 2.0 provisioning endpoints.

    GET    /scim/v2/Users          list, with `filter=userName eq "..."`
    POST   /scim/v2/Users          create (provision)
    GET    /scim/v2/Users/{id}
    PATCH  /scim/v2/Users/{id}     activate / deactivate / rename
    DELETE /scim/v2/Users/{id}     deactivate (never delete)
    GET    /scim/v2/Groups         list
    POST   /scim/v2/Groups         create a Department
    PATCH  /scim/v2/Groups/{id}    replace the member list

Authentication is a bearer token, not a user's credential: an IdP provisions on
its own schedule with no human present. The token is stored hashed and resolved
through a SECURITY DEFINER function (`resolve_scim_token`, migration 0030),
because the tenant is what the token *establishes* - there is no binding to
read it from.

Every mutation is audited with `actor_type=service` and the token's id, so "who
created this account" answers with the provisioning token rather than "system".
"""

from __future__ import annotations

import hashlib
import uuid
from typing import Any

from fastapi import APIRouter, Request
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from platform_core.audit import service as audit_service
from platform_core.db import app_role_url, session_scope_with_url
from platform_core.identity import scim
from platform_core.identity.models import Department, Membership, User
from platform_core.identity.tenant_context import TenantContext, apply_rls_tenant

router = APIRouter(prefix="/scim/v2", tags=["scim"])

AUDIT_PROVISIONED = "scim.user.provisioned"
AUDIT_DEACTIVATED = "scim.user.deactivated"
AUDIT_GROUP_CHANGED = "scim.group.changed"


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def _authenticate(request: Request) -> TenantContext | None:
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        return None
    token = header[7:].strip()
    if not token:
        return None

    async with session_scope_with_url(app_role_url()) as session:
        row = (
            await session.execute(
                # Through the SECURITY DEFINER function: `scim_tokens` is
                # FORCE-RLS'd on a binding that this is the thing establishing.
                _scim_lookup(_token_hash(token))
            )
        ).one_or_none()
    if row is None:
        # One answer for "unknown token", "revoked token" and "malformed
        # token". Distinguishing them tells a prober which tokens once existed.
        return None
    return TenantContext(
        tenant_id=row[0],
        actor_id=row[1],
        actor_kind="service",
        role="integration_service",
    )


def _scim_lookup(token_hash: str) -> Any:
    from sqlalchemy import text

    return text("SELECT tenant_id, token_id FROM resolve_scim_token(:h)").bindparams(h=token_hash)


def _unauthorized() -> Any:
    from fastapi.responses import JSONResponse

    return JSONResponse(
        status_code=401,
        content=scim.error_response("401", "invalid or missing bearer token"),
        headers={"WWW-Authenticate": "Bearer"},
    )


def _scim_error(exc: scim.ScimError) -> Any:
    from fastapi.responses import JSONResponse

    return JSONResponse(
        status_code=exc.status,
        content=scim.error_response(exc.code, exc.detail, scim_type=exc.scim_type),
    )


async def _tenant_session(ctx: TenantContext) -> AsyncSession:
    """A bound session for one request.

    A context manager rather than FastAPI's dependency because these endpoints
    need a *second* thing from the same transaction (audit + business write),
    and returning the session lets each handler commit once.
    """
    scope = session_scope_with_url(app_role_url())
    session = await scope.__aenter__()
    await apply_rls_tenant(session, ctx)
    return session


class _SessionScope:
    def __init__(self, ctx: TenantContext) -> None:
        self._ctx = ctx
        self._scope: Any = None

    async def __aenter__(self) -> AsyncSession:
        self._scope = session_scope_with_url(app_role_url())
        session: AsyncSession = await self._scope.__aenter__()
        await apply_rls_tenant(session, self._ctx)
        return session

    async def __aexit__(self, *exc: object) -> None:
        await self._scope.__aexit__(*exc)


# --- Users -------------------------------------------------------------------


@router.get("/Users")
async def list_users(request: Request) -> Any:
    ctx = await _authenticate(request)
    if ctx is None:
        return _unauthorized()

    try:
        parsed = scim.parse_filter(request.query_params.get("filter"))
        offset, limit, start = scim.paging(
            _int_param(request.query_params.get("startIndex")),
            _int_param(request.query_params.get("count")),
            0,
        )
    except scim.ScimError as exc:
        return _scim_error(exc)

    async with _SessionScope(ctx) as session:
        # Provisioned users are the ones a membership exists for: this endpoint
        # must not become a directory of every user row in the platform, and a
        # User with no membership in this tenant is not this tenant's resource.
        stmt = (
            select(User)
            .join(Membership, Membership.user_id == User.id)
            .where(Membership.tenant_id == ctx.tenant_id)
        )
        count_stmt = (
            select(func.count())
            .select_from(User)
            .join(Membership, Membership.user_id == User.id)
            .where(Membership.tenant_id == ctx.tenant_id)
        )
        if parsed is not None:
            if parsed.attribute == "username":
                stmt = stmt.where(User.primary_email == parsed.value.lower())
                count_stmt = count_stmt.where(User.primary_email == parsed.value.lower())
            elif parsed.attribute == "displayname":
                stmt = stmt.where(User.display_name == parsed.value)
                count_stmt = count_stmt.where(User.display_name == parsed.value)
            else:
                # `externalId`: this platform has no separate external id for a
                # User - `external_identities` does - so it is matched against
                # the email rather than returning an empty page that looks like
                # "no such user".
                stmt = stmt.where(User.primary_email == parsed.value.lower())
                count_stmt = count_stmt.where(User.primary_email == parsed.value.lower())

        total = int((await session.execute(count_stmt)).scalar_one())
        rows = list(
            (
                await session.execute(stmt.order_by(User.primary_email).limit(limit).offset(offset))
            ).scalars()
        )
        resources = [
            scim.user_resource(
                user_id=row.id,
                email=row.primary_email,
                display_name=row.display_name,
                active=True,
            )
            for row in rows
        ]
    return scim.list_response(resources, total=total, start=start, limit=limit)


@router.post("/Users")
async def create_user(request: Request) -> Any:
    ctx = await _authenticate(request)
    if ctx is None:
        return _unauthorized()

    payload = await request.json()
    try:
        email, display_name, active = scim.user_from_payload(payload)
    except scim.ScimError as exc:
        return _scim_error(exc)

    async with _SessionScope(ctx) as session:
        existing = (
            await session.execute(select(User).where(User.primary_email == email))
        ).scalar_one_or_none()
        if existing is not None:
            # Idempotent by `userName`: an IdP retrying a provisioning request
            # must not fail, and it must not create a duplicate either.
            return scim.user_resource(
                user_id=existing.id,
                email=existing.primary_email,
                display_name=existing.display_name,
                active=active,
            )

        user = User(primary_email=email, display_name=display_name, is_service_account=False)
        session.add(user)
        await session.flush()
        if active:
            session.add(
                Membership(
                    tenant_id=ctx.tenant_id,
                    user_id=user.id,
                    # The least-privileged role, never a role taken from the
                    # payload: see `scim.py` on why a Group is not a Role.
                    role="support_viewer",
                    status="active",
                )
            )
        await audit_service.record(
            session,
            ctx=ctx,
            action=AUDIT_PROVISIONED,
            resource_type="user",
            resource_id=user.id,
            decision="completed",
            reason_code="OK",
            metadata={"active": active, "token_id": str(ctx.actor_id)},
        )
        await session.commit()
        resource = scim.user_resource(
            user_id=user.id, email=email, display_name=display_name, active=active
        )
    return resource


@router.get("/Users/{user_id}")
async def get_user(request: Request, user_id: uuid.UUID) -> Any:
    ctx = await _authenticate(request)
    if ctx is None:
        return _unauthorized()
    async with _SessionScope(ctx) as session:
        row = await _provisioned_user(session, ctx.tenant_id, user_id)
        if row is None:
            return _scim_error(scim.ScimError(scim.SCIM_NOT_FOUND, "user not found", status=404))
        return scim.user_resource(
            user_id=row.id,
            email=row.primary_email,
            display_name=row.display_name,
            active=await _is_active(session, ctx.tenant_id, row.id),
        )


@router.patch("/Users/{user_id}")
async def patch_user(request: Request, user_id: uuid.UUID) -> Any:
    """The deprovisioning path, and the reason `remove` has to mean something."""
    ctx = await _authenticate(request)
    if ctx is None:
        return _unauthorized()

    payload = await request.json()
    operations = payload.get("Operations")
    try:
        # Validated before the read so a malformed PATCH cannot reach the
        # database at all.
        scim.apply_patch({"Operations": operations}, mutable=scim.MUTABLE_USER)
    except scim.ScimError as exc:
        return _scim_error(exc)

    async with _SessionScope(ctx) as session:
        row = await _provisioned_user(session, ctx.tenant_id, user_id)
        if row is None:
            return _scim_error(scim.ScimError(scim.SCIM_NOT_FOUND, "user not found", status=404))

        # PATCH is a **partial** update, so the merge is based on the resource as
        # it stands. Validating the patch alone against `user_from_payload`
        # demanded a `userName` the request never had to send - and a
        # deprovisioning PATCH that carries only `active` is the common case.
        current = {
            "userName": row.primary_email,
            "displayName": row.display_name,
            "active": await _is_active(session, ctx.tenant_id, row.id),
        }
        try:
            merged = scim.apply_patch(
                {**current, "Operations": operations}, mutable=scim.MUTABLE_USER
            )
            _, display_name, active = scim.user_from_payload(merged)
        except scim.ScimError as exc:
            return _scim_error(exc)

        row.display_name = display_name
        membership = await _membership(session, ctx.tenant_id, row.id)
        if membership is not None:
            # `status`, not a delete: the audit trail and the Cases they worked
            # on still reference this membership.
            membership.status = "active" if active else "suspended"
        await audit_service.record(
            session,
            ctx=ctx,
            action=AUDIT_PROVISIONED if active else AUDIT_DEACTIVATED,
            resource_type="user",
            resource_id=row.id,
            decision="completed",
            reason_code="OK",
            metadata={"active": active, "token_id": str(ctx.actor_id)},
        )
        await session.commit()
        resource = scim.user_resource(
            user_id=row.id, email=row.primary_email, display_name=display_name, active=active
        )
    return resource


@router.delete("/Users/{user_id}")
async def delete_user(request: Request, user_id: uuid.UUID) -> Any:
    """SCIM's DELETE means "deactivate", not "erase".

    A row that disappears breaks the audit trail that references it and the
    Cases it worked on. The IdP's intent - this person no longer has access - is
    exactly what suspension expresses.
    """
    ctx = await _authenticate(request)
    if ctx is None:
        return _unauthorized()
    async with _SessionScope(ctx) as session:
        row = await _provisioned_user(session, ctx.tenant_id, user_id)
        if row is None:
            return _scim_error(scim.ScimError(scim.SCIM_NOT_FOUND, "user not found", status=404))
        membership = await _membership(session, ctx.tenant_id, row.id)
        if membership is not None:
            membership.status = "suspended"
        await audit_service.record(
            session,
            ctx=ctx,
            action=AUDIT_DEACTIVATED,
            resource_type="user",
            resource_id=row.id,
            decision="completed",
            reason_code="OK",
            metadata={"token_id": str(ctx.actor_id)},
        )
        await session.commit()
    from fastapi.responses import Response

    return Response(status_code=204)


# --- Groups (Departments) ----------------------------------------------------


@router.get("/Groups")
async def list_groups(request: Request) -> Any:
    ctx = await _authenticate(request)
    if ctx is None:
        return _unauthorized()
    try:
        offset, limit, start = scim.paging(
            _int_param(request.query_params.get("startIndex")),
            _int_param(request.query_params.get("count")),
            0,
        )
    except scim.ScimError as exc:
        return _scim_error(exc)

    async with _SessionScope(ctx) as session:
        total = int(
            (
                await session.execute(
                    select(func.count())
                    .select_from(Department)
                    .where(Department.tenant_id == ctx.tenant_id)
                )
            ).scalar_one()
        )
        rows = list(
            (
                await session.execute(
                    select(Department)
                    .where(Department.tenant_id == ctx.tenant_id)
                    .order_by(Department.slug)
                    .limit(limit)
                    .offset(offset)
                )
            ).scalars()
        )
        resources = [
            scim.group_resource(
                department_id=row.id,
                display_name=row.name,
                slug=row.slug,
                members=await _department_members(session, ctx.tenant_id, row.id),
            )
            for row in rows
        ]
    return scim.list_response(resources, total=total, start=start, limit=limit)


@router.post("/Groups")
async def create_group(request: Request) -> Any:
    ctx = await _authenticate(request)
    if ctx is None:
        return _unauthorized()
    payload = await request.json()
    try:
        display_name, slug = scim.group_from_payload(payload)
        members = scim.member_ids(payload)
    except scim.ScimError as exc:
        return _scim_error(exc)

    async with _SessionScope(ctx) as session:
        existing = (
            await session.execute(
                select(Department).where(
                    Department.tenant_id == ctx.tenant_id, Department.slug == slug
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            # `externalId` is the idempotency key, so a repeated provisioning
            # request converges rather than creating `support` and `support-2`.
            await _replace_members(session, ctx, existing.id, members)
            await session.commit()
            return scim.group_resource(
                department_id=existing.id,
                display_name=existing.name,
                slug=existing.slug,
                members=members,
            )

        department = Department(tenant_id=ctx.tenant_id, name=display_name, slug=slug)
        session.add(department)
        try:
            await session.flush()
        except IntegrityError:
            await session.rollback()
            # `return`, so no `from exc` - and the exception is fully
            # represented by the SCIM error below.
            return _scim_error(
                scim.ScimError(
                    scim.SCIM_UNIQUENESS,
                    "slug already in use",
                    status=409,
                    scim_type=scim.SCIM_UNIQUENESS,
                )
            )
        await _replace_members(session, ctx, department.id, members)
        await session.commit()
        resource = scim.group_resource(
            department_id=department.id, display_name=display_name, slug=slug, members=members
        )
    return resource


@router.patch("/Groups/{group_id}")
async def patch_group(request: Request, group_id: uuid.UUID) -> Any:
    ctx = await _authenticate(request)
    if ctx is None:
        return _unauthorized()
    payload = await request.json()
    operations = payload.get("Operations")
    try:
        scim.apply_patch({"Operations": operations}, mutable=scim.MUTABLE_GROUP)
    except scim.ScimError as exc:
        return _scim_error(exc)

    async with _SessionScope(ctx) as session:
        department = (
            await session.execute(
                select(Department).where(
                    Department.id == group_id, Department.tenant_id == ctx.tenant_id
                )
            )
        ).scalar_one_or_none()
        if department is None:
            return _scim_error(scim.ScimError(scim.SCIM_NOT_FOUND, "group not found", status=404))

        # Based on the current resource, for the same reason as the user PATCH.
        current = {
            "displayName": department.name,
            "externalId": department.slug,
            "members": [
                {"value": str(m)}
                for m in await _department_members(session, ctx.tenant_id, department.id)
            ],
        }
        try:
            merged = scim.apply_patch(
                {**current, "Operations": operations}, mutable=scim.MUTABLE_GROUP
            )
            display_name, _ = scim.group_from_payload(merged)
            members = scim.member_ids(merged)
        except scim.ScimError as exc:
            return _scim_error(exc)

        department.name = display_name
        await _replace_members(session, ctx, department.id, members)
        await session.commit()
        resource = scim.group_resource(
            department_id=department.id,
            display_name=display_name,
            slug=department.slug,
            members=members,
        )
    return resource


# --- helpers -----------------------------------------------------------------


def _int_param(value: str | None) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except ValueError as exc:
        raise scim.ScimError(scim.SCIM_INVALID_VALUE, f"{value!r} is not a number") from exc


async def _provisioned_user(
    session: AsyncSession, tenant_id: uuid.UUID, user_id: uuid.UUID
) -> User | None:
    """A user this tenant actually has a membership for.

    The join is the authorisation: without it, a SCIM token could read any user
    row in the platform by id, and the tenant boundary would be the token's
    alone to cross.
    """
    return (
        await session.execute(
            select(User)
            .join(Membership, Membership.user_id == User.id)
            .where(Membership.tenant_id == tenant_id, User.id == user_id)
        )
    ).scalar_one_or_none()


async def _membership(
    session: AsyncSession, tenant_id: uuid.UUID, user_id: uuid.UUID
) -> Membership | None:
    return (
        await session.execute(
            select(Membership).where(
                Membership.tenant_id == tenant_id, Membership.user_id == user_id
            )
        )
    ).scalar_one_or_none()


async def _is_active(session: AsyncSession, tenant_id: uuid.UUID, user_id: uuid.UUID) -> bool:
    membership = await _membership(session, tenant_id, user_id)
    return membership is not None and membership.status == "active"


async def _department_members(
    session: AsyncSession, tenant_id: uuid.UUID, department_id: uuid.UUID
) -> list[uuid.UUID]:
    rows = await session.execute(
        select(Membership.user_id).where(
            Membership.tenant_id == tenant_id, Membership.department_id == department_id
        )
    )
    return [row[0] for row in rows.all()]


async def _replace_members(
    session: AsyncSession,
    ctx: TenantContext,
    department_id: uuid.UUID,
    members: list[uuid.UUID],
) -> None:
    """Set the department's membership to exactly `members`.

    Replace rather than add: SCIM's Group resource is declarative, and a
    membership that the IdP has dropped must disappear here or the two systems
    drift - which is the drift SCIM exists to remove.
    """
    current = await _department_members(session, ctx.tenant_id, department_id)
    wanted = set(members)
    for user_id in wanted - set(current):
        membership = await _membership(session, ctx.tenant_id, user_id)
        if membership is None:
            # No membership in this tenant: assigning a department to one is not
            # this endpoint's decision, so it is skipped rather than created.
            continue
        membership.department_id = department_id
    for user_id in set(current) - wanted:
        membership = await _membership(session, ctx.tenant_id, user_id)
        if membership is not None:
            membership.department_id = None
    await audit_service.record(
        session,
        ctx=ctx,
        action=AUDIT_GROUP_CHANGED,
        resource_type="department",
        resource_id=department_id,
        decision="completed",
        reason_code="OK",
        metadata={"members": len(wanted), "token_id": str(ctx.actor_id)},
    )


__all__ = ["router"]
