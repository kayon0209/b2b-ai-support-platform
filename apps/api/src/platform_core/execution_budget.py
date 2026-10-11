"""A per-work-item budget shared by nested external-call retry layers."""

from __future__ import annotations

import time
from collections.abc import Awaitable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field


class AttemptBudgetExhausted(RuntimeError):
    """The current work item exhausted its total, operation, or time budget."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass
class ExecutionBudget:
    """Shared attempt counters and monotonic deadline for one work item.

    max_attempts counts actual external request attempts, including the first
    request. Limits are shared by nested provider and connector clients, so an
    outer retry cannot reset the lower-level retry count.
    """

    deadline_at: float
    max_attempts: int
    operation_limits: dict[str, int]
    attempts: dict[str, int] = field(default_factory=dict)
    total_attempts: int = 0

    @classmethod
    def for_seconds(
        cls,
        *,
        deadline_seconds: float,
        max_attempts: int,
        operation_limits: dict[str, int],
        now: float | None = None,
    ) -> ExecutionBudget:
        started = time.monotonic() if now is None else now
        return cls(
            deadline_at=started + max(0.0, float(deadline_seconds)),
            max_attempts=max(0, int(max_attempts)),
            operation_limits={key: max(0, int(value)) for key, value in operation_limits.items()},
        )

    def remaining_seconds(self, *, now: float | None = None) -> float:
        current = time.monotonic() if now is None else now
        return max(0.0, self.deadline_at - current)

    def tighten_deadline(self, deadline_at: float) -> None:
        """Shorten the existing deadline; a replay cannot buy a fresh window."""
        self.deadline_at = min(self.deadline_at, deadline_at)

    def reserve_attempt(self, operation: str, *, now: float | None = None) -> float:
        """Consume one attempt and return the remaining call timeout."""
        remaining = self.remaining_seconds(now=now)
        if remaining <= 0:
            raise AttemptBudgetExhausted("deadline_exhausted")
        if self.total_attempts >= self.max_attempts:
            raise AttemptBudgetExhausted("total_attempt_budget_exhausted")
        used = self.attempts.get(operation, 0)
        limit = self.operation_limits.get(operation, 0)
        if used >= limit:
            raise AttemptBudgetExhausted(f"{operation}_attempt_budget_exhausted")
        self.total_attempts += 1
        self.attempts[operation] = used + 1
        return remaining

    def snapshot(self) -> dict[str, object]:
        return {
            "total_attempts": self.total_attempts,
            "attempts_by_operation": dict(sorted(self.attempts.items())),
            "max_attempts": self.max_attempts,
            "operation_limits": dict(sorted(self.operation_limits.items())),
            "remaining_seconds": round(self.remaining_seconds(), 3),
        }


_current_budget: ContextVar[ExecutionBudget | None] = ContextVar(
    "platform_execution_budget", default=None
)


def current_execution_budget() -> ExecutionBudget | None:
    return _current_budget.get()


@contextmanager
def use_execution_budget(budget: ExecutionBudget) -> Iterator[ExecutionBudget]:
    """Set the budget for nested async calls and restore the prior context."""
    token = _current_budget.set(budget)
    try:
        yield budget
    finally:
        _current_budget.reset(token)


async def run_with_execution_budget[T](budget: ExecutionBudget, awaitable: Awaitable[T]) -> T:
    """Await an operation with the budget active across its nested calls."""
    with use_execution_budget(budget):
        return await awaitable


__all__ = [
    "AttemptBudgetExhausted",
    "ExecutionBudget",
    "current_execution_budget",
    "run_with_execution_budget",
    "use_execution_budget",
]
