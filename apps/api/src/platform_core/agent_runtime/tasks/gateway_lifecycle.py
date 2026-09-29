"""Apply verified Tool Gateway outcomes to their linked conversation task.

This is the application boundary called by Tool Gateway after it has persisted
an execution result. The task becomes succeeded only for a verified receipt;
ambiguous and failed executions remain explicit states.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from platform_core.agent_runtime.tasks.models import ConversationTask
from platform_core.agent_runtime.tasks.state_machine import TaskStatus
from platform_core.agent_runtime.tasks.store import TaskCommand, transition


async def record_execution_result(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    proposal_id: uuid.UUID,
    execution_id: uuid.UUID,
    execution_status: str,
    verification_status: str | None,
    trace_id: str,
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
    if current_status not in (TaskStatus.AWAITING_CONFIRMATION, TaskStatus.EXECUTING):
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
    return await transition(
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
        ),
    )


__all__ = ["record_execution_result"]
