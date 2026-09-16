"""Tool Gateway service (tickets 29-30).

The gateway is the ONLY path through which the AI (or anyone) executes a
business write. Deterministic code owns every gate; the LLM can propose
but never bypass:

1. propose: validate args against the tool's JSON Schema (deny-by-default
   registry), sanitize inputs, compute the action hash, evaluate the
   permission decision via the policy engine.
2. confirm: high-risk tools require an ActionConfirmation bound to the
   same actor, tool version and action hash, with expiry.
3. execute: idempotent by (tenant_id, idempotency_key); the DB unique
   constraint plus the executor's short-circuit make duplicate commands
   single-execution.
4. verify: postcondition check through a read or provider receipt. An
   execution is successful only after verification; transport success is
   insufficient. Ambiguous outcomes stay UNKNOWN.
"""

import hashlib
import json
import time
import uuid
from typing import Any, Protocol

from jsonschema import ValidationError
from jsonschema import validate as jsonschema_validate
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from platform_core.tool_gateway.models import (
    ActionConfirmation,
    ProposalStatus,
    ToolDefinition,
    ToolExecution,
    ToolProposal,
    ToolRisk,
)


class ToolGatewayError(Exception):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code


class ToolDenied(ToolGatewayError):
    pass


class ToolExecutor(Protocol):
    """Adapter-backed executor registered per tool name."""

    async def execute(
        self, tool_name: str, parameters: dict[str, Any], idempotency_key: str
    ) -> dict[str, Any] | None: ...

    async def verify_postcondition(
        self, tool_name: str, parameters: dict[str, Any], output: dict[str, Any] | None
    ) -> bool | None:
        """True/False verified; None = cannot determine -> UNKNOWN."""
        ...


SENSITIVE_FIELD_NAMES = {
    "password",
    "secret",
    "token",
    "api_key",
    "apikey",
    "authorization",
    "credit_card",
    "ssn",
    "email",
    "phone",
}


def sanitize_arguments(args: dict[str, Any]) -> dict[str, Any]:
    """Strip credential-shaped keys and redact PII-shaped values."""
    cleaned: dict[str, Any] = {}
    for key, value in args.items():
        if key.lower() in SENSITIVE_FIELD_NAMES:
            cleaned[key] = "***"
        elif isinstance(value, dict):
            cleaned[key] = sanitize_arguments(value)
        else:
            cleaned[key] = value
    return cleaned


