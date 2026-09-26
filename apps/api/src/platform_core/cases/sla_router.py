"""SLA policy API: tenant-configurable targets.

    GET    /v1/sla-policies          list this tenant's overrides
    GET    /v1/sla-policies/effective  the policy in force for a tier
    PUT    /v1/sla-policies/{tier}   set targets for one tier   (admin)
    DELETE /v1/sla-policies/{tier}   reset that tier to default (admin)

Reads are `CASE_READ` - an agent explaining a deadline to a customer needs to
know what the deadline is. Writes are `TENANT_ADMIN`, because changing a target
changes when every escalation fires, which is a production change with the same
blast radius as a prompt release.

`/effective` exists because the interesting question is not "what rows exist" but
"what clock will this customer get" - and that answer involves the tier
normalisation (inactive contract -> standard) and the fallback, which a client
reimplementing it would get wrong. Declared before `/{tier}` for the same reason
`/queue` is declared before `/{user_ref}` in the agent router: FastAPI matches in
registration order and `effective` is a valid tier name.
"""

from typing import Any

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, Field

from platform_core.api import (
    AUTH_UNRESOLVED,
    VALIDATION_FAILED,
    error_response,
    get_context,
    require_policy,
    require_write_idempotency,
    tenant_session,
)
from platform_core.audit import service as audit_service
from platform_core.cases.models import DEFAULT_SLA
from platform_core.cases.sla_models import MAX_TARGET_MINUTES, MAX_TIER, MIN_TARGET_MINUTES
from platform_core.cases.sla_service import (
    SlaPolicyError,
    effective_tier_key,
    get_sla_policy_row,
    list_sla_policies,
    reset_sla_policy,
    resolve_sla_policy,
    upsert_sla_policy,
)
from platform_policy import Action

router = APIRouter(prefix="/v1/sla-policies", tags=["sla"])


class SlaPolicyIn(BaseModel):
    first_response_minutes: int = Field(ge=MIN_TARGET_MINUTES, le=MAX_TARGET_MINUTES)
    resolution_minutes: int = Field(ge=MIN_TARGET_MINUTES, le=MAX_TARGET_MINUTES)
    # Optional: omitted means "keep the default multipliers".
    priority_multipliers: dict[str, float] | None = None


def _row_out(row: Any) -> dict[str, Any]:
    return {
        "tier": row.tier,
        "first_response_minutes": int(row.first_response_minutes),
        "resolution_minutes": int(row.resolution_minutes),
        "priority_multipliers": row.priority_multipliers,
        "updated_at": row.updated_at,
    }


def _policy_out(policy: Any, *, tier: str, configured: bool) -> dict[str, Any]:
    return {
        "tier": tier,
        "configured": configured,
        "first_response_minutes": int(policy.first_response_minutes),
        "resolution_minutes": int(policy.resolution_minutes),
        "priority_multipliers": dict(policy.priority_multipliers),
        # Named so a client does not have to infer it: `running_states` is
        # deliberately not configurable, and saying so here is cheaper than a
        # support ticket asking why it is missing from the payload.
        "running_states": sorted(s.value for s in policy.running_states),
    }


def _unresolved() -> Any:
    return error_response(AUTH_UNRESOLVED, "tenant context not resolved", status_code=401)


@router.get("")
async def get_sla_policies(request: Request) -> Any:
    ctx = get_context(request)
    if ctx is None:
        return _unresolved()
    denied = require_policy(ctx, Action.CASE_READ)
    if denied is not None:
        return denied

    async with tenant_session(ctx) as session:
        rows = await list_sla_policies(session, tenant_id=ctx.tenant_id)
    return {
        "count": len(rows),
        "items": [_row_out(r) for r in rows],
        # What a tenant with no rows gets. Returned so the admin UI can show
        # the default next to the override rather than leaving a blank.
        "defaults": {
            "first_response_minutes": DEFAULT_SLA.first_response_minutes,
            "resolution_minutes": DEFAULT_SLA.resolution_minutes,
            "priority_multipliers": dict(DEFAULT_SLA.priority_multipliers),
        },
    }


# Declared before `/{tier}` - see the module docstring.
@router.get("/effective")
async def get_effective_policy(
    request: Request,
    tier: str = Query(default="standard", max_length=MAX_TIER),
    contract_status: str = Query(default="active", max_length=31),
) -> Any:
    """The clock this tier actually gets, including the fallback."""
    ctx = get_context(request)
    if ctx is None:
        return _unresolved()
    denied = require_policy(ctx, Action.CASE_READ)
    if denied is not None:
        return denied

    async with tenant_session(ctx) as session:
        key = effective_tier_key(tier, contract_status)
        configured = await get_sla_policy_row(session, tenant_id=ctx.tenant_id, tier=key)
        policy = await resolve_sla_policy(
            session, tenant_id=ctx.tenant_id, tier=tier, contract_status=contract_status
        )
    return _policy_out(policy, tier=key, configured=configured is not None)


@router.put("/{tier}")
async def put_sla_policy(request: Request, tier: str, body: SlaPolicyIn) -> Any:
    ctx = get_context(request)
    if ctx is None:
        return _unresolved()
    denied = require_policy(ctx, Action.TENANT_ADMIN)
    if denied is not None:
        return denied
    missing_idem = require_write_idempotency(request, Action.TENANT_ADMIN)
    if missing_idem is not None:
        return missing_idem

    try:
        async with tenant_session(ctx) as session:
            row = await upsert_sla_policy(
                session,
                tenant_id=ctx.tenant_id,
                tier=tier,
                first_response_minutes=body.first_response_minutes,
                resolution_minutes=body.resolution_minutes,
                actor_id=ctx.actor_id,
                priority_multipliers=body.priority_multipliers,
            )
            await audit_service.record(
                session,
                ctx=ctx,
                action="sla_policy.changed",
                resource_type="sla_policy",
                resource_id=row.id,
                metadata={
                    "tier": row.tier,
                    "first_response_minutes": int(row.first_response_minutes),
                    "resolution_minutes": int(row.resolution_minutes),
                },
            )
    except SlaPolicyError as exc:
        return error_response(VALIDATION_FAILED, str(exc), status_code=400)

    return _row_out(row)


@router.delete("/{tier}")
async def delete_sla_policy(request: Request, tier: str) -> Any:
    """Reset a tier to the code default. Idempotent."""
    ctx = get_context(request)
    if ctx is None:
        return _unresolved()
    denied = require_policy(ctx, Action.TENANT_ADMIN)
    if denied is not None:
        return denied
    missing_idem = require_write_idempotency(request, Action.TENANT_ADMIN)
    if missing_idem is not None:
        return missing_idem

    try:
        async with tenant_session(ctx) as session:
            removed = await reset_sla_policy(session, tenant_id=ctx.tenant_id, tier=tier)
            if removed:
                await audit_service.record(
                    session,
                    ctx=ctx,
                    action="sla_policy.reset",
                    resource_type="sla_policy",
                    metadata={"tier": tier},
                )
    except SlaPolicyError as exc:
        return error_response(VALIDATION_FAILED, str(exc), status_code=400)

    return {"tier": tier, "reset": removed}


__all__ = ["router"]
