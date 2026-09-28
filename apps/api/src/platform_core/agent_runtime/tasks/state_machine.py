"""The task state machine.

One table of legal transitions, checked in one place, because the transitions
are the part a customer can be harmed by. Three properties are enforced here
rather than at the call sites that happen to remember them today:

1. **Terminal states do not move.** `succeeded`, `failed`, `cancelled` are
   final. A late worker holding a stale read cannot reopen a finished task, and
   a retried command cannot resurrect a cancelled one. The only exception is
   `unknown -> succeeded|failed`, and it exists because `unknown` means "we
   cannot tell" - resolving it after reconciliation is the whole point of
   having the state.

2. **Execution is for reads only.** A normal read goes `ready -> executing`;
   the one operator-started `manual_flow` read path may also enter execution
   through its explicit flow executor. A write task cannot skip confirmation.
   This is checked here against the task's `kind`.

3. **Nothing writes `succeeded`.** There is deliberately no transition into
   `succeeded` from `executing` in this table's *caller* contract - the store
   requires a verified receipt or a recorded human action. The transition
   exists; what it demands is enforced at the store boundary.

The graph is small on purpose. R1 is a bounded DAG of at most 5 tasks and depth
3 (see `semantic.contracts`); a state machine that could express arbitrary
workflow would be a workflow engine nobody budgeted for.
"""

from __future__ import annotations

from enum import StrEnum


class TaskStatus(StrEnum):
    PROPOSED = "proposed"
    AWAITING_INPUT = "awaiting_input"
    READY = "ready"
    NEEDS_HUMAN = "needs_human"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    EXECUTING = "executing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNKNOWN = "unknown"
    CANCELLED = "cancelled"
    # An operator-started standard flow with no flow-specific executor. It is
    # deliberately excluded from `can_progress`: a generic task worker must
    # never mistake a catalog instance for executable work.
    MANUAL_FLOW = "manual_flow"


class TaskKind(StrEnum):
    READ = "read"
    WRITE = "write"
    CLARIFY = "clarify"


TERMINAL: frozenset[TaskStatus] = frozenset(
    {TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.CANCELLED}
)

# Legal transitions, ignoring kind. The kind-specific rules are applied in
# `check_transition` on top of this.
_TRANSITIONS: dict[TaskStatus, frozenset[TaskStatus]] = {
    TaskStatus.PROPOSED: frozenset(
        {TaskStatus.AWAITING_INPUT, TaskStatus.READY, TaskStatus.NEEDS_HUMAN, TaskStatus.CANCELLED}
    ),
    TaskStatus.AWAITING_INPUT: frozenset(
        {
            TaskStatus.READY,
            TaskStatus.AWAITING_INPUT,  # a second clarification round
            TaskStatus.NEEDS_HUMAN,
            TaskStatus.CANCELLED,
        }
    ),
    TaskStatus.READY: frozenset(
        {
            TaskStatus.EXECUTING,  # reads only - see check_transition
            TaskStatus.AWAITING_CONFIRMATION,  # writes only
            TaskStatus.AWAITING_INPUT,  # a field turned out to be missing
            TaskStatus.NEEDS_HUMAN,
            TaskStatus.CANCELLED,
        }
    ),
    TaskStatus.NEEDS_HUMAN: frozenset(
        {
            TaskStatus.READY,  # an agent supplied what was missing
            TaskStatus.AWAITING_INPUT,
            TaskStatus.AWAITING_CONFIRMATION,  # a controlled write proposal was prepared
            TaskStatus.NEEDS_HUMAN,  # a manual flow recorded another operator update
            TaskStatus.CANCELLED,
        }
    ),
    TaskStatus.AWAITING_CONFIRMATION: frozenset(
        {
            TaskStatus.EXECUTING,  # after confirmation
            TaskStatus.READY,  # arguments changed; revision bumped
            TaskStatus.NEEDS_HUMAN,  # an unexecuted proposal was withdrawn for handoff
            TaskStatus.CANCELLED,
        }
    ),
    TaskStatus.EXECUTING: frozenset(
        {
            TaskStatus.SUCCEEDED,
            TaskStatus.FAILED,
            TaskStatus.UNKNOWN,
            # A handoff during execution parks the task rather than cancelling
            # it: the external call may already have happened, and cancelling
            # would report an outcome nobody can verify.
            TaskStatus.NEEDS_HUMAN,
        }
    ),
    TaskStatus.UNKNOWN: frozenset(
        {
            # Reconciliation only. Re-executing is not here on purpose: a fresh
            # attempt with the same idempotency key is how a duplicate external
            # write happens.
            TaskStatus.SUCCEEDED,
            TaskStatus.FAILED,
            TaskStatus.NEEDS_HUMAN,
        }
    ),
    TaskStatus.SUCCEEDED: frozenset(),
    TaskStatus.FAILED: frozenset(),
    TaskStatus.CANCELLED: frozenset(),
    TaskStatus.MANUAL_FLOW: frozenset(
        {
            TaskStatus.MANUAL_FLOW,
            TaskStatus.EXECUTING,
            TaskStatus.AWAITING_CONFIRMATION,
            TaskStatus.NEEDS_HUMAN,
            TaskStatus.CANCELLED,
        }
    ),
}


