"""assessment -> task rows: the production wiring B1-02 found missing.

`plan_tasks` and `create_or_get` were pure functions with no caller. A customer
who asked "查一下 SO-240918，没发货就改成上海办公室，再补发票" produced no
tasks at all, so the workbench panel had nothing to show and TASK-01's unit
tests proved only that a pure function maps one list to another.

This module is the seam. It is deliberately small and deliberately gated:

- **Nothing runs unless a flag says so.** `agent.conversation_tasks` off means
  this returns before reading anything, and the customer's message is handled
  exactly as it was before R1. The gate is re-read here rather than trusted
  from the caller, because the caller's view may be minutes old.
- **A planning failure never fails the message.** The customer's answer is
  already sent by the time this runs; a planner error is recorded and the
  conversation continues.
- **The assessment is the input, not the raw text.** Tasks come from a
  validated `SemanticAssessment`, so a model that invented a tool or cited a
  turn it was never shown cannot produce a task.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from platform_core.agent_runtime.semantic.arbitration import (
    DECISION_ASSIST_CANDIDATES,
    DECISION_SEMANTIC_READ_ALLOWED,
    DECISION_SHADOW_RECORDED,
)
from platform_core.agent_runtime.semantic.contracts import (
    SemanticAssessment,
    SemanticMode,
    SemanticTaskKind,
)
from platform_core.agent_runtime.semantic.validator import CapabilityView
from platform_core.agent_runtime.tasks import store as task_store
from platform_core.agent_runtime.tasks.planner import plan_tasks

# The decisions that may create tasks.
#
# `shadow` is excluded on purpose: its whole contract is that it changes no
# business state, and writing task rows would break that in the one place a
# reader would check. `assist` and `semantic_read` are the modes where a
# suggestion is meant to become visible work.
TASK_CREATING_DECISIONS = frozenset({DECISION_ASSIST_CANDIDATES, DECISION_SEMANTIC_READ_ALLOWED})

REASON_TASKS_DISABLED = "SEMANTIC_TASKS_DISABLED"
REASON_SHADOW_NO_TASKS = "SEMANTIC_SHADOW_WRITES_NO_TASKS"
REASON_NO_INTENTS = "SEMANTIC_NO_INTENTS"
REASON_NO_MODEL_OUTPUT = "SEMANTIC_NO_MODEL_OUTPUT"

# Durable outbox request that a tenant-bound semantic worker analyzes after
# the customer's inbox run has completed.
TASK_PLANNING_EVENT_TYPE = "conversation.task_planning_requested"


@dataclass(frozen=True)
class PlanningOutcome:
    """What the seam did. Counts and codes, never content."""

    created: int = 0
    existing: int = 0
    reason: str = ""

    def as_log_fields(self) -> dict[str, Any]:
        return {
            "tasks_created": self.created,
            "tasks_existing": self.existing,
            "reason": self.reason,
        }


async def plan_and_persist(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    conversation_ref_id: uuid.UUID,
    assessment: SemanticAssessment,
    capabilities: dict[str, CapabilityView],
    accepted_tool_names: list[str],
    unsupported: dict[int, str],
    trace_id: str | None = None,
) -> PlanningOutcome:
    """Turn one validated assessment into task rows.

    Callers gate on the feature flag first; this function assumes it is on and
    refuses the decisions that must not create business state.
    """
    if assessment.mode is SemanticMode.OFF:
        return PlanningOutcome(reason=REASON_TASKS_DISABLED)
    if assessment.effective_decision not in TASK_CREATING_DECISIONS:
        # Shadow and the degraded paths land here. A shadow run that wrote
        # tasks would make SHD-01's "identical business state" claim false.
        return PlanningOutcome(
            reason=REASON_SHADOW_NO_TASKS
            if assessment.effective_decision == DECISION_SHADOW_RECORDED
            else assessment.effective_decision
        )
    output = assessment.model_output
    if output is None or not output.intents:
        return PlanningOutcome(
            reason=REASON_NO_MODEL_OUTPUT if output is None else REASON_NO_INTENTS
        )

    planned = plan_tasks(
        output.intents,
        capabilities=capabilities,
        accepted_tool_names=accepted_tool_names,
        unsupported=unsupported,
        needs_clarification=output.needs_clarification,
    )

    created = 0
    existing = 0
    for item in planned:
        _, was_created = await task_store.create_or_get(
            session,
            tenant_id=tenant_id,
            conversation_ref_id=conversation_ref_id,
            source_turn_id=item.source_turn_id,
            task_local_key=item.local_key,
            kind=item.kind,
            status=item.status,
            slots=item.slots,
            missing_slots=item.missing_slots,
            depends_on=item.depends_on,
            condition=item.condition,
            blocked_reason=item.blocked_reason,
            assessment_id=None,
            sequence=item.sequence,
            trace_id=trace_id,
        )
        if was_created:
            created += 1
        else:
            # A redelivery of a turn we already planned. Counting it separately
            # is what makes "the same message replayed produces the same tasks"
            # observable rather than merely true.
            existing += 1

    return PlanningOutcome(created=created, existing=existing, reason="PLANNED")


async def run_task_planning(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    conversation_ref_id: uuid.UUID,
    assessment: SemanticAssessment | None,
    capabilities: dict[str, CapabilityView],
    trace_id: str | None = None,
) -> PlanningOutcome:
    """The gated entry point the message path calls.

    Returns without touching the database when the flag is off, so the
    `off` path costs one flag read and nothing else.
    """
    from platform_core.agent_runtime.semantic.modes import (
        FLAG_ASSIST,
        FLAG_SHADOW,
        FLAG_TASKS,
        resolve_mode,
    )
    from platform_core.config import get_settings
    from platform_core.knowledge import flag_service

    try:
        decisions = await flag_service.evaluate_many(
            session,
            # The mode flags come first because `resolve_mode` derives the
            # effective mode from them: it only recognises shadow/assist/
            # semantic_read, so passing it `conversation_tasks` alone yields
            # OFF and a tenant that had opted in would be refused. The task
            # flag is a separate switch and is checked on its own below.
            flag_keys=[FLAG_SHADOW, FLAG_ASSIST, FLAG_TASKS],
            tenant_id=tenant_id,
            defaults={FLAG_SHADOW: False, FLAG_ASSIST: False, FLAG_TASKS: False},
        )
        resolution = resolve_mode(get_settings(), {k: d.enabled for k, d in decisions.items()})
        # The task flag is its own switch: it is not implied by shadow or
        # assist being on, because writing rows is a business effect and the
        # flags that turn on *suggestions* must not turn on *persistence*.
        #
        # Both checks, in this order: the flag is the switch, and a kill switch
        # on top of it stops the work even for a tenant that opted in.
        if not decisions.get(FLAG_TASKS) or not decisions[FLAG_TASKS].enabled:
            return PlanningOutcome(reason=REASON_TASKS_DISABLED)
        if resolution.mode is SemanticMode.OFF:
            # The tenant enabled task persistence but no suggestion mode, so
            # there is nothing to persist. Not an error - silence is right.
            return PlanningOutcome(reason=REASON_TASKS_DISABLED)
    except Exception:  # noqa: BLE001 - a flag read failure must not plan
        return PlanningOutcome(reason=REASON_TASKS_DISABLED)

    if assessment is None:
        return PlanningOutcome(reason=REASON_NO_MODEL_OUTPUT)

    unsupported = _unsupported_by_index(assessment, capabilities)
    accepted = [
        c.tool_name
        for c in (assessment.model_output.tool_candidates if assessment.model_output else [])
        if c.tool_name in capabilities
    ]
    return await plan_and_persist(
        session,
        tenant_id=tenant_id,
        conversation_ref_id=conversation_ref_id,
        assessment=assessment,
        capabilities=capabilities,
        accepted_tool_names=accepted,
        unsupported=unsupported,
        trace_id=trace_id,
    )


def _unsupported_by_index(
    assessment: SemanticAssessment, capabilities: dict[str, CapabilityView]
) -> dict[int, str]:
    """Recompute which intents cannot proceed, by position.

    The validator produced this map once, in-process, and it did not survive
    being written to a row - which is correct: a stored "unsupported" verdict
    would be a decision replayed without the capabilities that produced it. It
    is derived again here against the capabilities the server holds *now*.

    The direction is deliberately conservative. When no write capability is
    available for this tenant, a write intent is marked unsupported even if the
    model proposed one - which is the R1 behaviour the spec asks for, where an
    address change becomes a human task rather than a proposal.
    """
    output = assessment.model_output
    if output is None:
        return {}
    write_capable = [name for name, cap in capabilities.items() if _is_write(cap)]
    blocked: dict[int, str] = {}
    for index, intent in enumerate(output.intents):
        if intent.task_kind is SemanticTaskKind.WRITE and not write_capable:
            blocked[index] = "SEMANTIC_NO_WRITE_CAPABILITY"
    return blocked


def _is_write(cap: CapabilityView) -> bool:
    return cap.risk_class in ("low_write", "confirmed_write", "human_approval")


__all__ = [
    "REASON_NO_INTENTS",
    "REASON_NO_MODEL_OUTPUT",
    "REASON_SHADOW_NO_TASKS",
    "REASON_TASKS_DISABLED",
    "TASK_PLANNING_EVENT_TYPE",
    "TASK_CREATING_DECISIONS",
    "PlanningOutcome",
    "plan_and_persist",
    "run_task_planning",
]
