from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

from platform_core.agent_runtime.semantic.contracts import (
    ConditionOperator,
)
from platform_core.agent_runtime.tasks.conditions import facts_from_verified_read
from platform_core.agent_runtime.tasks.state_machine import TaskStatus
from platform_core.agent_runtime.tasks_router import _dependency_block_reason


class _ScalarRows:
    def __init__(self, rows: list[object]) -> None:
        self._rows = rows

    def scalars(self) -> _ScalarRows:
        return self

    def all(self) -> list[object]:
        return self._rows


class _Session:
    def __init__(self, rows: list[object]) -> None:
        self._rows = rows

    async def execute(self, _statement: object) -> _ScalarRows:
        return _ScalarRows(self._rows)


def test_order_receipt_projection_uses_only_known_statuses() -> None:
    shipped = facts_from_verified_read(
        "order.get_status",
        {
            "found": True,
            "resource": "orders",
            "record": {"status": "partially_shipped", "account": "acme"},
        },
    )
    producing = facts_from_verified_read(
        "order.get_status",
        {"found": True, "resource": "orders", "status": "in_production"},
    )

    assert shipped == {"order": {"status": "partially_shipped", "shipped": True}}
    assert producing == {"order": {"status": "in_production", "shipped": False}}
    assert (
        facts_from_verified_read(
            "order.get_status", {"found": True, "resource": "orders", "status": "unknown"}
        )
        == {}
    )


def test_non_order_and_missing_records_do_not_supply_condition_facts() -> None:
    assert facts_from_verified_read("case.read", {"found": True, "case": {"status": "open"}}) == {}


def test_unknown_order_state_does_not_become_a_condition_fact() -> None:
    facts = facts_from_verified_read(
        "order.get_status",
        {"found": True, "resource": "orders", "status": "in_transit"},
    )

    assert facts == {}
    assert (
        facts_from_verified_read("order.get_status", {"found": False, "resource": "orders"}) == {}
    )


def _task(*, condition: dict[str, object] | None) -> SimpleNamespace:
    return SimpleNamespace(
        tenant_id=uuid.uuid4(),
        conversation_ref_id=uuid.uuid4(),
        source_turn_id="turn-1",
        depends_on=["read-0"],
        condition=condition,
    )


def _parent(*, status: str = TaskStatus.SUCCEEDED.value) -> SimpleNamespace:
    execution_id = uuid.uuid4()
    return SimpleNamespace(
        task_local_key="read-0",
        status=status,
        execution_id=execution_id,
        completion_evidence=f"tool_receipt:{execution_id}",
        slots=[
            {
                "name": "order_id",
                "value": "SO-9001",
                "origin": "customer_stated",
                "confirmed": True,
            }
        ],
    )


def _verified_order(status: str, account: str = "account-ref") -> SimpleNamespace:
    return SimpleNamespace(
        tool_name="order.get_status",
        sanitized_input={"order_id": "SO-9001"},
        sanitized_output={
            "found": True,
            "resource": "orders",
            "order_id": "SO-9001",
            "status": status,
            "account": account,
        },
    )


def test_unfinished_parent_blocks_dependent_task() -> None:
    task = _task(condition=None)
    result = asyncio.run(
        _dependency_block_reason(
            _Session([_parent(status=TaskStatus.EXECUTING.value)]),
            tenant_id=task.tenant_id,
            task=task,
        )
    )

    assert result == "TASK_DEPENDENCY_PENDING"


def test_true_order_condition_still_requires_execution_time_recheck(monkeypatch) -> None:
    from platform_core.cases import service as case_service
    from platform_core.identity import profile
    from platform_core.tool_gateway import gateway

    parent = _parent()
    task = _task(
        condition={
            "field": "order.shipped",
            "operator": ConditionOperator.EQUALS.value,
            "value": False,
        }
    )

    async def verified_account(*_args: object, **_kwargs: object) -> uuid.UUID:
        return uuid.uuid4()

    async def account_ref(*_args: object, **_kwargs: object) -> str:
        return "account-ref"

    async def verified_read(*_args: object, **_kwargs: object) -> SimpleNamespace:
        return _verified_order("in_production")

    monkeypatch.setattr(case_service, "verified_account_for_conversation", verified_account)
    monkeypatch.setattr(profile, "business_system_ref_for_account", account_ref)
    monkeypatch.setattr(gateway, "load_verified_read_execution", verified_read)

    result = asyncio.run(
        _dependency_block_reason(_Session([parent]), tenant_id=task.tenant_id, task=task)
    )

    assert result == "TASK_DEPENDENCY_EXECUTION_RECHECK_REQUIRED"


