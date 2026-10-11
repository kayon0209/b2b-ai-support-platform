"""Tenant-scoped task dependency checks and durable child progression."""

from __future__ import annotations

import hashlib
import uuid
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from platform_core.agent_runtime.semantic.contracts import (
    SemanticCondition,
    SemanticInvalidOutput,
)
from platform_core.agent_runtime.tasks.conditions import (
    evaluate_condition,
    facts_from_verified_read,
)
from platform_core.agent_runtime.tasks.models import ConversationTask
from platform_core.agent_runtime.tasks.state_machine import TaskStatus
from platform_core.agent_runtime.tasks.store import TaskCommand, transition

TASK_WAITING_DEPENDENCY = "TASK_WAITING_DEPENDENCY"
TASK_DEPENDENCY_MISSING = "TASK_DEPENDENCY_MISSING"
TASK_DEPENDENCY_PENDING = "TASK_DEPENDENCY_PENDING"
TASK_DEPENDENCY_CONDITION_INVALID = "TASK_DEPENDENCY_CONDITION_INVALID"
TASK_DEPENDENCY_CONDITION_UNRESOLVED = "TASK_DEPENDENCY_CONDITION_UNRESOLVED"
TASK_DEPENDENCY_EXECUTION_RECHECK_REQUIRED = "TASK_DEPENDENCY_EXECUTION_RECHECK_REQUIRED"
TASK_DEPENDENCY_SATISFIED = "TASK_DEPENDENCY_SATISFIED"
TASK_DEPENDENCY_CONDITION_UNMET = "TASK_DEPENDENCY_CONDITION_UNMET"
TASK_CONDITION_UNMET = "TASK_CONDITION_UNMET"


async def dependency_block_reason(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    task: Any,
    parents: dict[str, Any] | None = None,
    allow_true_condition_for_proposal: bool = False,
) -> str | None:
    """Fail closed unless every scoped dependency is proven complete.

    Conditional tasks are based only on one verified `order.get_status`
    receipt that reads the customer-stated order and proves account ownership.
    A true historical result does not authorize execution; a caller may allow
    it only to prepare a proposal, while Tool Gateway must recheck at execute.
    """
    dependencies = list(task.depends_on or [])
    if not dependencies:
        return None

    if parents is None:
        dependency_rows = list(
            (
                await session.execute(
                    select(ConversationTask).where(
                        ConversationTask.tenant_id == tenant_id,
                        ConversationTask.conversation_ref_id == task.conversation_ref_id,
                        ConversationTask.source_turn_id == task.source_turn_id,
                        ConversationTask.task_local_key.in_(dependencies),
                    )
                )
            )
            .scalars()
            .all()
        )
        by_key = {row.task_local_key: row for row in dependency_rows}
    else:
        by_key = {key: parents[key] for key in dependencies if key in parents}
    if set(by_key) != set(dependencies):
        return TASK_DEPENDENCY_MISSING
    if any(row.status != TaskStatus.SUCCEEDED.value for row in by_key.values()):
        return TASK_DEPENDENCY_PENDING
    if task.condition is None:
        return None

    try:
        condition = SemanticCondition.model_validate(task.condition)
    except (ValidationError, TypeError, ValueError):
        return TASK_DEPENDENCY_CONDITION_INVALID

    from platform_core.cases.service import verified_account_for_conversation
    from platform_core.identity.profile import business_system_ref_for_account
    from platform_core.tool_gateway.gateway import load_verified_read_execution

    decisive: list[bool] = []
    for parent in by_key.values():
        execution_id = parent.execution_id
        if execution_id is None or parent.completion_evidence != f"tool_receipt:{execution_id}":
            continue
        read = await load_verified_read_execution(
            session,
            tenant_id=tenant_id,
            execution_id=execution_id,
        )
        if read is None or read.tool_name != "order.get_status":
            continue

        expected_order_id = next(
            (
                str(slot.get("value"))
                for slot in parent.slots or []
                if slot.get("name") in {"order_id", "order_ref", "order_number"}
                and slot.get("origin") == "customer_stated"
                and slot.get("confirmed") is True
                and slot.get("value") is not None
            ),
            None,
        )
        actual_order_id = read.sanitized_input.get("order_id")
        if not expected_order_id or str(actual_order_id or "") != expected_order_id:
            continue

        account_id = await verified_account_for_conversation(
            session,
            tenant_id=tenant_id,
            conversation_ref_id=task.conversation_ref_id,
        )
        if account_id is None:
            continue
        expected_account_ref = await business_system_ref_for_account(
            session,
            tenant_id=tenant_id,
            account_id=account_id,
            system_key="business_api",
        )
        record = read.sanitized_output.get("record")
        record = record if isinstance(record, dict) else read.sanitized_output
        actual_account_ref = record.get("account") or read.sanitized_output.get("account")
        if (
            not expected_account_ref
            or not isinstance(actual_account_ref, str)
            or actual_account_ref != expected_account_ref
        ):
            continue

        try:
            outcome = evaluate_condition(
                condition,
                facts_from_verified_read(read.tool_name, read.sanitized_output),
            )
        except SemanticInvalidOutput:
            return TASK_DEPENDENCY_CONDITION_INVALID
        if outcome.decidable:
            decisive.append(outcome.holds)

    # Zero results means proof/data are missing; multiple results mean the
    # source is ambiguous. Both remain blocked.
    if len(decisive) != 1:
        return TASK_DEPENDENCY_CONDITION_UNRESOLVED
    if not decisive[0]:
        return TASK_CONDITION_UNMET
    if allow_true_condition_for_proposal:
        return None
    return TASK_DEPENDENCY_EXECUTION_RECHECK_REQUIRED


