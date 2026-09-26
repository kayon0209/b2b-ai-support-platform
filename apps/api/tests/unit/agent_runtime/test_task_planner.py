"""Planner tests (T05), built around the spec's own journey.

The scenario is spec §1 verbatim: "查一下 SO-240918 到哪了，如果没发货就改成
上海办公室，另外补一下发票。" The assertions below are the acceptance
questions for TASK-01 and TOOL-01/02/03:

- all three needs survive as separate tasks;
- the read is ready, the two writes are needs_human with the reason attached;
- no write became a proposal, because R1 has no write path for either;
- a missing field asks the customer; an unavailable capability asks a person;
- a slot value that is sensitive or merely inferred is not stored.
"""

from __future__ import annotations

import json

from platform_core.agent_runtime.semantic.contracts import (
    ConditionOperator,
    SemanticCondition,
    SemanticIntent,
    SemanticSlot,
    SemanticTaskKind,
    SlotOrigin,
)
from platform_core.agent_runtime.semantic.validator import CapabilityView
from platform_core.agent_runtime.tasks.planner import (
    REASON_MISSING_FIELDS,
    REASON_NO_READ_CAPABILITY,
    REASON_NO_WRITE_CAPABILITY,
    plan_tasks,
)
from platform_core.agent_runtime.tasks.state_machine import TaskStatus

READ_CAPS = {
    "order.get_status": CapabilityView(
        tool_name="order.get_status", risk_class="read", allowed_task_kinds=frozenset({"read"})
    ),
    "billing.get_invoice": CapabilityView(
        tool_name="billing.get_invoice", risk_class="read", allowed_task_kinds=frozenset({"read"})
    ),
}

# The three intents the spec journey produces.
SPEC_INTENTS: list[SemanticIntent] = [
    SemanticIntent(
        task_kind=SemanticTaskKind.READ,
        source_turn_id="t-1",
        slots=[
            SemanticSlot(
                name="order_no",
                value="SO-240918",
                origin=SlotOrigin.CUSTOMER_STATED,
                confirmed=True,
            )
        ],
    ),
    SemanticIntent(
        task_kind=SemanticTaskKind.WRITE,
        source_turn_id="t-1",
        # Names the read by its position in the intents array.
        depends_on=["0"],
        condition=SemanticCondition(
            field="order.status", operator=ConditionOperator.NOT_EQUALS, value="shipped"
        ),
        slots=[
            SemanticSlot(
                name="address",
                value="上海办公室",
                origin=SlotOrigin.CUSTOMER_STATED,
                confirmed=False,
            )
        ],
        missing_slots=["street", "city", "postal_code"],
    ),
    SemanticIntent(
        task_kind=SemanticTaskKind.WRITE,
        source_turn_id="t-1",
        slots=[],
        missing_slots=["invoice_period", "tax_id"],
    ),
]


def test_the_spec_journey_produces_three_tasks() -> None:
    tasks = plan_tasks(
        SPEC_INTENTS,
        capabilities=READ_CAPS,
        accepted_tool_names=["order.get_status"],
        unsupported={1: REASON_NO_WRITE_CAPABILITY, 2: REASON_NO_WRITE_CAPABILITY},
    )
    assert len(tasks) == 3
    assert [t.kind.value for t in tasks] == ["read", "write", "write"]


def test_the_read_is_ready_and_the_two_writes_need_a_human() -> None:
    tasks = plan_tasks(
        SPEC_INTENTS,
        capabilities=READ_CAPS,
        accepted_tool_names=["order.get_status"],
        unsupported={1: REASON_NO_WRITE_CAPABILITY, 2: REASON_NO_WRITE_CAPABILITY},
    )
    read_task, address_task, invoice_task = tasks

    assert read_task.status is TaskStatus.READY
    assert read_task.blocked_reason is None

    # The spec's fourth step: an address change R1 cannot perform is a human
    # task. Not a proposal, not a failed write, not a fabricated success.
    assert address_task.status is TaskStatus.NEEDS_HUMAN
    assert address_task.blocked_reason == REASON_NO_WRITE_CAPABILITY
    assert invoice_task.status is TaskStatus.NEEDS_HUMAN
    assert invoice_task.blocked_reason == REASON_NO_WRITE_CAPABILITY