def test_false_or_unowned_order_condition_never_authorizes_child(monkeypatch) -> None:
    from platform_core.cases import service as case_service
    from platform_core.identity import profile
    from platform_core.tool_gateway import gateway

    task = _task(
        condition={
            "field": "order.shipped",
            "operator": ConditionOperator.EQUALS.value,
            "value": False,
        }
    )
    parent = _parent()

    async def verified_account(*_args: object, **_kwargs: object) -> uuid.UUID:
        return uuid.uuid4()

    async def account_ref(*_args: object, **_kwargs: object) -> str:
        return "account-ref"

    current_read = _verified_order("shipped")

    async def verified_read(*_args: object, **_kwargs: object) -> SimpleNamespace:
        return current_read

    monkeypatch.setattr(case_service, "verified_account_for_conversation", verified_account)
    monkeypatch.setattr(profile, "business_system_ref_for_account", account_ref)
    monkeypatch.setattr(gateway, "load_verified_read_execution", verified_read)

    unmet = asyncio.run(
        _dependency_block_reason(_Session([parent]), tenant_id=task.tenant_id, task=task)
    )
    assert unmet == "TASK_CONDITION_UNMET"

    current_read = _verified_order("in_production", account="other-account")
    unowned = asyncio.run(
        _dependency_block_reason(_Session([parent]), tenant_id=task.tenant_id, task=task)
    )
    assert unowned == "TASK_DEPENDENCY_CONDITION_UNRESOLVED"


def test_invalid_condition_fails_closed(monkeypatch) -> None:
    from platform_core.cases import service as case_service
    from platform_core.identity import profile
    from platform_core.tool_gateway import gateway

    task = _task(
        condition={
            "field": "order.owner",
            "operator": ConditionOperator.EQUALS.value,
            "value": "acme",
        }
    )
    parent = _parent()

    async def verified_account(*_args: object, **_kwargs: object) -> uuid.UUID:
        return uuid.uuid4()

    async def account_ref(*_args: object, **_kwargs: object) -> str:
        return "account-ref"

    async def verified_read(*_args: object, **_kwargs: object) -> SimpleNamespace:
        return _verified_order("in_production")

    monkeypatch.setattr(case_service, "verified_account_for_conversation", verified_account)
    monkeypatch.setattr(profile, "business_system_ref_for_account", account_ref)
    monkeypatch.setattr(gateway, "load_verified_read_execution", verified_read)

    result = asyncio.run(
        _dependency_block_reason(_Session([parent]), tenant_id=task.tenant_id, task=task)
    )

    assert result == "TASK_DEPENDENCY_CONDITION_INVALID"


def test_verified_read_loader_returns_only_sanitized_read_rows() -> None:
    from platform_core.tool_gateway.gateway import load_verified_read_execution

    row = SimpleNamespace(
        name="order.get_status",
        sanitized_input={"order_id": "SO-9001"},
        sanitized_output={"found": True, "resource": "orders", "status": "shipped"},
    )

    class _Result:
        def one_or_none(self) -> SimpleNamespace:
            return row

    class _QuerySession:
        async def execute(self, _statement: object) -> _Result:
            return _Result()

    result = asyncio.run(
        load_verified_read_execution(
            _QuerySession(), tenant_id=uuid.uuid4(), execution_id=uuid.uuid4()
        )
    )

    assert result is not None
    assert result.tool_name == "order.get_status"
    assert result.sanitized_input == {"order_id": "SO-9001"}
    assert result.sanitized_output["status"] == "shipped"
