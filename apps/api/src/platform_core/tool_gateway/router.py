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

import hashlib
import json
import time
import uuid
from dataclasses import replace
from typing import Any, Literal

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from observability_metrics import get_metrics
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
from platform_core.identity.repository import lock_active_membership_role
from platform_core.identity.tenant_context import TenantContext
from platform_core.outbox_service import enqueue
from platform_core.tool_gateway.gateway import (
    ToolGateway,
    ToolGatewayError,
    compute_action_hash,
    sanitize_arguments,
    validate_against_schema,
)
from platform_core.tool_gateway.models import (
    ActionConfirmation,
    ProposalStatus,
    ToolDefinition,
    ToolExecution,
    ToolExecutionCompensation,
    ToolExecutionReconciliation,
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
    "ACTOR_MEMBERSHIP_INACTIVE": 403,
    "ACTOR_PERMISSION_REVOKED": 403,
    "AUTHORIZATION_STATE_CHANGED": 409,
    "COMPENSATION_NOT_SUPPORTED": 409,
    "COMPENSATION_EXECUTION_NOT_VERIFIED": 409,
    "COMPENSATION_TARGET_CHANGED": 409,
    "COMPENSATION_CASE_NOT_FOUND": 409,
    "TOOL_EXECUTION_IN_PROGRESS": 409,
    "TOOL_EXECUTION_OUTCOME_UNKNOWN": 409,
    "TOOL_EXECUTION_NOT_RECONCILABLE": 409,
    "TOOL_EXECUTION_ALREADY_RESOLVED": 409,
    "TASK_DEPENDENCY_BLOCKED": 409,
    "TASK_LEASE_STALE": 409,
    "TASK_LEASE_NOT_OWNED": 409,
    "TASK_LEASE_MISSING": 409,
    "TASK_LEASE_EXPIRED": 409,
    "TASK_HUMAN_OWNERSHIP_REQUIRED": 409,
    "TASK_PROPOSAL_NOT_PENDING": 409,
    "IDEMPOTENCY_CONFLICT": 409,
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


class ToolExecutionReconcileIn(BaseModel):
    model_config = {"extra": "forbid"}

    decision: Literal["applied", "not_applied", "unresolved"]
    # Store a provider record/search reference, never a pasted document or
    # arbitrary free-form explanation.
    evidence_reference: str = Field(
        min_length=1,
        max_length=255,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]*$",
    )


class ToolExecutionCompensateIn(BaseModel):
    model_config = {"extra": "forbid"}

    reason_code: Literal["created_in_error", "duplicate_case", "incorrect_customer"]


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


def _serialize_reconciliation(row: ToolExecutionReconciliation) -> dict[str, Any]:
    return {
        "reconciliation_id": str(row.id),
        "execution_id": str(row.execution_id),
        "decision": row.decision,
        "evidence_reference": row.evidence_reference,
        "actor_id": str(row.actor_id),
        "created_at": int(row.created_at),
    }


def _serialize_compensation(row: ToolExecutionCompensation) -> dict[str, Any]:
    return {
        "compensation_id": str(row.id),
        "execution_id": str(row.execution_id),
        "action": row.action,
        "outcome": row.outcome,
        "reason_code": row.reason_code,
        "case_id": str(row.case_id) if row.case_id else None,
        "result": row.result,
        "created_at": int(row.created_at),
    }


async def _load_tool(session: AsyncSession, tool_definition_id: Any) -> ToolDefinition | None:
    return (
        await session.execute(select(ToolDefinition).where(ToolDefinition.id == tool_definition_id))
    ).scalar_one_or_none()


