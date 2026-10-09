"""Apply verified Tool Gateway outcomes to their linked conversation task.

This is the application boundary called by Tool Gateway after it has persisted
an execution result. The task becomes succeeded only for a verified receipt;
ambiguous and failed executions remain explicit states.
"""

from __future__ import annotations

import time
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from platform_core.agent_runtime.tasks.dependencies import advance_dependents_after_parent
from platform_core.agent_runtime.tasks.models import ConversationTask
from platform_core.agent_runtime.tasks.state_machine import TaskStatus
from platform_core.agent_runtime.tasks.store import TaskCommand, transition


async def lock_task_proposal_lease(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    proposal_id: uuid.UUID,
    now: int | None = None,
) -> str | None:
    """Lock and re-check the lease captured by a linked task proposal.

    Lock order is lease -> task -> ToolProposal. Task commands use the same
    order when withdrawing proposals, so confirmation and execution cannot
    race reassignment into a stale external action. Standalone proposals have
    no task and keep their existing policy-only flow.
    """
    linked = (
        await session.execute(
            select(ConversationTask.id, ConversationTask.conversation_ref_id).where(
                ConversationTask.tenant_id == tenant_id,
                ConversationTask.proposal_id == proposal_id,
            )
        )
    ).one_or_none()
    if linked is None:
        return None

    from platform_core.identity import lease_service

    lease = await lease_service.lease_snapshot(
        session,
        tenant_id=tenant_id,
        conversation_ref_id=linked.conversation_ref_id,
        for_update=True,
    )
    if lease is None:
        return "TASK_LEASE_MISSING"

    task = (
        await session.execute(
            select(ConversationTask)
            .where(
                ConversationTask.tenant_id == tenant_id,
                ConversationTask.id == linked.id,
                ConversationTask.proposal_id == proposal_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if task is None or task.status != TaskStatus.AWAITING_CONFIRMATION.value:
        return "TASK_PROPOSAL_NOT_PENDING"
    if task.proposal_lease_version is None:
        return "TASK_LEASE_VERSION_MISSING"
    current_time = int(time.time()) if now is None else now
    if lease.expires_at is not None and lease.expires_at <= current_time:
        return "TASK_LEASE_EXPIRED"
    if lease.owner_type != "human":
        return "TASK_HUMAN_OWNERSHIP_REQUIRED"
    if lease.lease_version != task.proposal_lease_version:
        return "TASK_LEASE_VERSION_STALE"
    return None


async def lock_task_proposal_reconciliation_owner(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    proposal_id: uuid.UUID,
    actor_id: uuid.UUID,
    now: int | None = None,
) -> str | None:
    """Require the current human owner to resolve a linked external outcome.

    Reconciliation records what a human found in the provider. It does not
    authorize another write, so a lease version captured by the old proposal
    must not prevent the current owner from closing an ambiguous result.
    """
    linked = (
        await session.execute(
            select(ConversationTask.id, ConversationTask.conversation_ref_id).where(
                ConversationTask.tenant_id == tenant_id,
                ConversationTask.proposal_id == proposal_id,
            )
        )
    ).one_or_none()
    if linked is None:
        return None

    from platform_core.identity import lease_service

    lease = await lease_service.lease_snapshot(
        session,
        tenant_id=tenant_id,
        conversation_ref_id=linked.conversation_ref_id,
        for_update=True,
    )
    if lease is None:
        return "TASK_LEASE_MISSING"
    current_time = int(time.time()) if now is None else now
    if lease.expires_at is not None and lease.expires_at <= current_time:
        return "TASK_LEASE_EXPIRED"
    if lease.owner_type != "human":
        return "TASK_HUMAN_OWNERSHIP_REQUIRED"
    if lease.owner_ref != str(actor_id):
        return "TASK_LEASE_NOT_OWNED"
    task_id = (
        await session.execute(
            select(ConversationTask.id)
            .where(
                ConversationTask.tenant_id == tenant_id,
                ConversationTask.id == linked.id,
                ConversationTask.proposal_id == proposal_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    return None if task_id is not None else "TASK_PROPOSAL_NOT_PENDING"


async def record_execution_result(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    proposal_id: uuid.UUID,
    execution_id: uuid.UUID,
    execution_status: str,
    verification_status: str | None,
    trace_id: str,
    result_slots: list[dict[str, Any]] | None = None,
) -> ConversationTask | None:
    """Synchronize a terminal Gateway result to the one linked task, if any."""
    task = (
        await session.execute(
            select(ConversationTask)
            .where(
                ConversationTask.tenant_id == tenant_id,
                ConversationTask.proposal_id == proposal_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if task is None:
        return None

    if execution_status == "executing":
        final_status = TaskStatus.EXECUTING
    elif execution_status == "executed" and verification_status == "verified":
        final_status = TaskStatus.SUCCEEDED
    elif execution_status == "failed" or verification_status == "failed":
        final_status = TaskStatus.FAILED
    elif execution_status == "unknown" or verification_status == "unknown":
        final_status = TaskStatus.UNKNOWN
    else:
        # An unrecognized or incomplete receipt is never success evidence.
        final_status = TaskStatus.UNKNOWN

    current_status = TaskStatus(task.status)
    if current_status not in (
        TaskStatus.AWAITING_CONFIRMATION,
        TaskStatus.EXECUTING,
        TaskStatus.UNKNOWN,
    ):
        return None

    if current_status is TaskStatus.AWAITING_CONFIRMATION:
        task = await transition(
            session,
            tenant_id=tenant_id,
            task=task,
            command=TaskCommand(
                target=TaskStatus.EXECUTING,
                reason_code="TASK_TOOL_EXECUTION_STARTED",
                actor_type="system",
                actor_ref="tool_gateway",
                trace_id=trace_id,
                expected_version=task.version,
                execution_id=execution_id,
            ),
        )
    elif current_status is TaskStatus.UNKNOWN and final_status is TaskStatus.UNKNOWN:
        # Repeated unresolved lookups add their own audit evidence at the
        # reconciliation boundary; they do not bump the task version again.
        return None

    if final_status is TaskStatus.EXECUTING:
        return task

    evidence = f"tool_receipt:{execution_id}" if final_status is TaskStatus.SUCCEEDED else None
    reason_code = {
        TaskStatus.SUCCEEDED: "TASK_TOOL_EXECUTION_VERIFIED",
        TaskStatus.FAILED: "TASK_TOOL_EXECUTION_FAILED",
        TaskStatus.UNKNOWN: "TASK_TOOL_EXECUTION_UNKNOWN",
    }[final_status]
    blocked_reason = (
        None
        if final_status is TaskStatus.SUCCEEDED
        else (
            "TOOL_EXECUTION_FAILED"
            if final_status is TaskStatus.FAILED
            else "TOOL_EXECUTION_UNKNOWN"
        )
    )
    updated = await transition(
        session,
        tenant_id=tenant_id,
        task=task,
        command=TaskCommand(
            target=final_status,
            reason_code=reason_code,
            actor_type="system",
            actor_ref="tool_gateway",
            trace_id=trace_id,
            expected_version=task.version,
            completion_evidence=evidence,
            execution_id=execution_id,
            blocked_reason=blocked_reason,
            slots=result_slots if final_status is TaskStatus.SUCCEEDED else None,
        ),
    )
    if final_status is TaskStatus.SUCCEEDED:
        await advance_dependents_after_parent(
            session,
            tenant_id=tenant_id,
            parent=updated,
            trace_id=trace_id,
        )
    return updated


__all__ = [
    "lock_task_proposal_lease",
    "lock_task_proposal_reconciliation_owner",
    "record_execution_result",
]
