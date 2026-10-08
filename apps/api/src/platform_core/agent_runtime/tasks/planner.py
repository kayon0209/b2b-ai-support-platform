"""Plan a validated assessment into tasks the workbench can show.

This is where the spec's journey becomes rows. Given "查一下 SO-240918 到哪了，
如果没发货就改成上海办公室，另外补一下发票", the planner produces three
tasks with three different fates:

- the read becomes `ready` (or `awaiting_input` if the order number is missing);
- the address change becomes `needs_human` with
  `SEMANTIC_NO_WRITE_CAPABILITY`, because R1 has no write path for it;
- the invoice request joins the address change as needs-human, for the same
  reason and with its own missing fields listed.

The ordering rules that matter:

- **A task's status is decided by capability and missing fields, never by the
  model's confidence.** A high-confidence write with no capability is still
  needs-human.
- **A missing field moves a task to `awaiting_input`; an unavailable capability
  moves it to `needs_human`.** These are different asks with different owners -
  one waits for the customer, the other waits for a person - and collapsing
  them would send the platform asking a customer for something no answer to
  them could provide.
- **Nothing here executes anything.** The planner writes rows; execution is the
  gateway's, and only for reads the capability filter allowed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from platform_core.agent_runtime.semantic.contracts import (
    SemanticIntent,
    SemanticTaskKind,
    SlotOrigin,
)
from platform_core.agent_runtime.semantic.validator import CapabilityView
from platform_core.agent_runtime.tasks.state_machine import TaskKind, TaskStatus
from platform_core.evaluation.pii import redact_text, should_withhold_value

# Why a task is in the state it is in. These become `blocked_reason` and are
# shown to the operator verbatim: "unsupported" without a reason is not
# something anyone can act on.
REASON_MISSING_FIELDS = "TASK_MISSING_FIELDS"
REASON_NO_WRITE_CAPABILITY = "SEMANTIC_NO_WRITE_CAPABILITY"
REASON_NO_READ_CAPABILITY = "SEMANTIC_NO_READ_CAPABILITY"
REASON_NEEDS_CLARIFICATION = "SEMANTIC_NEEDS_CLARIFICATION"
REASON_WAITING_DEPENDENCY = "TASK_WAITING_DEPENDENCY"
REASON_CONDITION_UNMET = "TASK_CONDITION_UNMET"
REASON_TOOL_SELECTION_AMBIGUOUS = "TASK_TOOL_SELECTION_AMBIGUOUS"
REASON_TOOL_SELECTION_UNRESOLVED = "TASK_TOOL_SELECTION_UNRESOLVED"

# `tool` is a reserved task-slot name. The model may propose a tool globally,
# but only this planner writes the task-local binding after matching the
# allowlisted candidate against the server's input schema.
TOOL_SLOT_NAME = "tool"
TOOL_SLOT_ORIGIN = "server_capability"

# Slots whose value is a real address, tax id or other identifier. Their
# *names* may be stored; the values are not, and a task row is not the place
# for them.
SENSITIVE_SLOT_NAMES = frozenset(
    {
        "address",
        "street",
        "postal_code",
        "tax_id",
        "phone",
        "email",
        "bank_account",
        "id_card",
    }
)


@dataclass(frozen=True)
class PlannedTask:
    """One task, ready to be written by `store.create_or_get`."""

    local_key: str
    kind: TaskKind
    status: TaskStatus
    sequence: int
    source_turn_id: str
    slots: list[dict[str, Any]]
    missing_slots: list[str]
    depends_on: list[str]
    condition: dict[str, Any] | None
    blocked_reason: str | None


def plan_tasks(
    intents: list[SemanticIntent],
    *,
    capabilities: dict[str, CapabilityView],
    accepted_tool_names: list[str],
    unsupported: dict[int, str],
    needs_clarification: bool = False,
) -> list[PlannedTask]:
    """Turn validated intents into tasks.

    `unsupported` maps an intent's index to the reason it cannot proceed, as
    decided by `validator._unsupported_intents`. Passing it in rather than
    recomputing keeps one definition of "unsupported" between the validator,
    the planner and the UI.
    """
    planned: list[PlannedTask] = []
    keys_by_index: dict[int, str] = {}
    write_capable = [
        name
        for name, cap in capabilities.items()
        if cap.risk_class in ("low_write", "confirmed_write", "human_approval")
    ]

    # First pass assigns keys, so a dependency can name a sibling that appears
    # later in the list.
    for index, intent in enumerate(intents):
        keys_by_index[index] = f"{intent.task_kind.value}-{index}"

    for index, intent in enumerate(intents):
        key = keys_by_index[index]
        # A model-supplied ordinary slot must never become the authoritative
        # tool choice. The reserved row below is server generated.
        slots = [_slot_row(s) for s in intent.slots if s.name != TOOL_SLOT_NAME]
        missing = [name for name in intent.missing_slots if name != TOOL_SLOT_NAME]
        selected_tool, selection_reason = _select_tool_for_intent(
            intent,
            capabilities=capabilities,
            accepted_tool_names=accepted_tool_names,
        )
        if selected_tool is None and any(slot.name == TOOL_SLOT_NAME for slot in intent.slots):
            # A model-written reserved slot is evidence of a selection attempt,
            # not an authority to choose a business tool.
            selection_reason = REASON_TOOL_SELECTION_UNRESOLVED
        if selected_tool is not None:
            provided_names = {slot.name for slot in intent.slots if slot.name != TOOL_SLOT_NAME}
            for required_name in capabilities[selected_tool].required_parameters:
                if required_name not in provided_names and required_name not in missing:
                    missing.append(required_name)
            slots.append(
                {
                    "name": TOOL_SLOT_NAME,
                    "value": selected_tool,
                    "origin": TOOL_SLOT_ORIGIN,
                    "selection_source": "allowlisted_candidate_schema_match",
                    "confirmed": False,
                }
            )
        condition = (
            intent.condition.model_dump(mode="json") if intent.condition is not None else None
        )
        # `depends_on` names a sibling by its zero-based position in the
        # model's own `intents` array - the one identifier that is stable
        # within a single plan, since the spec journey puts all three needs in
        # one turn and `source_turn_id` is therefore the same string for all
        # of them.
        #
        # The stored form is the server-issued local key, so a task never
        # depends on a model-chosen string. A reference the validator already
        # refused (unknown, out of range) is dropped here rather than
        # resolved: the intent keeps its own status, it just does not wait.
        depends = [
            keys_by_index[ordinal]
            for ordinal in _as_ordinals(intent.depends_on)
            if ordinal in keys_by_index
        ]

        blocked = unsupported.get(index)
        if selection_reason is not None and blocked in (None, REASON_MISSING_FIELDS):
            blocked = selection_reason
        status, reason = _decide(
            intent=intent,
            blocked=blocked,
            missing=missing,
            write_capable=write_capable,
            accepted_tool_names=accepted_tool_names,
            needs_clarification=needs_clarification,
        )

        planned.append(
            PlannedTask(
                local_key=key,
                kind=TaskKind(intent.task_kind.value),
                status=status,
                sequence=index,
                source_turn_id=intent.source_turn_id,
                slots=slots,
                missing_slots=missing,
                depends_on=depends,
                condition=condition,
                blocked_reason=reason,
            )
        )

    return planned


def _select_tool_for_intent(
    intent: SemanticIntent,
    *,
    capabilities: dict[str, CapabilityView],
    accepted_tool_names: list[str],
) -> tuple[str | None, str | None]:
    """Bind an allowlisted model candidate to one task only when schema says so.

    `tool_candidates` is global to the model response, while tasks are per
    intent. Matching by list order or picking the first candidate would attach
    the wrong business action to a subtask. Parameter names are the only
    server-owned bridge currently available. If more than one candidate fits,
    the caller parks that task for a human instead of guessing.
    """
    if intent.task_kind is SemanticTaskKind.CLARIFY:
        return None, None

    task_kind = intent.task_kind.value
    slot_names = {slot.name for slot in intent.slots if slot.name != TOOL_SLOT_NAME}
    supplied_or_missing = slot_names | {
        name for name in intent.missing_slots if name != TOOL_SLOT_NAME
    }
    candidates: list[CapabilityView] = []
    seen: set[str] = set()
    for name in accepted_tool_names:
        if name in seen:
            continue
        seen.add(name)
        capability = capabilities.get(name)
        if capability is None or task_kind not in capability.allowed_task_kinds:
            continue
        candidates.append(capability)

    if not candidates:
        return None, REASON_TOOL_SELECTION_UNRESOLVED if accepted_tool_names else None

    schema_matches: list[CapabilityView] = []
    complete_matches: list[CapabilityView] = []
    for capability in candidates:
        parameter_names = set(capability.parameter_names)
        required_parameters = set(capability.required_parameters)
        # An empty schema is only informative for a no-argument task. When
        # schema metadata is absent for a task with slots, do not infer a fit.
        if not parameter_names and (slot_names or required_parameters):
            continue
        if not slot_names.issubset(parameter_names):
            continue
        schema_matches.append(capability)
        if required_parameters.issubset(supplied_or_missing):
            complete_matches.append(capability)

    if len(complete_matches) == 1:
        return complete_matches[0].tool_name, None
    if len(complete_matches) > 1 or len(schema_matches) > 1:
        return None, REASON_TOOL_SELECTION_AMBIGUOUS
    if len(schema_matches) == 1:
        # The capability is unique even though some required fields were not
        # named by the model. Its schema supplies those names as missing input.
        return schema_matches[0].tool_name, None
    return None, REASON_TOOL_SELECTION_UNRESOLVED


def _decide(
    *,
    intent: SemanticIntent,
    blocked: str | None,
    missing: list[str],
    write_capable: list[str],
    accepted_tool_names: list[str],
    needs_clarification: bool,
) -> tuple[TaskStatus, str | None]:
    """The status and the reason, in one place.

    Order is deliberate: an unavailable capability outranks a missing field,
    because asking the customer for a street address cannot make a write
    capability appear, and the ask would be a question with no possible answer.
    """
    if intent.task_kind is SemanticTaskKind.CLARIFY:
        return TaskStatus.AWAITING_INPUT, REASON_NEEDS_CLARIFICATION

    if blocked == REASON_NO_WRITE_CAPABILITY or (
        intent.task_kind is SemanticTaskKind.WRITE and not write_capable
    ):
        # The spec journey's third step: an address change R1 cannot perform
        # is a human task, with the missing fields still listed so the agent
        # can collect them in one message.
        return TaskStatus.NEEDS_HUMAN, REASON_NO_WRITE_CAPABILITY

    if blocked == REASON_NO_READ_CAPABILITY or (
        intent.task_kind is SemanticTaskKind.READ and not accepted_tool_names
    ):
        return TaskStatus.NEEDS_HUMAN, REASON_NO_READ_CAPABILITY

    if blocked in (REASON_TOOL_SELECTION_AMBIGUOUS, REASON_TOOL_SELECTION_UNRESOLVED):
        return TaskStatus.NEEDS_HUMAN, blocked

    if blocked == REASON_MISSING_FIELDS or missing:
        return TaskStatus.AWAITING_INPUT, REASON_MISSING_FIELDS

    if intent.depends_on:
        # Ready, but not schedulable until the dependency settles. The
        # dependency evaluation is the scheduler's, and an unmet condition
        # moves the task then - not here, where the facts are not yet read.
        return TaskStatus.READY, REASON_WAITING_DEPENDENCY

    if needs_clarification:
        return TaskStatus.AWAITING_INPUT, REASON_NEEDS_CLARIFICATION

    return TaskStatus.READY, None


def _as_ordinals(deps: list[str]) -> list[int]:
    """Parse `depends_on` entries as positions in the intent array.

    A non-numeric or negative entry yields nothing, which drops that edge
    rather than guessing. Guessing which sibling a model meant would attach a
    write to the wrong read, and the wrong precondition is worse than a
    missing one.
    """
    out: list[int] = []
    for dep in deps:
        try:
            ordinal = int(str(dep).strip())
        except (TypeError, ValueError):
            continue
        if ordinal >= 0:
            out.append(ordinal)
    return out


def _slot_row(slot: Any) -> dict[str, Any]:
    """Project a validated slot for storage.

    Name, origin, confirmation - and the value only when it is neither
    sensitive nor merely inferred. A `customer_stated` order number is worth
    keeping; an `inferred` address and any address at all are not, because this
    row is read by the audit path and by a workbench list that does not need
    the value to tell an agent what is missing.
    """
    row: dict[str, Any] = {
        "name": slot.name,
        "origin": slot.origin.value,
        "confirmed": slot.confirmed,
    }
    sensitive = slot.name.lower() in SENSITIVE_SLOT_NAMES or should_withhold_value(slot.name)
    if sensitive:
        row["value_withheld"] = True
    elif slot.origin is SlotOrigin.INFERRED:
        # Guessing is allowed in the output and forbidden in storage: an
        # inferred value with no source is exactly what EVAL-02 counts as a
        # failure, so it must not be readable back as if it were data.
        row["value_withheld"] = True
        row["inferred"] = True
    else:
        if isinstance(slot.value, str):
            row["value"], _redaction_count = redact_text(slot.value)
        else:
            row["value"] = slot.value
    return row


__all__ = [
    "REASON_CONDITION_UNMET",
    "REASON_MISSING_FIELDS",
    "REASON_NEEDS_CLARIFICATION",
    "REASON_NO_READ_CAPABILITY",
    "REASON_NO_WRITE_CAPABILITY",
    "REASON_WAITING_DEPENDENCY",
    "REASON_TOOL_SELECTION_AMBIGUOUS",
    "REASON_TOOL_SELECTION_UNRESOLVED",
    "TOOL_SLOT_NAME",
    "TOOL_SLOT_ORIGIN",
    "SENSITIVE_SLOT_NAMES",
    "PlannedTask",
    "plan_tasks",
]
