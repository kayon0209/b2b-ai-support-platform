"""Feature flag API (Phase 4, ticket 40).

    GET  /v1/flags                    definitions and their rollout
    GET  /v1/flags/{key}/evaluate     what this tenant resolves to, and why
    GET  /v1/flags/{key}/preview      effect of a hypothetical rollout change
    POST /v1/flags                    define a flag (always starts closed)
    POST /v1/flags/{key}/rollout      canary step, and the rollback
    POST /v1/flags/{key}/enabled      the kill switch
    POST /v1/flags/{key}/targets      pin one tenant in or out

Authorization is split like the prompt release API. Reading which flags are
live is incident-analysis information, so `flag.read` is held by
security_admin, auditor and tenant_owner. Moving a rollout changes what
production serves, so `flag.write` is tenant_owner only.

Note the asymmetry in `evaluate`: it resolves a flag *for the calling tenant*.
There is no route that evaluates a flag on behalf of an arbitrary tenant id
supplied by the client, because a reader that could ask "is flag X on for
tenant Y?" would get a cross-tenant feature-state oracle. Targeting a
specific tenant is an operator action (`/targets`), not a read.
"""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Body, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from platform_core.api import error_response, require_write_idempotency
from platform_core.config import get_settings
from platform_core.db import session_scope_with_url
from platform_core.identity import tenant_context
from platform_core.identity.tenant_context import TenantContext, apply_rls_tenant
from platform_core.knowledge import flag_service
from platform_policy import Action, Decision, PolicyEngine, Principal

router = APIRouter(prefix="/v1/flags", tags=["feature-flags"])


class DefineIn(BaseModel):
    key: str = Field(min_length=1, max_length=127)
    description: str = Field(default="", max_length=2000)


class RolloutIn(BaseModel):
    rollout_percent: int = Field(ge=0, le=100)


class EnabledIn(BaseModel):
    enabled: bool


class TargetIn(BaseModel):
    target_tenant_id: str
    enabled: bool


def _principal_from_ctx(ctx: TenantContext) -> Principal:
    return Principal(
        tenant_id=str(ctx.tenant_id),
        actor_id=str(ctx.actor_id) if ctx.actor_id else "",
        role=ctx.role or "unknown",
    )


def _denied(action: str, reason: str) -> JSONResponse:
    """403, not a 200 with an error body (see prompt_router for the why)."""
    return error_response(
        "FLAG_ACCESS_DENIED",
        reason or "flag access denied",
        status_code=403,
        details={"action": action},
    )


def _flag_error(exc: flag_service.FlagError) -> dict[str, Any]:
    return {
        "error": {
            "code": exc.code,
            "reason": exc.detail or exc.code,
            "retryable": False,
        },
        "trace_id": "",
    }


def _ctx_of(request: Request) -> TenantContext:
    ctx = getattr(request.state, "tenant_context", None)
    if ctx is None:
        ctx = tenant_context.get_tenant_context()
    return ctx


def _gate(request: Request, ctx: TenantContext, action: Action, name: str) -> JSONResponse | None:
    """Return a denial envelope, or None when the caller may proceed.

    A write action additionally requires an Idempotency-Key, so every write
    endpoint in this router enforces it through this one gate.
    """
    decision = PolicyEngine().check(_principal_from_ctx(ctx), action)
    if decision.decision != Decision.ALLOW.value:
        return _denied(name, decision.reason_code)
    return require_write_idempotency(request, action)


def _app_url() -> str:
    settings = get_settings()
    return settings.database_url.replace("platform:platform@", "platform_app:platform_app@")


def _flag_out(row: Any) -> dict[str, Any]:
    return {
        "key": row.key,
        "description": row.description,
        "enabled": bool(row.enabled),
        "rollout_percent": row.rollout_percent,
        "created_at": row.created_at,
    }


def _decision_out(decision: flag_service.FlagDecision) -> dict[str, Any]:
    return {
        "key": decision.key,
        "enabled": decision.enabled,
        "reason": decision.reason,
        "rollout_percent": decision.rollout_percent,
    }


@router.get("")
async def list_feature_flags(request: Request) -> Any:
    ctx = _ctx_of(request)
    denial = _gate(request, ctx, Action.FLAG_READ, "flag.read")
    if denial is not None:
        return denial

    async with session_scope_with_url(_app_url()) as session:
        await apply_rls_tenant(session, ctx)
        rows = await flag_service.list_flags(session, tenant_id=ctx.tenant_id)
        return {"items": [_flag_out(r) for r in rows], "total": len(rows)}


