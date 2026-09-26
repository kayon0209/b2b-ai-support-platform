"""Task state machine, conditions and content hashing (T04).

These are pure-function tests: the state machine and the condition evaluator
have no I/O, so every claim below is checked without a database. The
database-backed half (RLS, concurrent CAS, cross-tenant negatives) lives in
`apps/api/tests/integration/`.
"""

from __future__ import annotations

import pytest

from platform_core.agent_runtime.semantic.contracts import (
    ConditionOperator,
    SemanticCondition,
    SemanticInvalidOutput,
)
from platform_core.agent_runtime.tasks.conditions import (
    ALLOWED_FIELDS,
    evaluate_condition,
    validate_condition,
)
from platform_core.agent_runtime.tasks.state_machine import (
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
    content_hash,
    make_local_key,
)

READ = TaskKind.READ
WRITE = TaskKind.WRITE
CLARIFY = TaskKind.CLARIFY


# --- terminal states --------------------------------------------------------


@pytest.mark.parametrize(
    "terminal", [TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.CANCELLED]
)
def test_terminal_states_accept_nothing(terminal: TaskStatus) -> None:
    for target in TaskStatus:
        with pytest.raises(TaskTransitionError) as exc:
            check_transition(terminal, target, READ)
        # A terminal state refuses everything, including a self-transition.
        assert exc.value.code in ("TASK_TERMINAL", "TASK_ALREADY_IN_STATE")
    assert is_terminal(terminal)


def test_a_task_cannot_move_to_its_own_state() -> None:
    with pytest.raises(TaskTransitionError) as exc:
        check_transition(TaskStatus.READY, TaskStatus.READY, READ)
    assert exc.value.code == "TASK_ALREADY_IN_STATE"


# --- writes need confirmation ----------------------------------------------


def test_a_read_may_execute_directly() -> None:
    check_transition(TaskStatus.READY, TaskStatus.EXECUTING, READ)


def test_a_write_may_not_skip_confirmation() -> None:
    """The single most important rule in this module.

    A write that could go `ready -> executing` would be an external side effect
    with no confirmation and no re-authorization, reached by a code path that
    merely knew the task's kind.
    """
    with pytest.raises(TaskTransitionError) as exc:
        check_transition(TaskStatus.READY, TaskStatus.EXECUTING, WRITE)
    assert exc.value.code == "TASK_WRITE_REQUIRES_CONFIRMATION"


def test_a_write_reaches_executing_only_through_confirmation() -> None:
    check_transition(TaskStatus.READY, TaskStatus.AWAITING_CONFIRMATION, WRITE)
    check_transition(TaskStatus.AWAITING_CONFIRMATION, TaskStatus.EXECUTING, WRITE)


def test_changed_arguments_return_to_ready_not_execution() -> None:
    """Revoking a confirmation must land somewhere an agent can re-approve.

    After the agent edits the arguments, the task goes back to `ready` and the
    action revision is bumped, so the old confirmation no longer matches. It
    does not go to `executing` - that is the whole point of the revision.
    """
    check_transition(TaskStatus.AWAITING_CONFIRMATION, TaskStatus.READY, WRITE)
    # From `ready` the write still cannot execute directly.
    with pytest.raises(TaskTransitionError) as exc:
        check_transition(TaskStatus.READY, TaskStatus.EXECUTING, WRITE)
    assert exc.value.code == "TASK_WRITE_REQUIRES_CONFIRMATION"


# --- execution outcomes -----------------------------------------------------


def test_executing_may_succeed_fail_or_become_unknown() -> None:
    for target in (TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.UNKNOWN):
        check_transition(TaskStatus.EXECUTING, target, READ)


def test_unknown_is_reconciled_not_retried() -> None:
    """`unknown` may be resolved, never re-executed.

    Re-running an action whose outcome is unknown is how a duplicate external
    write happens, so the transition is absent rather than discouraged.
    """
    check_transition(TaskStatus.UNKNOWN, TaskStatus.SUCCEEDED, WRITE)
    check_transition(TaskStatus.UNKNOWN, TaskStatus.FAILED, WRITE)
    with pytest.raises(TaskTransitionError):
        check_transition(TaskStatus.UNKNOWN, TaskStatus.EXECUTING, WRITE)


def test_a_handoff_during_execution_parks_rather_than_cancels() -> None:
    """The external call may already have happened; cancelling asserts it did not."""
    check_transition(TaskStatus.EXECUTING, TaskStatus.NEEDS_HUMAN, READ)
    with pytest.raises(TaskTransitionError):
        check_transition(TaskStatus.EXECUTING, TaskStatus.CANCELLED, READ)


# --- missing-field cycle ---------------------------------------------------


def test_a_clarification_task_can_be_asked_twice_then_become_ready() -> None:
    check_transition(TaskStatus.PROPOSED, TaskStatus.AWAITING_INPUT, CLARIFY)
    check_transition(TaskStatus.AWAITING_INPUT, TaskStatus.AWAITING_INPUT, CLARIFY)
    check_transition(TaskStatus.AWAITING_INPUT, TaskStatus.READY, CLARIFY)


def test_readiness_can_regress_to_awaiting_input_when_a_field_is_missing() -> None:
    check_transition(TaskStatus.READY, TaskStatus.AWAITING_INPUT, READ)


def test_a_human_task_can_be_unblocked_by_an_agent() -> None:
    check_transition(TaskStatus.NEEDS_HUMAN, TaskStatus.READY, WRITE)


def test_unknown_is_not_polled_by_the_scheduler() -> None:
    assert can_progress(TaskStatus.READY) is True
    assert can_progress(TaskStatus.AWAITING_CONFIRMATION) is True
    assert can_progress(TaskStatus.UNKNOWN) is False
    assert can_progress(TaskStatus.SUCCEEDED) is False