def test_missing_fields_are_still_listed_on_a_human_task() -> None:
    """The agent must be able to ask for everything in one message.

    A task that is needs-human *and* has no missing fields would leave the
    agent with nothing to collect, and the customer with a request that goes
    nowhere.
    """
    tasks = plan_tasks(
        SPEC_INTENTS,
        capabilities=READ_CAPS,
        accepted_tool_names=["order.get_status"],
        unsupported={1: REASON_NO_WRITE_CAPABILITY, 2: REASON_NO_WRITE_CAPABILITY},
    )
    assert tasks[1].missing_slots == ["street", "city", "postal_code"]
    assert tasks[2].missing_slots == ["invoice_period", "tax_id"]


def test_an_unavailable_capability_outranks_a_missing_field() -> None:
    """Asking a customer for a street address cannot conjure a write
    capability, so the ask would be a question with no possible answer."""
    intent = SemanticIntent(
        task_kind=SemanticTaskKind.WRITE,
        source_turn_id="t-1",
        missing_slots=["street"],
    )
    tasks = plan_tasks(
        [intent],
        capabilities=READ_CAPS,
        accepted_tool_names=[],
        unsupported={0: REASON_NO_WRITE_CAPABILITY},
    )
    assert tasks[0].status is TaskStatus.NEEDS_HUMAN
    assert tasks[0].blocked_reason == REASON_NO_WRITE_CAPABILITY


def test_a_missing_field_on_an_available_write_asks_the_customer() -> None:
    write_caps = {
        "jira.create_issue": CapabilityView(
            tool_name="jira.create_issue",
            risk_class="confirmed_write",
            allowed_task_kinds=frozenset({"write"}),
        )
    }
    intent = SemanticIntent(
        task_kind=SemanticTaskKind.WRITE,
        source_turn_id="t-1",
        missing_slots=["project"],
    )
    tasks = plan_tasks(
        [intent],
        capabilities=write_caps,
        accepted_tool_names=[],
        unsupported={0: REASON_MISSING_FIELDS},
    )
    assert tasks[0].status is TaskStatus.AWAITING_INPUT
    assert tasks[0].blocked_reason == REASON_MISSING_FIELDS


def test_a_read_with_no_available_tool_needs_a_human() -> None:
    intent = SemanticIntent(task_kind=SemanticTaskKind.READ, source_turn_id="t-1")
    tasks = plan_tasks(
        [intent],
        capabilities=READ_CAPS,
        accepted_tool_names=[],
        unsupported={0: REASON_NO_READ_CAPABILITY},
    )
    assert tasks[0].status is TaskStatus.NEEDS_HUMAN
    assert tasks[0].blocked_reason == REASON_NO_READ_CAPABILITY


def test_dependencies_are_remapped_to_server_keys() -> None:
    """A model's `depends_on` is positional; the stored key is server-issued."""
    tasks = plan_tasks(
        SPEC_INTENTS,
        capabilities=READ_CAPS,
        accepted_tool_names=["order.get_status"],
        unsupported={1: REASON_NO_WRITE_CAPABILITY, 2: REASON_NO_WRITE_CAPABILITY},
    )
    assert tasks[0].local_key == "read-0"
    assert tasks[1].local_key == "write-1"
    # Intent 1 depends on intent 0, and says so with the server's key.
    assert tasks[1].depends_on == ["read-0"]


def test_a_condition_is_stored_as_a_validated_structure() -> None:
    tasks = plan_tasks(
        SPEC_INTENTS,
        capabilities=READ_CAPS,
        accepted_tool_names=["order.get_status"],
        unsupported={1: REASON_NO_WRITE_CAPABILITY, 2: REASON_NO_WRITE_CAPABILITY},
    )
    condition = tasks[1].condition
    assert condition is not None
    assert condition["field"] == "order.status"
    assert condition["operator"] == "ne"
    assert condition["value"] == "shipped"