def compute_action_hash(tool_name: str, tool_version: int, sanitized: dict[str, Any]) -> str:
    canonical = json.dumps(
        {"tool": tool_name, "version": tool_version, "args": sanitized},
        sort_keys=True,
        ensure_ascii=False,
        default=str,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def validate_against_schema(args: dict[str, Any], schema: dict[str, Any]) -> None:
    try:
        jsonschema_validate(instance=args, schema=schema)
    except ValidationError as exc:
        raise ToolGatewayError("TOOL_ARGS_INVALID", exc.message) from exc


class ToolGateway:
    def __init__(self, session: AsyncSession, executors: dict[str, ToolExecutor]) -> None:
        self._session = session
        self._executors = executors

    async def propose(
        self,
        *,
        tenant_id: uuid.UUID,
        actor_id: uuid.UUID,
        tool_name: str,
        arguments: dict[str, Any],
        role: str,
        idempotency_key: str,
        permission_allowed: bool,
        permission_reason: str = "OK",
    ) -> ToolProposal:
        """Create a proposal. The caller supplies the policy decision for
        the required permission (checked against the policy engine);
        the gateway still enforces registry/schema/confirmation rules."""
        stmt = (
            select(ToolDefinition)
            .where(
                # SQL IN does not match NULLs: platform-catalog tools
                # (tenant_id IS NULL) need an explicit OR.
                (ToolDefinition.tenant_id == tenant_id) | ToolDefinition.tenant_id.is_(None),
                ToolDefinition.name == tool_name,
            )
            .order_by(ToolDefinition.version.desc())
            .limit(1)
        )
        tool = (await self._session.execute(stmt)).scalar_one_or_none()
        if tool is None:
            raise ToolDenied("TOOL_NOT_REGISTERED", tool_name)
        if tool.risk == ToolRisk.PROHIBITED.value:
            raise ToolDenied("TOOL_PROHIBITED", tool_name)

        validate_against_schema(arguments, tool.input_schema)
        sanitized = sanitize_arguments(arguments)
        action_hash = compute_action_hash(tool.name, tool.version, sanitized)
        requires_confirmation = (
            tool.risk in (ToolRisk.CONFIRMED_WRITE.value, ToolRisk.HUMAN_APPROVAL.value)
            or tool.requires_confirmation
        )

        proposal = ToolProposal(
            tenant_id=tenant_id,
            tool_definition_id=tool.id,
            action_hash=action_hash,
            actor_id=actor_id,
            sanitized_input=sanitized,
            status=ProposalStatus.AUTHORIZED.value
            if permission_allowed
            else ProposalStatus.REJECTED.value,
            permission_decision="allowed" if permission_allowed else "denied",
            permission_reason=permission_reason[:63],
            required_confirmation=requires_confirmation,
            expires_at=int(time.time()) + 900,  # 15 minutes to confirm/execute
            idempotency_key=idempotency_key,
        )
        self._session.add(proposal)
        await self._session.flush()
        if not permission_allowed:
            raise ToolDenied(permission_reason, tool_name)
        return proposal

    async def confirm(
        self,
        *,
        tenant_id: uuid.UUID,
        proposal_id: uuid.UUID,
        actor_id: uuid.UUID,
    ) -> ActionConfirmation:
        proposal = await self._get_proposal(tenant_id, proposal_id)
        if proposal is None:
            raise ToolGatewayError("PROPOSAL_NOT_FOUND")
        if proposal.status not in (ProposalStatus.AUTHORIZED.value,):
            raise ToolGatewayError("PROPOSAL_NOT_CONFIRMABLE", proposal.status)
        if proposal.expires_at < int(time.time()):
            await self._mark(proposal, ProposalStatus.EXPIRED.value)
            raise ToolGatewayError("PROPOSAL_EXPIRED")
        if not proposal.required_confirmation:
            raise ToolGatewayError("CONFIRMATION_NOT_REQUIRED")

        confirmation = ActionConfirmation(
            tenant_id=tenant_id,
            proposal_id=proposal.id,
            actor_id=actor_id,
            action_hash=proposal.action_hash,  # binds to exact arguments
            expires_at=int(time.time()) + 600,
            confirmed_at=int(time.time()),
        )
        self._session.add(confirmation)
        proposal.status = ProposalStatus.CONFIRMED.value
        await self._session.flush()
        return confirmation

    async def execute(
        self,
        *,
        tenant_id: uuid.UUID,
        actor_id: uuid.UUID,
        proposal_id: uuid.UUID,
        confirmed_by: uuid.UUID | None = None,
    ) -> ToolExecution:
        proposal = await self._get_proposal(tenant_id, proposal_id)
        if proposal is None:
            raise ToolGatewayError("PROPOSAL_NOT_FOUND")
        if proposal.status in (ProposalStatus.REJECTED.value, ProposalStatus.EXPIRED.value):
            raise ToolGatewayError("PROPOSAL_NOT_EXECUTABLE", proposal.status)
        if proposal.expires_at < int(time.time()):
            await self._mark(proposal, ProposalStatus.EXPIRED.value)
            raise ToolGatewayError("PROPOSAL_EXPIRED")

        tool = (
            await self._session.execute(
                select(ToolDefinition).where(ToolDefinition.id == proposal.tool_definition_id)
            )
        ).scalar_one()
        if tool is None:
            raise ToolGatewayError("TOOL_NOT_REGISTERED")

        # Confirmation gate: high-risk tools need a matching confirmation
        # bound to the same action hash before any execution.
        if proposal.required_confirmation:
            confirmation = (
                await self._session.execute(
                    select(ActionConfirmation).where(
                        ActionConfirmation.proposal_id == proposal.id,
                        ActionConfirmation.action_hash == proposal.action_hash,
                    )
                )
            ).scalar_one_or_none()
            if confirmation is None:
                raise ToolDenied("CONFIRMATION_REQUIRED")
            if confirmation.expires_at < int(time.time()):
                raise ToolGatewayError("CONFIRMATION_EXPIRED")

        # Idempotency: existing execution short-circuits (no second call).
        existing = (
            await self._session.execute(
                select(ToolExecution).where(
                    ToolExecution.tenant_id == tenant_id,
                    ToolExecution.idempotency_key == proposal.idempotency_key,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            return existing

        executor = self._executors.get(tool.name)
        if executor is None:
            raise ToolGatewayError("TOOL_EXECUTOR_MISSING", tool.name)

        execution = ToolExecution(
            tenant_id=tenant_id,
            proposal_id=proposal.id,
            actor_id=actor_id,
            tool_definition_id=tool.id,
            idempotency_key=proposal.idempotency_key,
            status=ProposalStatus.EXECUTING.value,
            sanitized_input=proposal.sanitized_input,
            started_at=int(time.time()),
        )
        self._session.add(execution)
        await self._session.flush()

        try:
            output = await executor.execute(
                tool.name, proposal.sanitized_input, proposal.idempotency_key
            )
        except Exception as exc:  # noqa: BLE001 - classified below
            execution.status = ProposalStatus.FAILED.value
            execution.error_code = "TOOL_EXECUTION_ERROR"
            execution.completed_at = int(time.time())
            await self._mark(proposal, ProposalStatus.FAILED.value)
            raise ToolGatewayError("TOOL_EXECUTION_ERROR", str(exc)[:200]) from exc

        execution.sanitized_output = sanitize_arguments(output or {})
        execution.completed_at = int(time.time())

        # Postcondition verification decides the final status.
        verified = await executor.verify_postcondition(
            tool.name, proposal.sanitized_input, execution.sanitized_output
        )
        if verified is True:
            execution.status = ProposalStatus.EXECUTED.value
            execution.verification_status = "verified"
            proposal.status = ProposalStatus.VERIFIED.value
        elif verified is False:
            execution.status = ProposalStatus.FAILED.value
            execution.verification_status = "failed"
            proposal.status = ProposalStatus.FAILED.value
        else:
            # Ambiguous: never report success.
            execution.status = ProposalStatus.UNKNOWN.value
            execution.verification_status = "unknown"
            proposal.status = ProposalStatus.UNKNOWN.value
        await self._session.flush()
        return execution

    async def _get_proposal(
        self, tenant_id: uuid.UUID, proposal_id: uuid.UUID
    ) -> ToolProposal | None:
        stmt = select(ToolProposal).where(
            ToolProposal.tenant_id == tenant_id, ToolProposal.id == proposal_id
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    @staticmethod
    async def _mark(proposal: ToolProposal, status: str) -> None:
        proposal.status = status