# --- conditions -------------------------------------------------------------


def test_condition_on_a_known_field_resolves() -> None:
    cond = SemanticCondition(
        field="order.status", operator=ConditionOperator.EQUALS, value="shipped"
    )
    out = evaluate_condition(cond, {"order": {"status": "shipped"}})
    assert out.holds is True
    assert out.decidable is True

    out2 = evaluate_condition(cond, {"order": {"status": "packing"}})
    assert out2.holds is False
    assert out2.decidable is True


def test_an_unread_field_is_undecidable_rather_than_false() -> None:
    """The distinction that keeps a task from being silently skipped.

    `decidable=False` means "hold this task". Returning `holds=False` here
    would resolve the dependency as unsatisfied and drop the action; returning
    `holds=True` would run it with a precondition nobody established.
    """
    cond = SemanticCondition(
        field="order.status", operator=ConditionOperator.EQUALS, value="shipped"
    )
    out = evaluate_condition(cond, {})
    assert out.holds is False
    assert out.decidable is False
    assert out.reason == "CONDITION_FIELD_UNKNOWN"


def test_in_operator_compares_membership() -> None:
    cond = SemanticCondition(
        field="order.status", operator=ConditionOperator.IN, value=["shipped", "delivered"]
    )
    assert evaluate_condition(cond, {"order": {"status": "delivered"}}).holds is True
    assert evaluate_condition(cond, {"order": {"status": "packing"}}).holds is False


def test_not_equals_inverts() -> None:
    cond = SemanticCondition(
        field="case.status", operator=ConditionOperator.NOT_EQUALS, value="closed"
    )
    assert evaluate_condition(cond, {"case": {"status": "open"}}).holds is True


def test_a_missing_condition_is_satisfied() -> None:
    assert evaluate_condition(None, {}).holds is True


def test_an_unregistered_field_is_refused_not_evaluated() -> None:
    cond = SemanticCondition(
        field="customer.password", operator=ConditionOperator.EQUALS, value="x"
    )
    with pytest.raises(SemanticInvalidOutput):
        evaluate_condition(cond, {"customer": {"password": "x"}})
    with pytest.raises(SemanticInvalidOutput):
        validate_condition(cond)


def test_registered_fields_are_a_closed_set() -> None:
    assert "order.status" in ALLOWED_FIELDS
    assert "customer.password" not in ALLOWED_FIELDS
    # Every registered field is dot-namespaced; a bare name would be ambiguous
    # about which object it came from.
    assert all("." in f for f in ALLOWED_FIELDS)


def test_a_condition_cannot_execute_anything() -> None:
    """Structural: the evaluator takes a value, never a string to run."""
    cond = SemanticCondition(
        field="order.status",
        operator=ConditionOperator.EQUALS,
        value="'; DROP TABLE conversation_tasks; --",
    )
    out = evaluate_condition(cond, {"order": {"status": "packing"}})
    # Compared as a string, equal to nothing. No evaluation, no execution.
    assert out.holds is False
    assert out.decidable is True


# --- idempotency keying -----------------------------------------------------


def test_local_keys_are_server_shaped_and_distinct() -> None:
    assert make_local_key(TaskKind.READ, 0) == "read-0"
    assert make_local_key(TaskKind.WRITE, 1) == "write-1"
    assert make_local_key(TaskKind.READ, 0) != make_local_key(TaskKind.WRITE, 0)


def test_content_hash_ignores_slot_values() -> None:
    """A retry that re-states the same request with different wording of a
    slot value is the same task.

    This is why slot values are excluded from the hash: including them would
    make every paraphrased redelivery look like a new task and defeat the
    idempotency the key exists to provide.
    """
    a = content_hash(
        kind=READ,
        slots=[
            {"name": "order_no", "value": "SO-1", "origin": "customer_stated", "confirmed": True}
        ],
        missing_slots=[],
        condition=None,
    )
    b = content_hash(
        kind=READ,
        slots=[
            {"name": "order_no", "value": "SO-999", "origin": "customer_stated", "confirmed": True}
        ],
        missing_slots=[],
        condition=None,
    )
    assert a == b


def test_content_hash_notices_a_different_confirmation_state() -> None:
    a = content_hash(
        kind=READ,
        slots=[
            {"name": "order_no", "value": "SO-1", "origin": "customer_stated", "confirmed": True}
        ],
        missing_slots=[],
        condition=None,
    )
    b = content_hash(
        kind=READ,
        slots=[
            {"name": "order_no", "value": "SO-1", "origin": "customer_stated", "confirmed": False}
        ],
        missing_slots=[],
        condition=None,
    )
    assert a != b


def test_content_hash_notices_different_missing_fields() -> None:
    a = content_hash(kind=WRITE, slots=[], missing_slots=["street"], condition=None)
    b = content_hash(kind=WRITE, slots=[], missing_slots=["street", "postal_code"], condition=None)
    assert a != b


def test_content_hash_is_order_independent_for_missing_slots() -> None:
    a = content_hash(kind=WRITE, slots=[], missing_slots=["street", "city"], condition=None)
    b = content_hash(kind=WRITE, slots=[], missing_slots=["city", "street"], condition=None)
    assert a == b


# --- completion evidence ----------------------------------------------------


def test_evidence_prefixes_are_the_only_accepted_forms() -> None:
    assert EVIDENCE_VERIFIED_RECEIPT.startswith("tool_receipt:")
    assert EVIDENCE_HUMAN_ACTION.startswith("human_action:")
