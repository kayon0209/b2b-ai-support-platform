"""Agent directory API: who can take work.

    GET   /v1/agents          list agents (any agent may read)
    GET   /v1/agents/queue    unassigned open cases, oldest first
    POST  /v1/agents          create or update an agent   (admin)
    PATCH /v1/agents/{ref}    change status / skills / capacity  (admin)

Read is `CASE_READ` and write is `TENANT_ADMIN`, for the same reason canned
replies split the two: anyone may *see* who is on shift, but changing who is on
shift (or how much they can carry) changes what the whole queue does next.

`/queue` is declared before `/{user_ref}` on purpose. FastAPI matches routes in
registration order, and `queue` is a valid `{user_ref}` - declared the other way
round, the queue endpoint would be unreachable and would 404 against a path that
very obviously exists.
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
from platform_core.cases.agent_models import MAX_NAME, MAX_REF, AgentStatus
from platform_core.cases.assignment import (
    AssignmentError,
    list_agents,
    queue_cases,
    update_agent,
    upsert_agent,
)
from platform_core.cases.models import Case
from platform_policy import Action

router = APIRouter(prefix="/v1/agents", tags=["agents"])


class AgentIn(BaseModel):
    user_ref: str = Field(min_length=1, max_length=MAX_REF)
    display_name: str = Field(min_length=1, max_length=MAX_NAME)
    skills: list[str] = Field(default_factory=list)
    max_concurrent: int = Field(default=5, ge=1)
    status: str = Field(default=AgentStatus.ACTIVE.value, max_length=15)


class AgentPatchIn(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=MAX_NAME)
    skills: list[str] | None = None
    max_concurrent: int | None = Field(default=None, ge=1)
    status: str | None = Field(default=None, max_length=15)


def _agent_out(row: Any) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "user_ref": row.user_ref,
        "display_name": row.display_name,
        "skills": list(row.skills or []),
        "max_concurrent": int(row.max_concurrent),
        "status": row.status,
    }


def _case_out(row: Case) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "subject": row.subject,
        "status": row.status,
        "priority": row.priority,
        "assignee_ref": row.assignee_ref,
        "opened_at": row.opened_at,
        "sla_tier": row.sla_tier,
    }


def _unresolved() -> Any:
    return error_response(AUTH_UNRESOLVED, "tenant context not resolved", status_code=401)


@router.get("")
async def get_agents(
    request: Request,
    status: str | None = Query(default=AgentStatus.ACTIVE.value, max_length=15),
) -> Any:
    ctx = get_context(request)
    if ctx is None:
        return _unresolved()
    denied = require_policy(ctx, Action.CASE_READ)
    if denied is not None:
        return denied

    async with tenant_session(ctx) as session:
        rows = await list_agents(session, tenant_id=ctx.tenant_id, status=status)
    return {"count": len(rows), "items": [_agent_out(r) for r in rows]}


# Declared before `/{user_ref}` - see the module docstring.
@router.get("/queue")
async def get_work_queue(request: Request, limit: int = Query(default=50, ge=1, le=200)) -> Any:
    """The unassigned queue: the view an agent opens to start work.

    Without this the only way in was knowing a case id, which meant an agent
    could not pick up work they had not already been told about.
    """
    ctx = get_context(request)
    if ctx is None:
        return _unresolved()
    denied = require_policy(ctx, Action.CASE_READ)
    if denied is not None:
        return denied

    async with tenant_session(ctx) as session:
        rows = await queue_cases(session, tenant_id=ctx.tenant_id, limit=limit)
    return {"count": len(rows), "items": [_case_out(r) for r in rows]}


@router.post("")
async def post_agent(request: Request, body: AgentIn) -> Any:
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
            row = await upsert_agent(
                session,
                tenant_id=ctx.tenant_id,
                user_ref=body.user_ref,
                display_name=body.display_name,
                skills=body.skills,
                max_concurrent=body.max_concurrent,
                status=body.status,
            )
            await audit_service.record(
                session,
                ctx=ctx,
                action="agent.directory_changed",
                resource_type="agent_profile",
                resource_id=row.id,
                metadata={
                    "user_ref": row.user_ref,
                    "skills": ",".join(row.skills or []),
                    "max_concurrent": int(row.max_concurrent),
                    "status": row.status,
                },
            )
    except AssignmentError as exc:
        return error_response(VALIDATION_FAILED, str(exc), status_code=400)

    return _agent_out(row)


@router.patch("/{user_ref}")
async def patch_agent(request: Request, user_ref: str, body: AgentPatchIn) -> Any:
    """Change one agent. Only the fields supplied.

    A patch that changes nothing but `status` must not clear `skills`, which is
    why the service takes explicit `None`s and this uses `exclude_unset`.
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

    fields = body.model_dump(exclude_unset=True)
    if not fields:
        return error_response(
            VALIDATION_FAILED, "an update must change at least one field", status_code=400
        )

    try:
        async with tenant_session(ctx) as session:
            row = await update_agent(
                session,
                tenant_id=ctx.tenant_id,
                user_ref=user_ref,
                **fields,
            )
            await audit_service.record(
                session,
                ctx=ctx,
                action="agent.directory_changed",
                resource_type="agent_profile",
                resource_id=row.id,
                metadata={
                    "user_ref": row.user_ref,
                    "fields": ",".join(sorted(fields)),
                    "status": row.status,
                },
            )
    except AssignmentError as exc:
        return error_response(VALIDATION_FAILED, str(exc), status_code=400)

    return _agent_out(row)
