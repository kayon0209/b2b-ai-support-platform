"""Case API (docs/api-contracts.md case command API).

- POST /v1/cases                      create
- GET  /v1/cases/{case_id}            read one
- GET  /v1/cases                      list (tenant-scoped, paginated)
- POST /v1/cases/{case_id}/commands   the only way a case changes state

Every command goes through CaseService so transition rules, optimistic
versioning, SLA accrual and the outbox event all happen in one transaction
(see cases/service.py). This router owns HTTP concerns only: policy gates,
payload validation, error mapping and audit records.

Error mapping (stable codes per docs/api-contracts.md):
  LookupError            -> 404 CASE_NOT_FOUND
  TransitionNotAllowed   -> 409 CASE_TRANSITION_NOT_ALLOWED
  VersionConflict        -> 409 CASE_VERSION_CONFLICT
  unknown command        -> 400 VALIDATION_FAILED
"""

from typing import Annotated, Any

from fastapi import APIRouter, Body, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from platform_core.api import (
    CASE_NOT_FOUND,
    CASE_TRANSITION_NOT_ALLOWED,
    CASE_VERSION_CONFLICT,
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
from platform_core.cases.models import (
    Case,
    CaseStatus,
    TransitionNotAllowed,
    VersionConflict,
)
from platform_core.cases.service import CaseService
from platform_core.outbox_service import enqueue
from platform_policy import Action

router = APIRouter(prefix="/v1/cases", tags=["cases"])

VALID_COMMANDS = frozenset({"transition", "change_priority", "assign", "record_first_response"})
VALID_PRIORITIES = frozenset({"p0", "p1", "p2", "p3"})


class CaseCreateIn(BaseModel):
    subject: str = Field(min_length=1, max_length=512)
    description: str = Field(default="", max_length=20_000)
    priority: str = Field(default="p2")
    category: str = Field(default="general", max_length=63)


class CaseCommandIn(BaseModel):
    command: str
    expected_version: int | None = None
    parameters: dict[str, Any] = Field(default_factory=dict)
    reason: str = Field(default="", max_length=500)


def _serialize(case: Case) -> dict[str, Any]:
    return {
        "case_id": str(case.id),
        "subject": case.subject,
        "status": case.status,
        "priority": case.priority,
        "category": case.category,
        "assignee_ref": case.assignee_ref,
        "team_ref": case.team_ref,
        "version": int(case.version),
        "opened_at": case.opened_at,
        "first_response_due_at": case.first_response_due_at,
        "resolution_due_at": case.resolution_due_at,
        "first_responded_at": case.first_responded_at,
        "resolved_at": case.resolved_at,
        "closed_at": case.closed_at,
    }


@router.post("")
async def create_case(request: Request, body: CaseCreateIn) -> Any:
    ctx = get_context(request)
    if ctx is None:
        return error_response("AUTH_UNRESOLVED", "tenant context not resolved", status_code=401)
    denied = require_policy(ctx, Action.CASE_CREATE)
    if denied is not None:
        return denied

    if body.priority not in VALID_PRIORITIES:
        return error_response(
            VALIDATION_FAILED,
            f"priority must be one of {sorted(VALID_PRIORITIES)}",
            status_code=400,
        )

    trace_id = new_trace_id()
    async with tenant_session(ctx) as session:
        service = CaseService(session)
        case = await service.create_case(
            tenant_id=ctx.tenant_id,
            subject=body.subject,
            description=body.description,
            priority=body.priority,
            category=body.category,
        )
        await audit_service.record(
            session,
            ctx=ctx,
            action="case.created",
            resource_type="case",
            resource_id=case.id,
            decision="completed",
            reason_code="OK",
            after={"priority": case.priority, "category": case.category},
            trace_id=trace_id,
        )
        await enqueue(
            session,
            tenant_id=ctx.tenant_id,
            event_type="case.created",
            aggregate_type="case",
            aggregate_id=str(case.id),
            payload={"case_id": str(case.id), "status": case.status},
            trace_id=trace_id,
        )
        payload = _serialize(case)

    return ok_response({"case": payload}, trace_id=trace_id)


@router.get("")
async def list_cases(
    request: Request,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    status: str | None = Query(default=None, max_length=31),
    priority: str | None = Query(default=None, max_length=7),
) -> Any:
    ctx = get_context(request)
    if ctx is None:
        return error_response("AUTH_UNRESOLVED", "tenant context not resolved", status_code=401)
    denied = require_policy(ctx, Action.CASE_READ)
    if denied is not None:
        return denied

    stmt = select(Case).order_by(Case.opened_at.desc(), Case.id)
    if status:
        stmt = stmt.where(Case.status == status)
    if priority:
        stmt = stmt.where(Case.priority == priority)

    async with tenant_session(ctx) as session:
        total = (
            await session.execute(select(func.count()).select_from(stmt.subquery()))
        ).scalar_one()
        rows = (await session.execute(stmt.limit(limit).offset(offset))).scalars().all()
        items = [_serialize(c) for c in rows]

    return ok_response({"items": items, "total": int(total), "limit": limit, "offset": offset})


@router.get("/{case_id}")
async def get_case(request: Request, case_id: str) -> Any:
    ctx = get_context(request)
    if ctx is None:
        return error_response("AUTH_UNRESOLVED", "tenant context not resolved", status_code=401)
    denied = require_policy(ctx, Action.CASE_READ)
    if denied is not None:
        return denied

    try:
        cid = parse_uuid(case_id, field="case_id")
    except ValueError as exc:
        return error_response(VALIDATION_FAILED, str(exc), status_code=400)

    async with tenant_session(ctx) as session:
        # RLS bounds this to the caller's tenant even without the explicit
        # tenant filter; the filter is kept so the intent is readable.
        case = (await session.execute(select(Case).where(Case.id == cid))).scalar_one_or_none()
        if case is None:
            return error_response(CASE_NOT_FOUND, "case not found", status_code=404)
        payload = _serialize(case)

    return ok_response({"case": payload})


@router.post("/{case_id}/commands")
async def apply_case_command(
    request: Request,
    case_id: str,
    body: Annotated[CaseCommandIn, Body()],
) -> Any:
    ctx = get_context(request)
    if ctx is None:
        return error_response("AUTH_UNRESOLVED", "tenant context not resolved", status_code=401)
    denied = require_policy(ctx, Action.CASE_UPDATE)
    if denied is not None:
        return denied

    idem = require_idempotency_key(request)
    if not idem:
        return error_response(
            IDEMPOTENCY_KEY_REQUIRED,
            "every case command must carry an Idempotency-Key header",
            status_code=400,
        )

    try:
        cid = parse_uuid(case_id, field="case_id")
    except ValueError as exc:
        return error_response(VALIDATION_FAILED, str(exc), status_code=400)

    if body.command not in VALID_COMMANDS:
        return error_response(
            VALIDATION_FAILED,
            f"command must be one of {sorted(VALID_COMMANDS)}",
            status_code=400,
        )

    # Validate parameters before touching the database so a malformed
    # command cannot consume a version bump.
    problem = _validate_parameters(body)
    if problem:
        return error_response(VALIDATION_FAILED, problem, status_code=400)

    trace_id = new_trace_id()
    async with tenant_session(ctx) as session:
        service = CaseService(session)
        before_status: str | None = None
        try:
            # Read the prior state for the audit trail's before snapshot.
            prior = (
                await session.execute(select(Case.status, Case.version).where(Case.id == cid))
            ).one_or_none()
            if prior is not None:
                before_status = prior[0]

            case = await service.apply_command(
                tenant_id=ctx.tenant_id,
                case_id=cid,
                command=body.command,
                expected_version=body.expected_version,
                parameters=body.parameters,
            )
        except LookupError:
            return error_response(CASE_NOT_FOUND, "case not found", status_code=404)
        except TransitionNotAllowed as exc:
            await audit_service.record(
                session,
                ctx=ctx,
                action="case.command.rejected",
                resource_type="case",
                resource_id=cid,
                decision="denied",
                reason_code=CASE_TRANSITION_NOT_ALLOWED,
                trace_id=trace_id,
            )
            return error_response(CASE_TRANSITION_NOT_ALLOWED, str(exc), status_code=409)
        except VersionConflict as exc:
            await audit_service.record(
                session,
                ctx=ctx,
                action="case.command.rejected",
                resource_type="case",
                resource_id=cid,
                decision="denied",
                reason_code=CASE_VERSION_CONFLICT,
                trace_id=trace_id,
            )
            return error_response(CASE_VERSION_CONFLICT, str(exc), status_code=409)

        await audit_service.record(
            session,
            ctx=ctx,
            action="case.command.applied",
            resource_type="case",
            resource_id=case.id,
            decision="completed",
            reason_code="OK",
            before={"status": before_status},
            after={"status": case.status, "version": int(case.version), "reason": body.reason},
            trace_id=trace_id,
        )
        await enqueue(
            session,
            tenant_id=ctx.tenant_id,
            event_type="case.updated",
            aggregate_type="case",
            aggregate_id=str(case.id),
            payload={
                "case_id": str(case.id),
                "command": body.command,
                "status": case.status,
                "version": int(case.version),
            },
            trace_id=trace_id,
        )
        payload = _serialize(case)

    return ok_response({"case": payload, "idempotency_key": idem}, trace_id=trace_id)


def _validate_parameters(body: CaseCommandIn) -> str:
    """Return a human-readable problem string, or "" when valid.

    Checked here rather than in the service because it is a request-shape
    concern; the service owns transition and version rules.
    """
    if body.command == "transition":
        target = body.parameters.get("target")
        if not target:
            return "transition requires parameters.target"
        if target not in {s.value for s in CaseStatus}:
            return f"unknown case status: {target}"
    elif body.command == "change_priority":
        priority = body.parameters.get("priority")
        if priority not in VALID_PRIORITIES:
            return f"priority must be one of {sorted(VALID_PRIORITIES)}"
    elif body.command == "assign":
        if not body.parameters.get("assignee_ref") and not body.parameters.get("team_ref"):
            return "assign requires parameters.assignee_ref or parameters.team_ref"
    return ""