async def check_live_dependent_write_precondition(
    session: AsyncSession,
    *,
    tenant_context: Any,
    write_proposal_id: uuid.UUID,
    write_tool_name: str,
    request_idempotency_key: str,
    trace_id: str,
) -> str | None:
    """Re-read an order condition after confirmation and before the write.

    The order read is itself a Tool Gateway execution with its own idempotency
    key and verified receipt. If the account, order identity, read tool,
    postcondition, or condition result cannot be proven, return a blocker and
    let the caller refuse the external write.
    """
    tenant_id = tenant_context.tenant_id
    task = (
        await session.execute(
            select(ConversationTask)
            .where(
                ConversationTask.tenant_id == tenant_id,
                ConversationTask.proposal_id == write_proposal_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if task is None or task.kind != "write" or task.condition is None:
        return None
    if not task.depends_on:
        return TASK_DEPENDENCY_MISSING

    selected_tool = next(
        (
            str(slot.get("value"))
            for slot in task.slots or []
            if slot.get("name") == "tool"
            and slot.get("origin") == "server_capability"
            and slot.get("selection_source") == "allowlisted_candidate_schema_match"
        ),
        None,
    )
    if task.flow_key in {"invoice_application", "repair_quality_intake", "technical_escalation"}:
        selected_tool = "case.create"
    if not selected_tool or selected_tool != write_tool_name:
        return TASK_DEPENDENCY_CONDITION_UNRESOLVED

    blocker = await dependency_block_reason(
        session,
        tenant_id=tenant_id,
        task=task,
        allow_true_condition_for_proposal=True,
    )
    if blocker is not None:
        return blocker

    try:
        condition = SemanticCondition.model_validate(task.condition)
    except (ValidationError, TypeError, ValueError):
        return TASK_DEPENDENCY_CONDITION_INVALID

    parents = list(
        (
            await session.execute(
                select(ConversationTask).where(
                    ConversationTask.tenant_id == tenant_id,
                    ConversationTask.conversation_ref_id == task.conversation_ref_id,
                    ConversationTask.source_turn_id == task.source_turn_id,
                    ConversationTask.task_local_key.in_(list(task.depends_on)),
                )
            )
        )
        .scalars()
        .all()
    )
    from platform_core.cases.service import verified_account_for_conversation
    from platform_core.identity.profile import business_system_ref_for_account
    from platform_core.tool_gateway.gateway import (
        ToolGateway,
        ToolGatewayError,
        load_verified_read_execution,
    )

    candidates: list[str] = []
    for parent in parents:
        if parent.execution_id is None:
            continue
        order_id = next(
            (
                str(slot.get("value"))
                for slot in parent.slots or []
                if slot.get("name") in {"order_id", "order_ref", "order_number"}
                and slot.get("origin") == "customer_stated"
                and slot.get("confirmed") is True
                and slot.get("value") is not None
            ),
            None,
        )
        if order_id is None:
            continue
        prior = await load_verified_read_execution(
            session,
            tenant_id=tenant_id,
            execution_id=parent.execution_id,
        )
        if (
            prior is not None
            and prior.tool_name == "order.get_status"
            and str(prior.sanitized_input.get("order_id") or "") == order_id
        ):
            candidates.append(order_id)
    if len(candidates) != 1:
        return TASK_DEPENDENCY_CONDITION_UNRESOLVED

    order_id = candidates[0]
    account_id = await verified_account_for_conversation(
        session,
        tenant_id=tenant_id,
        conversation_ref_id=task.conversation_ref_id,
    )
    if account_id is None:
        return TASK_DEPENDENCY_CONDITION_UNRESOLVED
    expected_account_ref = await business_system_ref_for_account(
        session,
        tenant_id=tenant_id,
        account_id=account_id,
        system_key="business_api",
    )
    if not expected_account_ref or tenant_context.actor_id is None:
        return TASK_DEPENDENCY_CONDITION_UNRESOLVED

    from platform_core.integrations.readiness import active_connector_capabilities
    from platform_core.tool_gateway.registry import resolve_executors
    from platform_policy import Action

    connector_capabilities = await active_connector_capabilities(session, tenant_id=tenant_id)
    if "orders_read" not in connector_capabilities:
        return TASK_DEPENDENCY_CONDITION_UNRESOLVED
    executors = await resolve_executors(
        session,
        tenant_id=tenant_id,
        tool_names=["order.get_status"],
        ctx=tenant_context,
        trace_id=trace_id,
    )
    if "order.get_status" not in executors:
        return TASK_DEPENDENCY_CONDITION_UNRESOLVED

    request_digest = hashlib.sha256(request_idempotency_key.encode()).hexdigest()[:32]
    read_key = f"task-precondition:{task.id}:{write_proposal_id}:{request_digest}"
    gateway = ToolGateway(session, executors)
    read_proposal_id = await gateway.proposal_id_for_idempotency_key(
        tenant_id=tenant_id,
        idempotency_key=read_key,
    )
    try:
        if read_proposal_id is None:
            read_proposal_id = await gateway.propose_id(
                tenant_id=tenant_id,
                actor_id=tenant_context.actor_id,
                tool_name="order.get_status",
                arguments={"order_id": order_id},
                role=tenant_context.role or "unknown",
                idempotency_key=read_key,
                permission_allowed=True,
                required_action=Action.TOOL_READ.value,
            )
        receipt = await gateway.execute_receipt(
            tenant_id=tenant_id,
            actor_id=tenant_context.actor_id,
            proposal_id=read_proposal_id,
        )
    except ToolGatewayError:
        return TASK_DEPENDENCY_CONDITION_UNRESOLVED

    read = await load_verified_read_execution(
        session,
        tenant_id=tenant_id,
        execution_id=receipt.id,
    )
    if (
        receipt.status != "executed"
        or receipt.verification_status != "verified"
        or read is None
        or read.tool_name != "order.get_status"
        or str(read.sanitized_input.get("order_id") or "") != order_id
    ):
        await _record_precondition_check(
            session,
            tenant_context=tenant_context,
            task=task,
            write_proposal_id=write_proposal_id,
            read_execution_id=receipt.id,
            result=TASK_DEPENDENCY_CONDITION_UNRESOLVED,
            trace_id=trace_id,
        )
        return TASK_DEPENDENCY_CONDITION_UNRESOLVED

    output = read.sanitized_output
    record = output.get("record")
    record = record if isinstance(record, dict) else output
    actual_account_ref = record.get("account") or output.get("account")
    if (
        output.get("order_id") != order_id
        or not isinstance(actual_account_ref, str)
        or actual_account_ref != expected_account_ref
    ):
        await _record_precondition_check(
            session,
            tenant_context=tenant_context,
            task=task,
            write_proposal_id=write_proposal_id,
            read_execution_id=receipt.id,
            result=TASK_DEPENDENCY_CONDITION_UNRESOLVED,
            trace_id=trace_id,
        )
        return TASK_DEPENDENCY_CONDITION_UNRESOLVED

    result: str | None = None
    try:
        outcome = evaluate_condition(condition, facts_from_verified_read(read.tool_name, output))
    except SemanticInvalidOutput:
        result = TASK_DEPENDENCY_CONDITION_INVALID
    else:
        if not outcome.decidable:
            result = TASK_DEPENDENCY_CONDITION_UNRESOLVED
        elif not outcome.holds:
            result = TASK_CONDITION_UNMET
    await _record_precondition_check(
        session,
        tenant_context=tenant_context,
        task=task,
        write_proposal_id=write_proposal_id,
        read_execution_id=receipt.id,
        result=result or "TASK_PRECONDITION_SATISFIED",
        trace_id=trace_id,
    )
    if result == TASK_CONDITION_UNMET:
        await _skip_false_condition_proposal(
            session,
            tenant_id=tenant_id,
            task=task,
            write_proposal_id=write_proposal_id,
            trace_id=trace_id,
        )
    return result


async def _skip_false_condition_proposal(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    task: ConversationTask,
    write_proposal_id: uuid.UUID,
    trace_id: str,
) -> None:
    """Withdraw the frozen action and persist a terminal skipped task."""
    from platform_core.tool_gateway.gateway import ToolGateway, ToolGatewayError

    if task.status != TaskStatus.AWAITING_CONFIRMATION.value:
        return
    try:
        await ToolGateway(session, {}).withdraw(
            tenant_id=tenant_id,
            proposal_id=write_proposal_id,
        )
    except ToolGatewayError:
        # The proposal may already have a terminal or ambiguous result. Do not
        # rewrite task state unless withdrawal proves the write cannot run.
        return
    await transition(
        session,
        tenant_id=tenant_id,
        task=task,
        command=TaskCommand(
            target=TaskStatus.CANCELLED,
            reason_code=TASK_DEPENDENCY_CONDITION_UNMET,
            actor_type="system",
            actor_ref="task_dependency_resolver",
            trace_id=trace_id,
            expected_version=task.version,
            blocked_reason=TASK_CONDITION_UNMET,
            proposal_withdrawn=True,
        ),
    )


async def _record_precondition_check(
    session: AsyncSession,
    *,
    tenant_context: Any,
    task: ConversationTask,
    write_proposal_id: uuid.UUID,
    read_execution_id: uuid.UUID,
    result: str,
    trace_id: str,
) -> None:
    from platform_core.audit import service as audit_service

    await audit_service.record(
        session,
        ctx=tenant_context,
        action="conversation.task_precondition_rechecked",
        resource_type="conversation_task",
        resource_id=task.id,
        decision="completed" if result == "TASK_PRECONDITION_SATISFIED" else "denied",
        reason_code=result,
        metadata={
            "write_proposal_id": str(write_proposal_id),
            "read_execution_id": str(read_execution_id),
            "read_tool": "order.get_status",
        },
        trace_id=trace_id,
    )


async def advance_dependents_after_parent(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    parent: ConversationTask,
    trace_id: str | None,
) -> list[ConversationTask]:
    """Durably unblock or skip ready children after a parent is verified.

    A condition that evaluates true remains ready but blocked by the live
    write-time recheck. A verified false condition cancels the child with an
    explicit event; it never reaches a proposal or executor.
    """
    if parent.status != TaskStatus.SUCCEEDED.value:
        return []
    rows = list(
        (
            await session.execute(
                select(ConversationTask)
                .where(
                    ConversationTask.tenant_id == tenant_id,
                    ConversationTask.conversation_ref_id == parent.conversation_ref_id,
                    ConversationTask.source_turn_id == parent.source_turn_id,
                    ConversationTask.id != parent.id,
                    ConversationTask.status == TaskStatus.READY.value,
                    ConversationTask.blocked_reason == TASK_WAITING_DEPENDENCY,
                )
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    advanced: list[ConversationTask] = []
    for child in rows:
        if parent.task_local_key not in (child.depends_on or []):
            continue
        blocker = await dependency_block_reason(
            session,
            tenant_id=tenant_id,
            task=child,
        )
        if blocker is None:
            advanced.append(
                await transition(
                    session,
                    tenant_id=tenant_id,
                    task=child,
                    command=TaskCommand(
                        target=TaskStatus.READY,
                        reason_code=TASK_DEPENDENCY_SATISFIED,
                        actor_type="system",
                        actor_ref="task_dependency_resolver",
                        trace_id=trace_id,
                        expected_version=child.version,
                        blocked_reason="",
                    ),
                )
            )
        elif blocker == TASK_CONDITION_UNMET:
            advanced.append(
                await transition(
                    session,
                    tenant_id=tenant_id,
                    task=child,
                    command=TaskCommand(
                        target=TaskStatus.CANCELLED,
                        reason_code=TASK_DEPENDENCY_CONDITION_UNMET,
                        actor_type="system",
                        actor_ref="task_dependency_resolver",
                        trace_id=trace_id,
                        expected_version=child.version,
                        blocked_reason=TASK_CONDITION_UNMET,
                    ),
                )
            )
    return advanced


__all__ = [
    "TASK_CONDITION_UNMET",
    "TASK_DEPENDENCY_CONDITION_INVALID",
    "TASK_DEPENDENCY_CONDITION_UNMET",
    "TASK_DEPENDENCY_CONDITION_UNRESOLVED",
    "TASK_DEPENDENCY_EXECUTION_RECHECK_REQUIRED",
    "TASK_DEPENDENCY_MISSING",
    "TASK_DEPENDENCY_PENDING",
    "TASK_DEPENDENCY_SATISFIED",
    "TASK_WAITING_DEPENDENCY",
    "advance_dependents_after_parent",
    "check_live_dependent_write_precondition",
    "dependency_block_reason",
]