class TaskTransitionError(Exception):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


# States a task may legitimately remain in while something happens to it.
#
# `awaiting_input -> awaiting_input` is a second clarification round. Manual
# flow/needs-human self transitions record operator field updates while
# keeping the task outside generic execution. Each bumps the version and
# appends an event, so each round remains countable.
SELF_TRANSITIONS: frozenset[TaskStatus] = frozenset(
    {TaskStatus.AWAITING_INPUT, TaskStatus.NEEDS_HUMAN, TaskStatus.MANUAL_FLOW}
)


def check_transition(
    current: TaskStatus,
    target: TaskStatus,
    kind: TaskKind,
) -> None:
    """Raise unless `current -> target` is legal for a task of this kind.

    Three rules, in order:

    - a terminal state accepts nothing;
    - only a read may enter `executing` directly from `ready` - a write must
      pass through `awaiting_confirmation`, which is where the confirmation and
      the re-authorization live;
    - a task may not move to the state it is already in, except for the
      explicitly repeatable states in `SELF_TRANSITIONS`. Re-running the same
      command should be a no-op the caller recognises, not a version bump that
      makes an idempotent retry look like a new intent.
    """
    # Checked here and not only in `store.transition`: a terminal state must
    # be final for every caller, and a caller that reaches the state machine
    # directly (a new route, a repair script) would otherwise be able to
    # reopen a finished task.
    if is_terminal(current):
        raise TaskTransitionError("TASK_TERMINAL", f"{current.value} accepts no transition")

    if current is target and current not in SELF_TRANSITIONS:
        raise TaskTransitionError("TASK_ALREADY_IN_STATE", target.value)

    allowed = _TRANSITIONS[current]
    if target not in allowed:
        raise TaskTransitionError("TASK_TRANSITION_NOT_ALLOWED", f"{current.value}->{target.value}")

    if target is TaskStatus.EXECUTING and current in {
        TaskStatus.READY,
        TaskStatus.MANUAL_FLOW,
    }:
        if kind is not TaskKind.READ:
            raise TaskTransitionError(
                "TASK_WRITE_REQUIRES_CONFIRMATION",
                "a write must pass through awaiting_confirmation",
            )
    if target is TaskStatus.AWAITING_CONFIRMATION and kind is not TaskKind.WRITE:
        raise TaskTransitionError(
            "TASK_CONFIRMATION_REQUIRES_WRITE",
            "only a write task may wait for tool confirmation",
        )


def is_terminal(status: TaskStatus) -> bool:
    return status in TERMINAL


def can_progress(status: TaskStatus) -> bool:
    """Whether the scheduler should look at this task.

    `unknown` is excluded: it waits for reconciliation, and polling it would
    either spin or invite a blind retry.
    """
    return status in {
        TaskStatus.PROPOSED,
        TaskStatus.AWAITING_INPUT,
        TaskStatus.READY,
        TaskStatus.AWAITING_CONFIRMATION,
    }


__all__ = [
    "TERMINAL",
    "TaskKind",
    "TaskStatus",
    "TaskTransitionError",
    "can_progress",
    "check_transition",
    "is_terminal",
]