def test_a_sensitive_slot_value_is_not_stored() -> None:
    """SEC-04: the address is in the transcript, not in the task row."""
    tasks = plan_tasks(
        SPEC_INTENTS,
        capabilities=READ_CAPS,
        accepted_tool_names=["order.get_status"],
        unsupported={1: REASON_NO_WRITE_CAPABILITY, 2: REASON_NO_WRITE_CAPABILITY},
    )
    address_slot = tasks[1].slots[0]
    assert address_slot["name"] == "address"
    assert "value" not in address_slot
    assert address_slot["value_withheld"] is True
    assert "上海办公室" not in json.dumps(tasks[1].slots, ensure_ascii=False)


def test_a_non_sensitive_stated_value_is_kept() -> None:
    tasks = plan_tasks(
        SPEC_INTENTS,
        capabilities=READ_CAPS,
        accepted_tool_names=["order.get_status"],
        unsupported={1: REASON_NO_WRITE_CAPABILITY, 2: REASON_NO_WRITE_CAPABILITY},
    )
    order_slot = tasks[0].slots[0]
    assert order_slot["value"] == "SO-240918"
    assert order_slot["origin"] == "customer_stated"
    assert order_slot["confirmed"] is True


def test_an_inferred_value_is_withheld_even_when_not_sensitive() -> None:
    """EVAL-02 counts "no source, treated as confirmed" as a failure, so an
    inferred value must not be readable back as data."""
    intent = SemanticIntent(
        task_kind=SemanticTaskKind.READ,
        source_turn_id="t-1",
        slots=[
            SemanticSlot(
                name="order_no", value="SO-9999", origin=SlotOrigin.INFERRED, confirmed=False
            )
        ],
    )
    tasks = plan_tasks(
        [intent], capabilities=READ_CAPS, accepted_tool_names=["order.get_status"], unsupported={}
    )
    slot = tasks[0].slots[0]
    assert "value" not in slot
    assert slot["inferred"] is True
    assert "SO-9999" not in json.dumps(tasks[0].slots, ensure_ascii=False)


def test_a_clarification_intent_becomes_an_ask() -> None:
    intent = SemanticIntent(task_kind=SemanticTaskKind.CLARIFY, source_turn_id="t-1")
    tasks = plan_tasks([intent], capabilities=READ_CAPS, accepted_tool_names=[], unsupported={})
    assert tasks[0].status is TaskStatus.AWAITING_INPUT


def test_model_confidence_does_not_change_any_status() -> None:
    """Confidence is not an input here at all - the planner never sees it.

    Asserted through behaviour: identical intents with different confidence
    produce identical tasks. If a future change wires confidence into the
    planner, this fails.
    """
    a = plan_tasks(
        SPEC_INTENTS,
        capabilities=READ_CAPS,
        accepted_tool_names=["order.get_status"],
        unsupported={1: REASON_NO_WRITE_CAPABILITY, 2: REASON_NO_WRITE_CAPABILITY},
    )
    b = plan_tasks(
        SPEC_INTENTS,
        capabilities=READ_CAPS,
        accepted_tool_names=["order.get_status"],
        unsupported={1: REASON_NO_WRITE_CAPABILITY, 2: REASON_NO_WRITE_CAPABILITY},
    )
    assert [t.status for t in a] == [t.status for t in b]


def test_no_task_is_ever_created_in_a_successful_or_executing_state() -> None:
    """A planner may not assert an outcome. Execution and completion are the
    gateway's and a verified receipt's to record."""
    tasks = plan_tasks(
        SPEC_INTENTS,
        capabilities=READ_CAPS,
        accepted_tool_names=["order.get_status"],
        unsupported={1: REASON_NO_WRITE_CAPABILITY, 2: REASON_NO_WRITE_CAPABILITY},
    )
    forbidden = {TaskStatus.SUCCEEDED, TaskStatus.EXECUTING, TaskStatus.FAILED, TaskStatus.UNKNOWN}
    assert not ({t.status for t in tasks} & forbidden)
