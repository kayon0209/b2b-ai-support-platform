"""Tool proposal API (docs/api-contracts.md tool proposal API).

- POST /v1/tool-proposals                     propose a tool call
- POST /v1/tool-proposals/{proposal_id}/confirm   bind a human confirmation
- POST /v1/tool-proposals/{proposal_id}/execute   run it, once, verified
- GET  /v1/tool-proposals                     list proposals, newest first
- GET  /v1/tool-proposals/{proposal_id}       one proposal with its executions

The gateway (tool_gateway/gateway.py) owns every gate; this router owns
only HTTP concerns. In particular it must NOT re-implement or soften the
confirmation rules: it maps gateway exceptions to the stable codes in
docs/api-contracts.md and returns a truthful `status`.

Truthfulness rules enforced here:
- A proposal response never claims success. `status` is read back from the
  row after the gateway ran, so `unknown` (ambiguous postcondition) is
  reported as `unknown` and never as `executed`.
- The policy decision passed to `propose` is computed here from the
  server-resolved principal and the tool's declared risk class. The caller
  cannot supply `permission_allowed`.
- `execute` requires its own Idempotency-Key on top of the proposal's own
  key, so a retried HTTP call cannot become a second execution.
"""

import time
from typing import Any

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from platform_core.api import (
    IDEMPOTENCY_KEY_REQUIRED,
    VALIDATION_FAILED,
    check_policy,
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
from platform_core.tool_gateway.gateway import (
    ToolGateway,
    ToolGatewayError,
)
from platform_core.tool_gateway.models import (
    ActionConfirmation,
    ProposalStatus,
    ToolDefinition,
    ToolExecution,
    ToolProposal,
    ToolRisk,
)
from platform_core.tool_gateway.registry import RISK_ACTION, resolve_executors
from platform_policy import Action, Decision

router = APIRouter(prefix="/v1/tool-proposals", tags=["tool-gateway"])

# The catalog is a different resource from a proposal, so it gets its own
# prefix rather than a `/v1/tool-proposals/catalog` path that the
# `/{proposal_id}` route would shadow depending on declaration order.
catalog_router = APIRouter(prefix="/v1/tools", tags=["tool-gateway"])

# --- Error codes (docs/api-contracts.md stable vocabulary) -----------------

PROPOSAL_NOT_FOUND = "PROPOSAL_NOT_FOUND"
TOOL_EXECUTOR_MISSING = "TOOL_EXECUTOR_MISSING"
TOOL_EXECUTION_ERROR = "TOOL_EXECUTION_ERROR"
TOOL_ARGS_INVALID = "TOOL_ARGS_INVALID"

# Gateway code -> HTTP status. A code absent from this map is a server-side
# programming error and becomes a 500 with a generic code, never a silent
# 200.
_STATUS_BY_CODE: dict[str, int] = {
    "TOOL_NOT_REGISTERED": 404,
    "TOOL_PROHIBITED": 403,
    TOOL_ARGS_INVALID: 400,
    "PROPOSAL_NOT_FOUND": 404,
    "PROPOSAL_NOT_CONFIRMABLE": 409,
    "PROPOSAL_NOT_EXECUTABLE": 409,
    "PROPOSAL_EXPIRED": 409,
    "CONFIRMATION_REQUIRED": 409,
    "CONFIRMATION_EXPIRED": 409,
    "CONFIRMATION_NOT_REQUIRED": 409,
    "TOOL_EXECUTOR_MISSING": 501,
    "TOOL_EXECUTION_ERROR": 502,
}

# Risk class -> the policy action a caller must hold to propose it.
# Deny-by-default: a risk value with no entry is not proposable.
#
# Derived from the registry's single definition rather than written out here,
# so the agent's write path and this endpoint cannot drift into requiring
# different actions for the same tool.
RISK_TO_ACTION: dict[str, Action] = {risk: Action(value) for risk, value in RISK_ACTION.items()}


class ToolProposeIn(BaseModel):
    tool_name: str = Field(min_length=1, max_length=127)
    arguments: dict[str, Any] = Field(default_factory=dict)
    reason: str = Field(default="", max_length=500)


class ToolProposalCommandIn(BaseModel):
    """Confirmation / execution body.

    `reason` is audit-only. No field here can influence the action hash:
    the hash is bound to the frozen sanitized arguments stored on the
    proposal, so a body that tries to alter arguments is ignored rather
    than re-interpreted.
    """

    reason: str = Field(default="", max_length=500)


def gateway_error_response(exc: ToolGatewayError, *, trace_id: str) -> Any:
    """Map a gateway exception to the documented envelope.

    `ToolDenied` carries a reason code chosen by the gateway (for example
    `CONFIRMATION_REQUIRED`); generic `ToolGatewayError` carries the code
    as its first positional argument. Both are the same taxonomy, so a
    single mapper covers them.
    """
    code = exc.code
    status = _STATUS_BY_CODE.get(code)
    if status is None:
        # Unknown code means the gateway grew a new failure without the
        # contract being updated. Surface it as an internal error instead
        # of guessing a status that could mislead a client.
        return error_response(
            "INTERNAL_ERROR",
            "unmapped tool gateway error",
            status_code=500,
            details={"code": code},
            trace_id=trace_id,
        )
    return error_response(code, str(exc), status_code=status, trace_id=trace_id)


def _serialize_proposal(proposal: ToolProposal, tool: ToolDefinition | None) -> dict[str, Any]:
    return {
        "proposal_id": str(proposal.id),
        "tool_name": tool.name if tool is not None else None,
        "tool_version": tool.version if tool is not None else None,
        "risk": tool.risk if tool is not None else None,
        "status": proposal.status,
        # Computed here rather than only in the list endpoint. It was
        # list-only at first, and the detail view - which reads the same
        # proposal from `GET /{id}` - silently got `undefined`, so both of its
        # action buttons rendered disabled and the screen looked like a
        # permissions problem. Every response that carries a proposal carries
        # this, because every consumer that acts on one needs it.
        "effective_status": _effective_status(proposal, now=int(time.time())),
        # Sanitized only: credential-shaped keys were replaced by the
        # gateway before this row was written.
        "arguments": proposal.sanitized_input,
        "action_hash": proposal.action_hash,
        "permission_decision": proposal.permission_decision,
        "permission_reason": proposal.permission_reason,
        "required_confirmation": proposal.required_confirmation,
        "expires_at": int(proposal.expires_at),
    }


def _serialize_execution(execution: ToolExecution) -> dict[str, Any]:
    return {
        "execution_id": str(execution.id),
        "status": execution.status,
        # `verification_status` is the honest field: transport success is
        # not success. `unknown` means the postcondition could not be
        # determined and must never be rendered as a completed action.
        "verification_status": execution.verification_status,
        "output": execution.sanitized_output,
        "error_code": execution.error_code,
        "started_at": int(execution.started_at),
        "completed_at": int(execution.completed_at) if execution.completed_at else None,
    }


async def _load_tool(session: AsyncSession, tool_definition_id: Any) -> ToolDefinition | None:
    return (
        await session.execute(select(ToolDefinition).where(ToolDefinition.id == tool_definition_id))
    ).scalar_one_or_none()


async def _load_tools(
    session: AsyncSession, tool_definition_ids: list[Any]
) -> dict[Any, ToolDefinition]:
    """Batch-load the definitions behind a page of proposals.

    One query per page rather than one per row: a list endpoint that issues
    N+1 queries is the classic way a console gets slow enough that nobody
    uses it, and this one is the only place a pending write is discoverable.
    """
    if not tool_definition_ids:
        return {}
    rows = (
        (
            await session.execute(
                select(ToolDefinition).where(ToolDefinition.id.in_(set(tool_definition_ids)))
            )
        )
        .scalars()
        .all()
    )
    return {row.id: row for row in rows}


def _effective_status(proposal: ToolProposal, *, now: int) -> str:
    """The status a reader should act on, not the one stored.

    A proposal past its expiry is not `authorized` in any sense a human can
    use: `confirm` and `execute` both refuse it and mark it expired. Reporting
    the stored value would show an operator a pending action that can no
    longer be approved, and they would only discover that by trying.
    """
    if proposal.status == ProposalStatus.AUTHORIZED.value and proposal.expires_at < now:
        return ProposalStatus.EXPIRED.value
    return proposal.status


@router.post("")
async def propose_tool_call(request: Request, body: ToolProposeIn) -> Any:
    """Create a frozen proposal. Proposing never executes anything.

    The permission decision is computed from the server-resolved principal
    and the tool's own risk class, so a caller cannot propose a confirmed
    write while holding only `tool.write.low`.
    """
    ctx = get_context(request)
    if ctx is None:
        return error_response("AUTH_UNRESOLVED", "tenant context not resolved", status_code=401)

    idem = require_idempotency_key(request)
    if not idem:
        return error_response(
            IDEMPOTENCY_KEY_REQUIRED,
            "proposing a tool call requires an Idempotency-Key header",
            status_code=400,
        )

    trace_id = new_trace_id()
    async with tenant_session(ctx) as session:
        # Resolve the tool first: its risk class decides which policy action
        # the caller must hold, so the gate cannot run before this lookup.
        tool = (
            await session.execute(
                select(ToolDefinition)
                .where(
                    (ToolDefinition.tenant_id == ctx.tenant_id)
                    | ToolDefinition.tenant_id.is_(None),
                    ToolDefinition.name == body.tool_name,
                )
                .order_by(ToolDefinition.version.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if tool is None:
            return error_response(
                "TOOL_NOT_REGISTERED",
                f"no tool registered under name {body.tool_name}",
                status_code=404,
                trace_id=trace_id,
            )

        required_action = RISK_TO_ACTION.get(tool.risk)
        if required_action is None:
            # `prohibited` (or an unknown future risk) never reaches the
            # gateway as an unclassified write.
            return error_response(
                "TOOL_PROHIBITED",
                f"tool {body.tool_name} is prohibited",
                status_code=403,
                trace_id=trace_id,
            )

        denied = require_policy(ctx, required_action)
        if denied is not None:
            return denied

        if ctx.actor_id is None:
            # The gateway records actor_id as a non-nullable column; an
            # unattributable write must not be proposed at all.
            return error_response(
                "POLICY_DENIED",
                "principal has no actor id; tool calls must be attributable",
                status_code=403,
            )

        # Proposing never executes, so no executor is resolved here. The
        # adapter is only needed at execute time, and resolving it early
        # would create a connector lookup on the propose path for nothing.
        gateway = ToolGateway(session, {})
        try:
            proposal = await gateway.propose(
                tenant_id=ctx.tenant_id,
                actor_id=ctx.actor_id,
                tool_name=body.tool_name,
                arguments=body.arguments,
                role=ctx.role or "unknown",
                idempotency_key=idem,
                permission_allowed=True,
                required_action=required_action.value,
            )
        except ToolGatewayError as exc:
            await audit_service.record(
                session,
                ctx=ctx,
                action="tool_proposal.rejected",
                resource_type="tool",
                resource_id=tool.id,
                decision="denied",
                reason_code=exc.code,
                trace_id=trace_id,
            )
            return gateway_error_response(exc, trace_id=trace_id)

        await audit_service.record(
            session,
            ctx=ctx,
            action="tool_proposal.created",
            resource_type="tool_proposal",
            resource_id=proposal.id,
            decision="completed",
            reason_code="OK",
            after={
                "tool_name": tool.name,
                "risk": tool.risk,
                "action_hash": proposal.action_hash,
                "required_confirmation": proposal.required_confirmation,
                "reason": body.reason,
            },
            trace_id=trace_id,
        )
        payload = _serialize_proposal(proposal, tool)

    return ok_response({"proposal": payload}, trace_id=trace_id)


@router.post("/{proposal_id}/confirm")
async def confirm_tool_call(request: Request, proposal_id: str) -> Any:
    """Bind a human confirmation to the proposal's exact action hash.

    Only meaningful for tools flagged `required_confirmation`; the gateway
    rejects an unnecessary confirmation rather than storing a meaningless
    approval that would look like diligence in the audit trail.
    """
    ctx = get_context(request)
    if ctx is None:
        return error_response("AUTH_UNRESOLVED", "tenant context not resolved", status_code=401)
    if ctx.actor_id is None:
        return error_response(
            "POLICY_DENIED",
            "principal has no actor id; confirmations must be attributable",
            status_code=403,
        )

    try:
        proposal_uuid = parse_uuid(proposal_id, field="proposal_id")
    except ValueError as exc:
        return error_response(VALIDATION_FAILED, str(exc), status_code=400)

    trace_id = new_trace_id()
    async with tenant_session(ctx) as session:
        # Confirming a write is itself a write; gate it on the case-update
        # permission so a read-only principal cannot approve mutations.
        denied = require_policy(ctx, Action.CASE_UPDATE)
        if denied is not None:
            return denied

        # Confirming records a decision; it does not call the adapter.
        gateway = ToolGateway(session, {})
        try:
            confirmation = await gateway.confirm(
                tenant_id=ctx.tenant_id,
                proposal_id=proposal_uuid,
                actor_id=ctx.actor_id,
            )
        except ToolGatewayError as exc:
            await audit_service.record(
                session,
                ctx=ctx,
                action="tool_confirmation.rejected",
                resource_type="tool_proposal",
                resource_id=proposal_uuid,
                decision="denied",
                reason_code=exc.code,
                trace_id=trace_id,
            )
            return gateway_error_response(exc, trace_id=trace_id)

        proposal = (
            await session.execute(
                select(ToolProposal).where(
                    ToolProposal.tenant_id == ctx.tenant_id,
                    ToolProposal.id == proposal_uuid,
                )
            )
        ).scalar_one()
        tool = await _load_tool(session, proposal.tool_definition_id)

        await audit_service.record(
            session,
            ctx=ctx,
            action="tool_confirmation.created",
            resource_type="tool_proposal",
            resource_id=proposal.id,
            decision="completed",
            reason_code="OK",
            after={
                "action_hash": confirmation.action_hash,
                "confirmed_by": str(confirmation.actor_id),
                "expires_at": int(confirmation.expires_at),
            },
            trace_id=trace_id,
        )
        payload = {
            "proposal": _serialize_proposal(proposal, tool),
            "confirmation": {
                "confirmation_id": str(confirmation.id),
                "action_hash": confirmation.action_hash,
                "scope": confirmation.scope,
                "expires_at": int(confirmation.expires_at),
            },
        }

    return ok_response(payload, trace_id=trace_id)


@router.post("/{proposal_id}/execute")
async def execute_tool_call(request: Request, proposal_id: str, body: ToolProposalCommandIn) -> Any:
    """Execute a confirmed proposal exactly once and verify the outcome.

    A repeat HTTP call returns the existing execution (the unique index on
    `(tenant_id, idempotency_key)` is the hard guarantee), so this endpoint
    is safe to retry.
    """
    ctx = get_context(request)
    if ctx is None:
        return error_response("AUTH_UNRESOLVED", "tenant context not resolved", status_code=401)
    if ctx.actor_id is None:
        return error_response(
            "POLICY_DENIED",
            "principal has no actor id; executions must be attributable",
            status_code=403,
        )

    idem = require_idempotency_key(request)
    if not idem:
        return error_response(
            IDEMPOTENCY_KEY_REQUIRED,
            "executing a tool call requires an Idempotency-Key header",
            status_code=400,
        )

    try:
        proposal_uuid = parse_uuid(proposal_id, field="proposal_id")
    except ValueError as exc:
        return error_response(VALIDATION_FAILED, str(exc), status_code=400)

    trace_id = new_trace_id()
    async with tenant_session(ctx) as session:
        proposal = (
            await session.execute(
                select(ToolProposal).where(
                    ToolProposal.tenant_id == ctx.tenant_id,
                    ToolProposal.id == proposal_uuid,
                )
            )
        ).scalar_one_or_none()
        if proposal is None:
            return error_response(
                PROPOSAL_NOT_FOUND, "proposal not found", status_code=404, trace_id=trace_id
            )

        tool = await _load_tool(session, proposal.tool_definition_id)
        required_action = RISK_TO_ACTION.get(tool.risk) if tool is not None else None
        if required_action is None:
            return error_response(
                "TOOL_PROHIBITED",
                "tool is no longer executable",
                status_code=403,
                trace_id=trace_id,
            )

        # Execution is gated on the same action the proposal required: a
        # caller whose permission was revoked between propose and execute
        # must not be able to finish the write.
        denied = require_policy(ctx, required_action)
        if denied is not None:
            return denied

        # `confirmed_by` is only set when a matching confirmation exists;
        # the gateway re-checks the confirmation itself, so this is audit
        # context rather than the gate.
        confirmed_by = (
            await session.execute(
                select(ActionConfirmation.actor_id).where(
                    ActionConfirmation.proposal_id == proposal.id,
                    ActionConfirmation.action_hash == proposal.action_hash,
                )
            )
        ).scalar_one_or_none()

        # Resolve the executor from this tenant's active connectors. A
        # tenant with no connected system yields no executor, and the
        # gateway reports TOOL_EXECUTOR_MISSING rather than the router
        # inventing a different failure.
        #
        # `ctx` and `trace_id` are passed so that a credential the provider
        # rejects is attributed to this actor in the connector's audit trail
        # and joins the same trace as the call that revealed it.
        executors = await resolve_executors(
            session,
            tenant_id=ctx.tenant_id,
            tool_names=[tool.name] if tool is not None else [],
            ctx=ctx,
            trace_id=trace_id,
        )

        gateway = ToolGateway(session, executors)
        try:
            execution = await gateway.execute(
                tenant_id=ctx.tenant_id,
                actor_id=ctx.actor_id,
                proposal_id=proposal_uuid,
                confirmed_by=confirmed_by,
            )
        except ToolGatewayError as exc:
            await audit_service.record(
                session,
                ctx=ctx,
                action="tool_execution.rejected",
                resource_type="tool_proposal",
                resource_id=proposal.id,
                decision="denied",
                reason_code=exc.code,
                trace_id=trace_id,
            )
            return gateway_error_response(exc, trace_id=trace_id)

        # Re-read the proposal: the gateway advanced its status, and the
        # response must report the post-execution truth, not the pre-call
        # snapshot.
        await session.refresh(proposal)
        await audit_service.record(
            session,
            ctx=ctx,
            action="tool_execution.finished",
            resource_type="tool_proposal",
            resource_id=proposal.id,
            decision=_audit_decision(execution.status),
            reason_code=execution.verification_status or execution.status,
            after={
                "status": execution.status,
                "verification_status": execution.verification_status,
                "reason": body.reason,
            },
            trace_id=trace_id,
        )
        payload = {
            "proposal": _serialize_proposal(proposal, tool),
            "execution": _serialize_execution(execution),
            "idempotency_key": idem,
        }

    return ok_response(payload, trace_id=trace_id)


def _audit_decision(status: str) -> str:
    """Map an execution status onto the audit vocabulary.

    `unknown` is deliberately not `completed`: an ambiguous outcome is a
    distinct audit result so an operator can find every unresolved write.
    """
    if status in (ProposalStatus.EXECUTED.value, ProposalStatus.VERIFIED.value):
        return "completed"
    if status == ProposalStatus.UNKNOWN.value:
        return "unknown"
    return "failed"


@router.get("")
async def list_tool_proposals(
    request: Request,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    status: str | None = Query(default=None, max_length=31),
) -> Any:
    """List proposals for the tenant, newest first.

    This is the carrier the agent's write path needs. The agent can propose a
    confirmed write and then stop, but without a way to enumerate proposals a
    human would have to be told the proposal id out of band to act on it -
    which makes "the agent prepared this, approve it" a notification nobody
    can follow up rather than a workflow.

    `effective_status` is the field a caller should act on; `status` remains
    the stored truth (see `_effective_status`). The `status` *filter* matches
    `effective_status`, so `?status=authorized` means "waiting on a human and
    still approvable" rather than "stored as authorized".
    """
    ctx = get_context(request)
    if ctx is None:
        return error_response("AUTH_UNRESOLVED", "tenant context not resolved", status_code=401)
    denied = require_policy(ctx, Action.CASE_READ)
    if denied is not None:
        return denied

    trace_id = new_trace_id()
    now = int(time.time())
    # UUIDv7 keys sort by creation time, so this is newest-first without a
    # timestamp column to keep in step.
    stmt = select(ToolProposal).order_by(ToolProposal.id.desc())
    if status:
        # The filter matches `effective_status`, not the stored column.
        #
        # Filtering on the stored value made the two disagree in the one place
        # it matters: a console asking for `authorized` (i.e. "what is waiting
        # on me?") was handed proposals whose expiry had already passed, each
        # labelled `expired` in the same response and each of which `confirm`
        # refuses. A filter that returns rows contradicting their own label is
        # worse than no filter.
        if status == ProposalStatus.EXPIRED.value:
            stmt = stmt.where(
                (ToolProposal.status == ProposalStatus.EXPIRED.value)
                | (
                    (ToolProposal.status == ProposalStatus.AUTHORIZED.value)
                    & (ToolProposal.expires_at < now)
                )
            )
        elif status == ProposalStatus.AUTHORIZED.value:
            stmt = stmt.where(
                ToolProposal.status == ProposalStatus.AUTHORIZED.value,
                ToolProposal.expires_at >= now,
            )
        else:
            stmt = stmt.where(ToolProposal.status == status)

    async with tenant_session(ctx) as session:
        total = (
            await session.execute(select(func.count()).select_from(stmt.subquery()))
        ).scalar_one()
        rows = (await session.execute(stmt.limit(limit).offset(offset))).scalars().all()
        tools = await _load_tools(session, [p.tool_definition_id for p in rows])
        items = [_serialize_proposal(p, tools.get(p.tool_definition_id)) for p in rows]

    return ok_response(
        {"items": items, "total": int(total), "limit": limit, "offset": offset},
        trace_id=trace_id,
    )


@router.get("/{proposal_id}")
async def get_tool_proposal(request: Request, proposal_id: str) -> Any:
    """Read one proposal with its executions, newest first.

    Read-only and cheap: the admin UI needs this to explain a blocked write
    ("waiting for confirmation", "verification unknown") without database
    access.
    """
    ctx = get_context(request)
    if ctx is None:
        return error_response("AUTH_UNRESOLVED", "tenant context not resolved", status_code=401)
    denied = require_policy(ctx, Action.CASE_READ)
    if denied is not None:
        return denied

    try:
        proposal_uuid = parse_uuid(proposal_id, field="proposal_id")
    except ValueError as exc:
        return error_response(VALIDATION_FAILED, str(exc), status_code=400)

    async with tenant_session(ctx) as session:
        proposal = (
            await session.execute(
                select(ToolProposal).where(
                    ToolProposal.tenant_id == ctx.tenant_id,
                    ToolProposal.id == proposal_uuid,
                )
            )
        ).scalar_one_or_none()
        if proposal is None:
            return error_response(
                PROPOSAL_NOT_FOUND, "proposal not found", status_code=404, trace_id=new_trace_id()
            )

        tool = await _load_tool(session, proposal.tool_definition_id)
        executions = (
            (
                await session.execute(
                    select(ToolExecution)
                    .where(ToolExecution.proposal_id == proposal.id)
                    .order_by(ToolExecution.id.desc())
                )
            )
            .scalars()
            .all()
        )
        payload = {
            "proposal": _serialize_proposal(proposal, tool),
            "executions": [_serialize_execution(e) for e in executions],
        }

    return ok_response(payload, trace_id=new_trace_id())


# --- Tool catalog (docs/api-contracts.md tool catalog API) -----------------


@catalog_router.get("")
async def list_tools(request: Request) -> Any:
    """The tools this tenant can propose against.

    The console needs this to offer a choice of tool, and it reads the
    *catalog* rather than carrying its own list: a tool added to `TOOL_CATALOG`
    should appear without a front-end change, and one a tenant has disabled
    must disappear. Before this endpoint the Approvals screen could only act on
    proposals that already existed, so a support rep had to reach for `curl` to
    raise one - which is not a workflow.

    A tool the caller cannot propose is omitted rather than listed-and-refused.
    A choice the API will always reject is not a choice, and offering it turns
    the form into an error generator. Two things disqualify a tool: the
    `prohibited` class, and a risk class whose action this principal does not
    hold - `human_approval` is reserved for `tenant_owner`, so a support agent
    must not be shown the EQ confirmation as an option.
    """
    ctx = get_context(request)
    if ctx is None:
        return error_response("AUTH_UNRESOLVED", "tenant context not resolved", status_code=401)
    denied = require_policy(ctx, Action.TOOL_READ)
    if denied is not None:
        return denied

    async with tenant_session(ctx) as session:
        rows = (
            (
                await session.execute(
                    select(ToolDefinition)
                    .where(
                        (ToolDefinition.tenant_id == ctx.tenant_id)
                        | ToolDefinition.tenant_id.is_(None),
                        ToolDefinition.risk != ToolRisk.PROHIBITED.value,
                    )
                    # Highest version first so the dedupe below keeps the one
                    # the propose path would actually resolve: a tenant override
                    # outranks the global row of the same name.
                    .order_by(ToolDefinition.name, ToolDefinition.version.desc())
                )
            )
            .scalars()
            .all()
        )

    seen: set[str] = set()
    items: list[dict[str, Any]] = []
    for row in rows:
        if row.name in seen:
            continue
        seen.add(row.name)
        required = RISK_ACTION.get(row.risk)
        if required is None or check_policy(ctx, Action(required)) != Decision.ALLOW:
            # Same rule as `prohibited`: the propose call would be refused, so
            # the choice is not a choice.
            continue
        items.append(
            {
                "name": row.name,
                "version": row.version,
                "risk": row.risk,
                "requires_confirmation": row.requires_confirmation,
                "input_schema": row.input_schema,
                "tenant_scoped": row.tenant_id is not None,
            }
        )

    return ok_response({"items": items, "total": len(items)}, trace_id=new_trace_id())
