"""Conversation tasks: a bounded, recoverable record of what a customer asked for.

R1 scope. A run has one status; a customer turn can carry three needs with
different states, different missing fields and different blockers. These tables
are where that difference survives, so the workbench can show "checked, blocked,
unsupported" instead of collapsing it into one answer.

The rules that matter, and where each is enforced:

- transitions: `state_machine.check_transition` - terminal states do not move,
  and a write cannot reach `executing` without passing confirmation;
- conditions: `conditions.evaluate_condition` - registered fields only, and an
  undecidable condition holds the task rather than resolving it wrongly;
- idempotency and optimistic concurrency: `store.create_or_get` /
  `store.transition`;
- proof of completion: `store._require_evidence` - `succeeded` needs a verified
  receipt or a recorded human action.
"""

from platform_core.agent_runtime.tasks.conditions import (
    ALLOWED_FIELDS,
    ConditionOutcome,
    evaluate_condition,
    validate_condition,
)
from platform_core.agent_runtime.tasks.models import (
    ConversationTask,
    ConversationTaskEvent,
    CopilotDraft,
    SemanticAssessmentRow,
)
from platform_core.agent_runtime.tasks.state_machine import (
    TERMINAL,
    TaskKind,
    TaskStatus,
    TaskTransitionError,
    can_progress,
    check_transition,
    is_terminal,
)
from platform_core.agent_runtime.tasks.store import (
    EVIDENCE_HUMAN_ACTION,
    EVIDENCE_VERIFIED_RECEIPT,
    TaskCommand,
    TaskConflict,
    content_hash,
    create_or_get,
    get_task,
    list_tasks,
    make_local_key,
    record_assessment,
    transition,
)

__all__ = [
    "ALLOWED_FIELDS",
    "EVIDENCE_HUMAN_ACTION",
    "EVIDENCE_VERIFIED_RECEIPT",
    "TERMINAL",
    "ConditionOutcome",
    "ConversationTask",
    "ConversationTaskEvent",
    "CopilotDraft",
    "SemanticAssessmentRow",
    "TaskCommand",
    "TaskConflict",
    "TaskKind",
    "TaskStatus",
    "TaskTransitionError",
    "can_progress",
    "check_transition",
    "content_hash",
    "create_or_get",
    "evaluate_condition",
    "get_task",
    "is_terminal",
    "list_tasks",
    "make_local_key",
    "record_assessment",
    "transition",
    "validate_condition",
]