@router.get("/{key}/evaluate")
async def evaluate_feature_flag(
    request: Request,
    key: str,
    default: bool = Query(default=False),
) -> Any:
    """Resolve a flag for the calling tenant.

    The calling tenant is taken from the resolved context, never from a
    parameter: accepting a tenant id here would make this a cross-tenant
    feature-state oracle.
    """
    ctx = _ctx_of(request)
    denial = _gate(request, ctx, Action.FLAG_READ, "flag.read")
    if denial is not None:
        return denial

    async with session_scope_with_url(_app_url()) as session:
        await apply_rls_tenant(session, ctx)
        decision = await flag_service.evaluate(
            session, flag_key=key, tenant_id=ctx.tenant_id, default=default
        )
        return _decision_out(decision)


@router.get("/{key}/preview")
async def preview_feature_flag(
    request: Request,
    key: str,
    percents: Annotated[list[int] | None, Query()] = None,
) -> Any:
    """Whether this tenant would be inside a rollout at various percentages."""
    ctx = _ctx_of(request)
    denial = _gate(request, ctx, Action.FLAG_READ, "flag.read")
    if denial is not None:
        return denial

    if percents is not None and any(not 0 <= p <= 100 for p in percents):
        return _flag_error(
            flag_service.FlagError("INVALID_PERCENT", "rollout must be between 0 and 100")
        )

    async with session_scope_with_url(_app_url()) as session:
        await apply_rls_tenant(session, ctx)
        rows = await flag_service.rollout_preview(
            session, tenant_id=ctx.tenant_id, key=key, percents=percents
        )
        return {"key": key, "points": rows}


@router.post("")
async def define_feature_flag(
    request: Request,
    payload: Annotated[DefineIn, Body()],
) -> Any:
    ctx = _ctx_of(request)
    denial = _gate(request, ctx, Action.FLAG_WRITE, "flag.write")
    if denial is not None:
        return denial

    async with session_scope_with_url(_app_url()) as session:
        await apply_rls_tenant(session, ctx)
        try:
            row = await flag_service.define_flag(
                session, ctx=ctx, key=payload.key, description=payload.description
            )
        except flag_service.FlagError as exc:
            return _flag_error(exc)
        await session.commit()
        return _flag_out(row)


@router.post("/{key}/rollout")
async def set_feature_flag_rollout(
    request: Request,
    key: str,
    payload: Annotated[RolloutIn, Body()],
) -> Any:
    ctx = _ctx_of(request)
    denial = _gate(request, ctx, Action.FLAG_WRITE, "flag.write")
    if denial is not None:
        return denial

    async with session_scope_with_url(_app_url()) as session:
        await apply_rls_tenant(session, ctx)
        try:
            row = await flag_service.set_rollout(
                session, ctx=ctx, key=key, rollout_percent=payload.rollout_percent
            )
        except flag_service.FlagError as exc:
            return _flag_error(exc)
        await session.commit()
        return _flag_out(row)


@router.post("/{key}/enabled")
async def set_feature_flag_enabled(
    request: Request,
    key: str,
    payload: Annotated[EnabledIn, Body()],
) -> Any:
    ctx = _ctx_of(request)
    denial = _gate(request, ctx, Action.FLAG_WRITE, "flag.write")
    if denial is not None:
        return denial

    async with session_scope_with_url(_app_url()) as session:
        await apply_rls_tenant(session, ctx)
        try:
            row = await flag_service.set_enabled(session, ctx=ctx, key=key, enabled=payload.enabled)
        except flag_service.FlagError as exc:
            return _flag_error(exc)
        await session.commit()
        return _flag_out(row)


@router.post("/{key}/targets")
async def target_feature_flag(
    request: Request,
    key: str,
    payload: Annotated[TargetIn, Body()],
) -> Any:
    ctx = _ctx_of(request)
    denial = _gate(request, ctx, Action.FLAG_WRITE, "flag.write")
    if denial is not None:
        return denial

    try:
        target_id = _uuid(payload.target_tenant_id)
    except flag_service.FlagError as exc:
        return _flag_error(exc)

    async with session_scope_with_url(_app_url()) as session:
        await apply_rls_tenant(session, ctx)
        try:
            row = await flag_service.target_tenant(
                session,
                ctx=ctx,
                key=key,
                tenant_id=target_id,
                enabled=payload.enabled,
            )
        except flag_service.FlagError as exc:
            return _flag_error(exc)
        await session.commit()
        return {
            "flag_id": str(row.flag_id),
            "target_tenant_id": str(row.target_tenant_id),
            "enabled": bool(row.enabled),
        }


def _uuid(raw: str) -> uuid.UUID:
    """Parse a tenant id, mapping malformed input to a flag error.

    A bad id must not reach the database as a cast error: it would surface as
    a 500 rather than a usable message.
    """
    try:
        return uuid.UUID(raw)
    except (ValueError, AttributeError, TypeError) as exc:
        raise flag_service.FlagError("INVALID_TENANT_ID", "not a valid tenant id") from exc


__all__ = ["router"]
