"""Case assignment endpoints: claim and release.

    POST /v1/cases/{case_id}/claim     take this case
    POST /v1/cases/{case_id}/release   give it back to the queue

Both are **commands**, so both need an `Idempotency-Key` - a client retry after
a timeout must not look like a second decision. Both require `CASE_UPDATE`
rather than `CASE_READ`: changing who owns a case is a change to the case.

Why claim/release and not a single `assign`: an agent volunteering is a
different act from a supervisor reassigning, and the existing `/commands`
endpoint already covers `assign` for the supervisor case (with its own
transition rules and version check). Adding a second path to the same state
would give one state two sets of rules. This module owns the queue half only.

Route note: `/{case_id}/claim` is two path segments and cannot collide with the
existing `/{case_id}` in `cases/router.py`, so registration order does not
matter here.
"""

import uuid
from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from platform_core.api import (
    AUTH_UNRESOLVED,
    error_response,
    get_context,
    require_policy,
    require_write_idempotency,
    tenant_session,
)
from platform_core.audit import service as audit_service
from platform_core.cases.assignment import AssignmentError, claim_case, release_case
from platform_core.cases.models import Case
from platform_policy import Action

router = APIRouter(prefix="/v1/cases", tags=["cases"])


class ClaimIn(BaseModel):
    user_ref: str = Field(min_length=1, max_length=255)


def _case_out(row: Case) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "status": row.status,
        "assignee_ref": row.assignee_ref,
        "version": row.version,
    }


def _unresolved() -> Any:
    return error_response(AUTH_UNRESOLVED, "tenant context not resolved", status_code=401)


@router.post("/{case_id}/claim")
async def post_claim_case(request: Request, case_id: uuid.UUID, body: ClaimIn) -> Any:
    """Take a case. Refused when the agent is at capacity."""
    ctx = get_context(request)
    if ctx is None:
        return _unresolved()
    denied = require_policy(ctx, Action.CASE_UPDATE)
    if denied is not None:
        return denied
    missing_idem = require_write_idempotency(request, Action.CASE_UPDATE)
    if missing_idem is not None:
        return missing_idem

    try:
        async with tenant_session(ctx) as session:
            case = await claim_case(
                session,
                tenant_id=ctx.tenant_id,
                case_id=case_id,
                user_ref=body.user_ref,
            )
            await audit_service.record(
                session,
                ctx=ctx,
                action="case.claimed",
                resource_type="case",
                resource_id=case.id,
                metadata={"assignee_ref": body.user_ref},
            )
    except AssignmentError as exc:
        # 409, not 404: "at capacity" and "no such case" are both refusals of
        # the same command, and a client cannot act differently on them anyway.
        return error_response("CASE_CLAIM_REFUSED", str(exc), status_code=409)

    return _case_out(case)


@router.post("/{case_id}/release")
async def post_release_case(request: Request, case_id: uuid.UUID) -> Any:
    """Return a case to the queue. No-op if it is already unassigned."""
    ctx = get_context(request)
    if ctx is None:
        return _unresolved()
    denied = require_policy(ctx, Action.CASE_UPDATE)
    if denied is not None:
        return denied
    missing_idem = require_write_idempotency(request, Action.CASE_UPDATE)
    if missing_idem is not None:
        return missing_idem

    try:
        async with tenant_session(ctx) as session:
            case = await release_case(session, tenant_id=ctx.tenant_id, case_id=case_id)
            await audit_service.record(
                session,
                ctx=ctx,
                action="case.released",
                resource_type="case",
                resource_id=case.id,
            )
    except AssignmentError as exc:
        return error_response("CASE_RELEASE_REFUSED", str(exc), status_code=409)

    return _case_out(case)