async def _live_membership_permission_error(
    session: AsyncSession, *, ctx: TenantContext, action: Action
) -> ToolGatewayError | None:
    """Lock current membership state before approval or execution is committed.

    The membership row lock serializes a permission change with the durable
    ToolExecution intent. Under REPEATABLE READ, a concurrent membership
    update may instead invalidate this transaction's snapshot; that also
    fails closed and asks the caller to retry with freshly resolved identity.
    """
    if ctx.actor_id is None:
        return ToolGatewayError("ACTOR_MEMBERSHIP_INACTIVE")
    try:
        # Use a savepoint so a serialization failure does not poison the
        # surrounding request transaction before it can record a denial.
        async with session.begin_nested():
            role = await lock_active_membership_role(
                session,
                tenant_id=ctx.tenant_id,
                user_id=ctx.actor_id,
            )
    except DBAPIError as exc:
        original = getattr(exc, "orig", None)
        sqlstate = getattr(original, "sqlstate", None) or getattr(original, "pgcode", None)
        if sqlstate == "40001":
            return ToolGatewayError("AUTHORIZATION_STATE_CHANGED")
        raise
    if role is None:
        return ToolGatewayError("ACTOR_MEMBERSHIP_INACTIVE")
    if check_policy(replace(ctx, role=role), action) != Decision.ALLOW:
        return ToolGatewayError("ACTOR_PERMISSION_REVOKED")
    return None


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

        # The proposal row is the durable confirmation target, so duplicate
        # submissions must resolve to one frozen action. The transaction lock
        # closes the select-then-insert race across API processes without a
        # migration; the scope is tenant + caller-provided idempotency key.
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:scope, 0))"),
            {"scope": f"tool-proposal:{ctx.tenant_id}:{idem}"},
        )
        existing = (
            await session.execute(
                select(ToolProposal)
                .where(
                    ToolProposal.tenant_id == ctx.tenant_id,
                    ToolProposal.idempotency_key == idem,
                )
                .order_by(ToolProposal.id.desc())
                .limit(1)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if existing is not None:
            try:
                validate_against_schema(body.arguments, tool.input_schema)
            except ToolGatewayError as exc:
                return gateway_error_response(exc, trace_id=trace_id)
            expected_hash = compute_action_hash(
                tool.name,
                tool.version,
                sanitize_arguments(body.arguments),
            )
            if existing.tool_definition_id != tool.id or existing.action_hash != expected_hash:
                await audit_service.record(
                    session,
                    ctx=ctx,
                    action="tool_proposal.idempotency_conflict",
                    resource_type="tool_proposal",
                    resource_id=existing.id,
                    decision="denied",
                    reason_code="IDEMPOTENCY_CONFLICT",
                    trace_id=trace_id,
                )
                return error_response(
                    "IDEMPOTENCY_CONFLICT",
                    "Idempotency-Key was already used for a different tool action",
                    status_code=409,
                    trace_id=trace_id,
                )
            await audit_service.record(
                session,
                ctx=ctx,
                action="tool_proposal.replayed",
                resource_type="tool_proposal",
                resource_id=existing.id,
                decision="completed",
                reason_code="IDEMPOTENT_REPLAY",
                trace_id=trace_id,
            )
            return ok_response(
                {"proposal": _serialize_proposal(existing, tool), "replayed": True},
                trace_id=trace_id,
            )

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

    return ok_response({"proposal": payload, "replayed": False}, trace_id=trace_id)


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
            from platform_core.agent_runtime.tasks.gateway_lifecycle import (
                lock_task_proposal_lease,
            )

            lease_blocker = await lock_task_proposal_lease(
                session,
                tenant_id=ctx.tenant_id,
                proposal_id=proposal_uuid,
            )
            if lease_blocker is not None:
                raise ToolGatewayError("TASK_LEASE_STALE", lease_blocker)
            membership_error = await _live_membership_permission_error(
                session, ctx=ctx, action=Action.CASE_UPDATE
            )
            if membership_error is not None:
                raise membership_error
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

        # Preserve idempotent receipt replays after completion, but bind any
        # new external side effect to the linked task's current human lease.
        existing_execution_status = (
            await session.execute(
                select(ToolExecution.status).where(
                    ToolExecution.tenant_id == ctx.tenant_id,
                    ToolExecution.idempotency_key == proposal.idempotency_key,
                )
            )
        ).scalar_one_or_none()
        if (
            existing_execution_status is None
            or existing_execution_status == ProposalStatus.EXECUTING.value
        ):
            from platform_core.agent_runtime.tasks.gateway_lifecycle import (
                lock_task_proposal_lease,
            )

            lease_blocker = await lock_task_proposal_lease(
                session,
                tenant_id=ctx.tenant_id,
                proposal_id=proposal.id,
            )
            if lease_blocker is not None:
                await audit_service.record(
                    session,
                    ctx=ctx,
                    action="tool_execution.rejected",
                    resource_type="tool_proposal",
                    resource_id=proposal.id,
                    decision="denied",
                    reason_code=lease_blocker,
                    trace_id=trace_id,
                )
                return gateway_error_response(
                    ToolGatewayError("TASK_LEASE_STALE", lease_blocker), trace_id=trace_id
                )

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

        async def check_task_precondition(
            linked_proposal: ToolProposal, linked_tool: ToolDefinition
        ) -> None:
            current_permission = await _live_membership_permission_error(
                session,
                ctx=ctx,
                action=RISK_TO_ACTION[linked_tool.risk],
            )
            if current_permission is not None:
                raise current_permission
            from platform_core.agent_runtime.tasks.dependencies import (
                check_live_dependent_write_precondition,
            )

            blocker = await check_live_dependent_write_precondition(
                session,
                tenant_context=ctx,
                write_proposal_id=linked_proposal.id,
                write_tool_name=linked_tool.name,
                request_idempotency_key=idem,
                trace_id=trace_id,
            )
            if blocker is not None:
                raise ToolGatewayError("TASK_DEPENDENCY_BLOCKED", blocker)

        try:
            execution = await gateway.execute(
                tenant_id=ctx.tenant_id,
                actor_id=ctx.actor_id,
                proposal_id=proposal_uuid,
                confirmed_by=confirmed_by,
                pre_execution_check=check_task_precondition,
            )
        except ToolGatewayError as exc:
            failed_execution = (
                await session.execute(
                    select(ToolExecution)
                    .where(
                        ToolExecution.tenant_id == ctx.tenant_id,
                        ToolExecution.proposal_id == proposal.id,
                    )
                    .order_by(ToolExecution.started_at.desc(), ToolExecution.id.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            if failed_execution is not None:
                await _sync_linked_task_execution(
                    session,
                    ctx=ctx,
                    proposal_id=proposal.id,
                    execution=failed_execution,
                    trace_id=trace_id,
                )
            await audit_service.record(
                session,
                ctx=ctx,
                action="tool_execution.rejected",
                resource_type="tool_proposal",
                resource_id=proposal.id,
                decision=(
                    "unknown"
                    if exc.code in {"TOOL_EXECUTION_IN_PROGRESS", "TOOL_EXECUTION_OUTCOME_UNKNOWN"}
                    else "denied"
                ),
                reason_code=exc.code,
                trace_id=trace_id,
            )
            return gateway_error_response(exc, trace_id=trace_id)

        # Re-read the proposal: the gateway advanced its status, and the
        # response must report the post-execution truth, not the pre-call
        # snapshot.
        await session.refresh(proposal)
        await _sync_linked_task_execution(
            session,
            ctx=ctx,
            proposal_id=proposal.id,
            execution=execution,
            trace_id=trace_id,
        )
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


@router.post("/{proposal_id}/compensate")
async def compensate_tool_execution(
    request: Request, proposal_id: str, body: ToolExecutionCompensateIn
) -> Any:
    """Apply the one qualified business compensator: close an untouched Case.

    External provider writes, notifications, and release confirmations do not
    have a generic reverse path. This endpoint preserves the original Case and
    only closes it when the domain service proves it is still NEW/version 1.
    """
    ctx = get_context(request)
    if ctx is None:
        return error_response("AUTH_UNRESOLVED", "tenant context not resolved", status_code=401)
    if ctx.actor_id is None or ctx.actor_kind != "user":
        return error_response(
            "POLICY_DENIED",
            "business compensation must be attributable to a human actor",
            status_code=403,
        )
    denied = require_policy(ctx, Action.CASE_CLOSE)
    if denied is not None:
        return denied
    idem = require_idempotency_key(request)
    if not idem:
        return error_response(
            IDEMPOTENCY_KEY_REQUIRED,
            "compensating a tool execution requires an Idempotency-Key",
            status_code=400,
        )
    try:
        proposal_uuid = parse_uuid(proposal_id, field="proposal_id")
    except ValueError as exc:
        return error_response(VALIDATION_FAILED, str(exc), status_code=400)

    idem_hash = hashlib.sha256(idem.encode("utf-8")).hexdigest()
    request_hash = hashlib.sha256(
        json.dumps(
            {"proposal_id": str(proposal_uuid), "reason_code": body.reason_code},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    trace_id = new_trace_id()
    now = int(time.time())

    async with tenant_session(ctx) as session:
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
            {"lock_key": f"{ctx.tenant_id}:compensation:{idem_hash}"},
        )
        prior = (
            await session.execute(
                select(ToolExecutionCompensation).where(
                    ToolExecutionCompensation.tenant_id == ctx.tenant_id,
                    ToolExecutionCompensation.idempotency_key_hash == idem_hash,
                )
            )
        ).scalar_one_or_none()
        if prior is not None:
            if prior.request_hash != request_hash:
                return error_response(
                    "IDEMPOTENCY_CONFLICT",
                    "Idempotency-Key was already used for another compensation request",
                    status_code=409,
                    trace_id=trace_id,
                )
            membership_error = await _live_membership_permission_error(
                session, ctx=ctx, action=Action.CASE_CLOSE
            )
            if membership_error is not None:
                return gateway_error_response(membership_error, trace_id=trace_id)
            return ok_response(
                {"compensation": _serialize_compensation(prior), "replayed": True},
                trace_id=trace_id,
            )

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
        if tool is None:
            return error_response(
                "TOOL_NOT_REGISTERED",
                "tool definition is missing",
                status_code=404,
                trace_id=trace_id,
            )
        if tool.name != "case.create":
            return gateway_error_response(
                ToolGatewayError("COMPENSATION_NOT_SUPPORTED"), trace_id=trace_id
            )

        from platform_core.agent_runtime.tasks.gateway_lifecycle import (
            lock_task_proposal_reconciliation_owner,
        )

        lease_blocker = await lock_task_proposal_reconciliation_owner(
            session,
            tenant_id=ctx.tenant_id,
            proposal_id=proposal.id,
            actor_id=ctx.actor_id,
            now=now,
        )
        if lease_blocker is not None:
            return gateway_error_response(ToolGatewayError(lease_blocker), trace_id=trace_id)
        membership_error = await _live_membership_permission_error(
            session, ctx=ctx, action=Action.CASE_CLOSE
        )
        if membership_error is not None:
            await audit_service.record(
                session,
                ctx=ctx,
                action="tool_execution.compensation_rejected",
                resource_type="tool_proposal",
                resource_id=proposal.id,
                decision="denied",
                reason_code=membership_error.code,
                trace_id=trace_id,
            )
            return gateway_error_response(membership_error, trace_id=trace_id)

        proposal = (
            await session.execute(
                select(ToolProposal)
                .where(
                    ToolProposal.tenant_id == ctx.tenant_id,
                    ToolProposal.id == proposal_uuid,
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one()
        execution = (
            await session.execute(
                select(ToolExecution)
                .where(
                    ToolExecution.tenant_id == ctx.tenant_id,
                    ToolExecution.proposal_id == proposal.id,
                )
                .order_by(ToolExecution.started_at.desc(), ToolExecution.id.desc())
                .limit(1)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if (
            execution is None
            or execution.status != ProposalStatus.EXECUTED.value
            or execution.verification_status != "verified"
            or not isinstance(execution.sanitized_output, dict)
        ):
            return gateway_error_response(
                ToolGatewayError("COMPENSATION_EXECUTION_NOT_VERIFIED"), trace_id=trace_id
            )
        try:
            case_id = uuid.UUID(str(execution.sanitized_output["case_id"]))
        except (KeyError, ValueError, TypeError):
            return gateway_error_response(
                ToolGatewayError("COMPENSATION_EXECUTION_NOT_VERIFIED"), trace_id=trace_id
            )

        from platform_core.cases.service import CaseError, compensate_unmodified_tool_created_case

        error_code: str | None = None
        case_result: dict[str, Any] = {}
        try:
            async with session.begin_nested():
                case_result = await compensate_unmodified_tool_created_case(
                    session,
                    tenant_id=ctx.tenant_id,
                    case_id=case_id,
                    reason_code=body.reason_code,
                )
        except CaseError as exc:
            error_code = exc.code
        except DBAPIError as exc:
            original = getattr(exc, "orig", None)
            sqlstate = getattr(original, "sqlstate", None) or getattr(original, "pgcode", None)
            if sqlstate == "40001":
                error_code = "COMPENSATION_TARGET_CHANGED"
            else:
                raise

        outcome = "failed" if error_code is not None else "succeeded"
        action = "case_create_close_unmodified"
        compensation = ToolExecutionCompensation(
            tenant_id=ctx.tenant_id,
            execution_id=execution.id,
            proposal_id=proposal.id,
            actor_id=ctx.actor_id,
            idempotency_key_hash=idem_hash,
            request_hash=request_hash,
            action=action,
            outcome=outcome,
            reason_code=body.reason_code,
            case_id=case_id,
            result={"error_code": error_code} if error_code else case_result,
            created_at=now,
        )
        session.add(compensation)
        await session.flush()
        if error_code is None:
            await enqueue(
                session,
                tenant_id=ctx.tenant_id,
                event_type="case.updated",
                aggregate_type="case",
                aggregate_id=str(case_id),
                payload={
                    "case_id": str(case_id),
                    "command": "compensate_case_create",
                    "status": case_result["status"],
                    "version": case_result["version"],
                },
                trace_id=trace_id,
            )
        await audit_service.record(
            session,
            ctx=ctx,
            action=(
                "tool_execution.compensated"
                if error_code is None
                else "tool_execution.compensation_failed"
            ),
            resource_type="tool_execution",
            resource_id=execution.id,
            decision=outcome,
            reason_code=error_code or body.reason_code.upper(),
            metadata={
                "proposal_id": str(proposal.id),
                "compensation_id": str(compensation.id),
                "action": action,
                "case_id": str(case_id),
                "reason_code": body.reason_code,
                "result": compensation.result,
            },
            trace_id=trace_id,
        )
        get_metrics().tool_compensations_total.labels(action=action, outcome=outcome).inc()
        return ok_response(
            {"compensation": _serialize_compensation(compensation), "replayed": False},
            trace_id=trace_id,
        )


@router.post("/{proposal_id}/reconcile")
async def reconcile_tool_execution(
    request: Request, proposal_id: str, body: ToolExecutionReconcileIn
) -> Any:
    """Record an authorized human lookup for an ambiguous provider outcome.

    This endpoint never invokes the executor. An unresolved or lost-ack write
    remains fenced by its original execution idempotency key; a verified
    no-effect decision is terminal for that attempt and needs a new proposal
    and confirmation before any later write.
    """
    ctx = get_context(request)
    if ctx is None:
        return error_response("AUTH_UNRESOLVED", "tenant context not resolved", status_code=401)
    if ctx.actor_id is None:
        return error_response(
            "POLICY_DENIED",
            "principal has no actor id; reconciliation must be attributable",
            status_code=403,
        )
    denied = require_policy(ctx, Action.CASE_UPDATE)
    if denied is not None:
        return denied
    idem = require_idempotency_key(request)
    if not idem:
        return error_response(
            IDEMPOTENCY_KEY_REQUIRED,
            "reconciling a tool execution requires an Idempotency-Key",
            status_code=400,
        )
    try:
        proposal_uuid = parse_uuid(proposal_id, field="proposal_id")
    except ValueError as exc:
        return error_response(VALIDATION_FAILED, str(exc), status_code=400)

    idem_hash = hashlib.sha256(idem.encode("utf-8")).hexdigest()
    request_hash = hashlib.sha256(
        json.dumps(
            {
                "proposal_id": str(proposal_uuid),
                "decision": body.decision,
                "evidence_reference": body.evidence_reference,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    trace_id = new_trace_id()
    now = int(time.time())

    async with tenant_session(ctx) as session:
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
            {"lock_key": f"{ctx.tenant_id}:{idem_hash}"},
        )
        prior = (
            await session.execute(
                select(ToolExecutionReconciliation).where(
                    ToolExecutionReconciliation.tenant_id == ctx.tenant_id,
                    ToolExecutionReconciliation.idempotency_key_hash == idem_hash,
                )
            )
        ).scalar_one_or_none()
        if prior is not None:
            if prior.request_hash != request_hash:
                return error_response(
                    "IDEMPOTENCY_CONFLICT",
                    "Idempotency-Key was already used for a different reconciliation",
                    status_code=409,
                    trace_id=trace_id,
                )
            proposal = (
                await session.execute(
                    select(ToolProposal).where(
                        ToolProposal.tenant_id == ctx.tenant_id,
                        ToolProposal.id == prior.proposal_id,
                    )
                )
            ).scalar_one_or_none()
            execution = (
                await session.execute(
                    select(ToolExecution).where(
                        ToolExecution.tenant_id == ctx.tenant_id,
                        ToolExecution.id == prior.execution_id,
                        ToolExecution.proposal_id == prior.proposal_id,
                    )
                )
            ).scalar_one_or_none()
            if proposal is None or execution is None:
                return error_response(
                    PROPOSAL_NOT_FOUND,
                    "the reconciled execution is no longer available",
                    status_code=404,
                    trace_id=trace_id,
                )
            tool = await _load_tool(session, proposal.tool_definition_id)
            if tool is None:
                return error_response(
                    "TOOL_NOT_REGISTERED",
                    "tool definition is missing",
                    status_code=404,
                    trace_id=trace_id,
                )
            required_action = RISK_TO_ACTION.get(tool.risk)
            if required_action is None:
                return error_response(
                    "TOOL_PROHIBITED",
                    "tool is no longer executable",
                    status_code=403,
                    trace_id=trace_id,
                )
            denied = require_policy(ctx, required_action)
            if denied is not None:
                return denied
            return ok_response(
                {
                    "proposal": _serialize_proposal(proposal, tool),
                    "execution": _serialize_execution(execution),
                    "reconciliation": _serialize_reconciliation(prior),
                    "replayed": True,
                },
                trace_id=trace_id,
            )

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
        if tool is None:
            return error_response(
                "TOOL_NOT_REGISTERED",
                "tool definition is missing",
                status_code=404,
                trace_id=trace_id,
            )
        required_action = RISK_TO_ACTION.get(tool.risk)
        if required_action is None:
            return error_response(
                "TOOL_PROHIBITED",
                "tool is no longer executable",
                status_code=403,
                trace_id=trace_id,
            )
        denied = require_policy(ctx, required_action)
        if denied is not None:
            return denied

        from platform_core.agent_runtime.tasks.gateway_lifecycle import (
            lock_task_proposal_reconciliation_owner,
        )

        lease_blocker = await lock_task_proposal_reconciliation_owner(
            session,
            tenant_id=ctx.tenant_id,
            proposal_id=proposal.id,
            actor_id=ctx.actor_id,
            now=now,
        )
        if lease_blocker is not None:
            return gateway_error_response(ToolGatewayError(lease_blocker), trace_id=trace_id)

        membership_error = await _live_membership_permission_error(
            session, ctx=ctx, action=Action.CASE_UPDATE
        )
        if membership_error is None:
            membership_error = await _live_membership_permission_error(
                session, ctx=ctx, action=required_action
            )
        if membership_error is not None:
            await audit_service.record(
                session,
                ctx=ctx,
                action="tool_execution.reconciliation_rejected",
                resource_type="tool_proposal",
                resource_id=proposal.id,
                decision="denied",
                reason_code=membership_error.code,
                trace_id=trace_id,
            )
            return gateway_error_response(membership_error, trace_id=trace_id)

        proposal = (
            await session.execute(
                select(ToolProposal)
                .where(
                    ToolProposal.tenant_id == ctx.tenant_id,
                    ToolProposal.id == proposal_uuid,
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one()
        execution = (
            await session.execute(
                select(ToolExecution)
                .where(
                    ToolExecution.tenant_id == ctx.tenant_id,
                    ToolExecution.proposal_id == proposal.id,
                )
                .order_by(ToolExecution.started_at.desc(), ToolExecution.id.desc())
                .limit(1)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if execution is None:
            return error_response(
                "TOOL_EXECUTION_NOT_RECONCILABLE",
                "no persisted execution exists for this proposal",
                status_code=409,
                trace_id=trace_id,
            )
        previous_status = execution.status
        if execution.status == ProposalStatus.EXECUTING.value:
            recovery_after = execution.started_at + max(60, int(tool.timeout_ms / 1000) + 60)
            if now < recovery_after:
                return error_response(
                    "TOOL_EXECUTION_IN_PROGRESS",
                    "the execution is still inside its bounded provider window",
                    status_code=409,
                    trace_id=trace_id,
                )
            execution.status = ProposalStatus.UNKNOWN.value
            execution.verification_status = "unknown"
            execution.completed_at = now
            execution.error_code = "EXECUTION_WORKER_LOST"
            proposal.status = ProposalStatus.UNKNOWN.value
        elif execution.status != ProposalStatus.UNKNOWN.value:
            return error_response(
                "TOOL_EXECUTION_ALREADY_RESOLVED",
                "only an unresolved execution can be reconciled",
                status_code=409,
                trace_id=trace_id,
            )

        reconciliation = ToolExecutionReconciliation(
            tenant_id=ctx.tenant_id,
            execution_id=execution.id,
            proposal_id=proposal.id,
            actor_id=ctx.actor_id,
            idempotency_key_hash=idem_hash,
            request_hash=request_hash,
            decision=body.decision,
            evidence_reference=body.evidence_reference,
            created_at=now,
        )
        session.add(reconciliation)
        reconciliation_output = {
            "method": "human_provider_lookup",
            "decision": body.decision,
            "evidence_reference": body.evidence_reference,
            "recorded_at": now,
        }
        current_output = (
            execution.sanitized_output if isinstance(execution.sanitized_output, dict) else {}
        )
        execution.sanitized_output = sanitize_arguments(
            {**current_output, "reconciliation": reconciliation_output}
        )
        execution.completed_at = now
        if body.decision == "applied":
            execution.status = ProposalStatus.EXECUTED.value
            execution.verification_status = "verified"
            execution.error_code = None
            proposal.status = ProposalStatus.VERIFIED.value
        elif body.decision == "not_applied":
            execution.status = ProposalStatus.FAILED.value
            execution.verification_status = "failed"
            execution.error_code = "RECONCILED_NOT_APPLIED"
            proposal.status = ProposalStatus.FAILED.value
        else:
            execution.status = ProposalStatus.UNKNOWN.value
            execution.verification_status = "unknown"
            execution.error_code = "RECONCILIATION_UNRESOLVED"
            proposal.status = ProposalStatus.UNKNOWN.value
        await session.flush()

        await _sync_linked_task_execution(
            session,
            ctx=ctx,
            proposal_id=proposal.id,
            execution=execution,
            trace_id=trace_id,
        )
        await audit_service.record(
            session,
            ctx=ctx,
            action="tool_execution.reconciled",
            resource_type="tool_execution",
            resource_id=execution.id,
            decision=body.decision,
            reason_code=f"RECONCILED_{body.decision.upper()}",
            metadata={
                "proposal_id": str(proposal.id),
                "idempotency_key_hash": idem_hash,
                "evidence_reference": body.evidence_reference,
                "previous_status": previous_status,
                "status": execution.status,
                "reconciliation_id": str(reconciliation.id),
            },
            trace_id=trace_id,
        )
        return ok_response(
            {
                "proposal": _serialize_proposal(proposal, tool),
                "execution": _serialize_execution(execution),
                "reconciliation": _serialize_reconciliation(reconciliation),
                "replayed": False,
            },
            trace_id=trace_id,
        )


async def _sync_linked_task_execution(
    session: AsyncSession,
    *,
    ctx: Any,
    proposal_id: uuid.UUID,
    execution: ToolExecution,
    trace_id: str,
) -> None:
    """Project a Gateway receipt into its task within the same transaction."""
    from platform_core.agent_runtime.tasks.gateway_lifecycle import record_execution_result

    task = await record_execution_result(
        session,
        tenant_id=ctx.tenant_id,
        proposal_id=proposal_id,
        execution_id=execution.id,
        execution_status=execution.status,
        verification_status=execution.verification_status,
        trace_id=trace_id,
    )
    if task is None:
        return
    await audit_service.record(
        session,
        ctx=ctx,
        action="conversation.task_gateway_execution",
        resource_type="conversation_task",
        resource_id=task.id,
        reason_code=task.status,
        metadata={
            "proposal_id": str(proposal_id),
            "execution_id": str(execution.id),
            "execution_status": execution.status,
            "verification_status": execution.verification_status or "",
        },
        trace_id=trace_id,
    )
    await enqueue(
        session,
        tenant_id=ctx.tenant_id,
        event_type="conversation_task.updated",
        aggregate_type="conversation_task",
        aggregate_id=str(task.id),
        payload={
            "task_id": str(task.id),
            "conversation_ref": str(task.conversation_ref_id),
            "command": "tool_execution_result",
            "version": task.version,
        },
        trace_id=trace_id,
    )
    if task.flow_key:
        get_metrics().workbench_standard_flow_actions_total.labels(
            flow_key=task.flow_key,
            action="gateway_execution",
            outcome=task.status,
        ).inc()


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
        execution_ids = [row.id for row in executions]
        reconciliations = (
            (
                await session.execute(
                    select(ToolExecutionReconciliation)
                    .where(
                        ToolExecutionReconciliation.tenant_id == ctx.tenant_id,
                        ToolExecutionReconciliation.execution_id.in_(execution_ids),
                    )
                    .order_by(
                        ToolExecutionReconciliation.created_at,
                        ToolExecutionReconciliation.id,
                    )
                )
            )
            .scalars()
            .all()
            if execution_ids
            else []
        )
        compensations = (
            (
                await session.execute(
                    select(ToolExecutionCompensation)
                    .where(
                        ToolExecutionCompensation.tenant_id == ctx.tenant_id,
                        ToolExecutionCompensation.execution_id.in_(execution_ids),
                    )
                    .order_by(
                        ToolExecutionCompensation.created_at,
                        ToolExecutionCompensation.id,
                    )
                )
            )
            .scalars()
            .all()
            if execution_ids
            else []
        )
        payload = {
            "proposal": _serialize_proposal(proposal, tool),
            "executions": [_serialize_execution(e) for e in executions],
            "reconciliations": [_serialize_reconciliation(row) for row in reconciliations],
            "compensations": [_serialize_compensation(row) for row in compensations],
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
