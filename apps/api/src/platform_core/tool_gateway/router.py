"""Tool proposal API (docs/api-contracts.md tool proposal API).

- POST /v1/tool-proposals                     propose a tool call
- POST /v1/tool-proposals/{proposal_id}/confirm   bind a human confirmation
- POST /v1/tool-proposals/{proposal_id}/execute   run it, once, verified

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

from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

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
from platform_policy import Action

router = APIRouter(prefix="/v1/tool-proposals", tags=["tool-gateway"])

# --- Error codes (docs/api-contracts.md stable vocabulary) -----------------

PROPOSAL_NOT_FOUND = "PROPOSAL_NOT_FOUND"
TOOL_EXECUTOR_MISSING = "TOOL_EXECUTOR_MISSING"
TOOL_EXECUTION_ERROR = "TOOL_EXECUTION_ERROR"
TOOL_INPUT_INVALID = "TOOL_ARGS_INVALID"

# Gateway code -> HTTP status. A code absent from this map is a server-side
# programming error and becomes a 500 with a generic code, never a silent
# 200.
_STATUS_BY_CODE: dict[str, int] = {
    "TOOL_NOT_REGISTERED": 404,
    "TOOL_PROHIBITED": 403,
    "TOOL_ARGS_INVALID": 400,
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
RISK_TO_ACTION: dict[str, Action] = {
    ToolRisk.READ.value: Action.TOOL_READ,
    ToolRisk.LOW_WRITE.value: Action.TOOL_WRITE_LOW,
    ToolRisk.CONFIRMED_WRITE.value: Action.TOOL_WRITE_CONFIRMED,
    ToolRisk.HUMAN_APPROVAL.value: Action.TOOL_HUMAN_APPROVAL,
}


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

        gateway = ToolGateway(session, {})
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
