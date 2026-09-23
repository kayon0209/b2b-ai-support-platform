"""A/B experiment API: define an experiment, read what its arms did.

    GET  /v1/quality/experiments          every experiment, with its arms' results
    PUT  /v1/quality/experiments/{key}    define or replace one   (admin)

Reads are `AUDIT_READ` like the rest of `/v1/quality`; writes are `TENANT_ADMIN`,
because starting an experiment changes what a fraction of customers receive.

The results are read from the runs that recorded their arm, never re-derived
from the weights. Re-bucketing history would silently move every past run the
moment someone adjusted a split, so a result you looked at yesterday would
change - and the comparison would be of two different populations rather than
two arms.
"""

import uuid
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
from platform_core.evaluation.ab_service import (
    ExperimentError,
    experiment_results,
    list_experiments,
    upsert_experiment,
)
from platform_core.evaluation.router import MAX_WINDOW_SECONDS
from platform_policy import Action

router = APIRouter(prefix="/v1/quality/experiments", tags=["quality"])


class VariantIn(BaseModel):
    name: str = Field(min_length=1, max_length=63)
    weight: float = Field(default=1.0, gt=0)
    # Omitted means "current behaviour" - the control. Requiring a row for the
    # control would force an operator to publish a duplicate of the live prompt
    # just to compare against it.
    prompt_version_id: uuid.UUID | None = None


class ExperimentIn(BaseModel):
    description: str = Field(default="", max_length=2000)
    variants: list[VariantIn] = Field(min_length=1, max_length=8)
    enabled: bool = False


def _experiment_out(row: Any) -> dict[str, Any]:
    return {
        "key": row.key,
        "description": row.description,
        "variants": row.variants,
        "enabled": bool(row.enabled),
        "updated_at": row.updated_at,
    }


def _results_out(bucket: Any) -> dict[str, Any]:
    return {
        "key": bucket.key,
        "enabled": bucket.enabled,
        "arms": bucket.arms,
    }


@router.get("")
async def get_experiments(
    request: Request,
    window_seconds: int = Query(default=14 * 24 * 3600, ge=60, le=MAX_WINDOW_SECONDS),
) -> Any:
    """Every experiment with what each arm actually did.

    An arm with no runs reports `automation_rate: None`, not `0.0` - "nobody was
    bucketed into it" and "everything in it escalated" are opposite facts, and
    only the second is a reason to stop the experiment.
    """
    ctx = get_context(request)
    if ctx is None:
        return error_response(AUTH_UNRESOLVED, "tenant context not resolved", status_code=401)
    denied = require_policy(ctx, Action.AUDIT_READ)
    if denied is not None:
        return denied

    async with tenant_session(ctx) as session:
        definitions = await list_experiments(session, tenant_id=ctx.tenant_id)
        results = await experiment_results(
            session, tenant_id=ctx.tenant_id, window_seconds=window_seconds
        )
    return {
        "window_seconds": window_seconds,
        "count": len(definitions),
        "experiments": [_experiment_out(r) for r in definitions],
        "results": [_results_out(b) for b in results],
    }


@router.put("/{key}")
async def put_experiment(request: Request, key: str, body: ExperimentIn) -> Any:
    ctx = get_context(request)
    if ctx is None:
        return error_response(AUTH_UNRESOLVED, "tenant context not resolved", status_code=401)
    denied = require_policy(ctx, Action.TENANT_ADMIN)
    if denied is not None:
        return denied
    missing_idem = require_write_idempotency(request, Action.TENANT_ADMIN)
    if missing_idem is not None:
        return missing_idem

    try:
        async with tenant_session(ctx) as session:
            row = await upsert_experiment(
                session,
                tenant_id=ctx.tenant_id,
                key=key,
                variants=[v.model_dump(mode="json") for v in body.variants],
                actor_id=ctx.actor_id,
                description=body.description,
                enabled=body.enabled,
            )
            await audit_service.record(
                session,
                ctx=ctx,
                action="ab_experiment.changed",
                resource_type="ab_experiment",
                resource_id=row.id,
                metadata={
                    "key": row.key,
                    "enabled": bool(row.enabled),
                    "arms": ",".join(str(v.get("name")) for v in (row.variants or [])),
                },
            )
    except ExperimentError as exc:
        return error_response(VALIDATION_FAILED, str(exc), status_code=400)

    return _experiment_out(row)


__all__ = ["router"]
