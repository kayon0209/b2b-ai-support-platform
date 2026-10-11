from __future__ import annotations

import asyncio

import pytest

from platform_core.execution_budget import (
    AttemptBudgetExhausted,
    ExecutionBudget,
    current_execution_budget,
    use_execution_budget,
)


def test_attempt_limits_are_shared_across_nested_operation_types() -> None:
    budget = ExecutionBudget.for_seconds(
        deadline_seconds=10,
        max_attempts=3,
        operation_limits={"model": 2, "tool": 2},
        now=100,
    )
    with use_execution_budget(budget):
        assert current_execution_budget() is budget
        assert budget.reserve_attempt("model", now=101) == 9
        assert budget.reserve_attempt("tool", now=102) == 8
        assert budget.reserve_attempt("model", now=103) == 7
        with pytest.raises(AttemptBudgetExhausted, match="total_attempt_budget_exhausted"):
            budget.reserve_attempt("tool", now=104)

    assert current_execution_budget() is None
    assert budget.snapshot()["attempts_by_operation"] == {"model": 2, "tool": 1}


def test_operation_specific_limit_and_deadline_fail_before_dispatch() -> None:
    budget = ExecutionBudget.for_seconds(
        deadline_seconds=1,
        max_attempts=5,
        operation_limits={"model": 1, "tool": 3},
        now=10,
    )
    assert budget.reserve_attempt("model", now=10.5) == 0.5
    with pytest.raises(AttemptBudgetExhausted, match="model_attempt_budget_exhausted"):
        budget.reserve_attempt("model", now=10.6)
    with pytest.raises(AttemptBudgetExhausted, match="deadline_exhausted"):
        budget.reserve_attempt("tool", now=11)


def test_budget_context_is_visible_to_nested_async_calls() -> None:
    budget = ExecutionBudget.for_seconds(
        deadline_seconds=1,
        max_attempts=1,
        operation_limits={"model": 1},
    )

    async def nested() -> ExecutionBudget | None:
        await asyncio.sleep(0)
        return current_execution_budget()

    with use_execution_budget(budget):
        assert asyncio.run(nested()) is budget
