"""Condition evaluation: the only place a task dependency is resolved.

A condition names a field the server registered and an operator from a closed
set, and is compared in Python against data the platform already read. There is
no expression parser, no template, no URL, and no path into the database: the
alternative designs all end with a model's string being evaluated somewhere, and
"a model controls what the condition reads" is the whole class of bug this
feature exists to avoid.

Unknown field or unknown operator raises. It does not evaluate to false,
because "the condition did not hold" and "the condition was nonsense" lead to
opposite operator actions - one retries, the other escalates to a human.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from platform_core.agent_runtime.semantic.contracts import (
    ConditionOperator,
    SemanticCondition,
    SemanticInvalidOutput,
)

# Fields the server is willing to test, and how to read each one from a fact
# set. A field that is not here cannot be named by a model, so adding one is a
# deliberate act with a bounded blast radius.
ConditionReader = Callable[[dict[str, Any]], Any]


def _nested(data: dict[str, Any], path: str) -> Any:
    node: Any = data
    for part in path.split("."):
        if not isinstance(node, dict):
            return None
        node = node.get(part)
    return node


# Read-only accessors. Each is a pure projection of data the platform fetched
# through an authorized read; none of them opens a connection or re-reads a
# source of truth.
REGISTERED_FIELDS: dict[str, ConditionReader] = {
    "order.status": lambda d: _nested(d, "order.status"),
    "order.shipped": lambda d: _nested(d, "order.shipped"),
    "invoice.exists": lambda d: _nested(d, "invoice.exists"),
    "case.status": lambda d: _nested(d, "case.status"),
    "account.contract_status": lambda d: _nested(d, "account.contract_status"),
}

# The same set the semantic validator accepts. Two lists would drift, and a
# field accepted at parse time but unknown here would raise at evaluation -
# which is safe, but a confusing way to learn about it in production.
ALLOWED_FIELDS = frozenset(REGISTERED_FIELDS)


@dataclass(frozen=True)
class ConditionOutcome:
    """The result, and whether it is trustworthy enough to act on."""

    holds: bool
    # False when the field was absent from the fact set. A dependency whose
    # subject has not been read yet is "unknown", not "false" - treating it as
    # false would silently skip the task, and treating it as true would run an
    # action whose precondition was never established.
    decidable: bool
    reason: str


def validate_condition(condition: SemanticCondition | None) -> None:
    """Raise unless the condition is one this server can evaluate."""
    if condition is None:
        return
    if condition.field not in ALLOWED_FIELDS:
        raise SemanticInvalidOutput(
            "SEMANTIC_INVALID_OUTPUT", "condition references an unregistered field"
        )
    if condition.operator is ConditionOperator.IN and not isinstance(condition.value, list):
        raise SemanticInvalidOutput("SEMANTIC_INVALID_OUTPUT", "`in` needs a list")


def evaluate_condition(
    condition: SemanticCondition | None,
    facts: dict[str, Any],
) -> ConditionOutcome:
    """Evaluate a condition against verified facts.

    `facts` is what an authorized read returned. A missing field yields
    `decidable=False` so the caller can hold the task rather than resolve it
    wrongly in either direction.
    """
    if condition is None:
        return ConditionOutcome(holds=True, decidable=True, reason="NO_CONDITION")

    validate_condition(condition)
    reader = REGISTERED_FIELDS[condition.field]
    actual = reader(facts)

    if actual is None:
        return ConditionOutcome(holds=False, decidable=False, reason="CONDITION_FIELD_UNKNOWN")

    op = condition.operator
    expected = condition.value
    try:
        if op is ConditionOperator.EQUALS:
            holds = bool(actual == expected)
        elif op is ConditionOperator.NOT_EQUALS:
            holds = bool(actual != expected)
        else:  # IN
            holds = any(actual == candidate for candidate in (expected or []))
    except TypeError:
        # Comparing incompatible types is not a false condition; it is a
        # condition nobody should have written.
        return ConditionOutcome(holds=False, decidable=False, reason="CONDITION_TYPE_MISMATCH")

    return ConditionOutcome(holds=holds, decidable=True, reason=f"CONDITION_{op.value}")


__all__ = [
    "ALLOWED_FIELDS",
    "REGISTERED_FIELDS",
    "ConditionOutcome",
    "evaluate_condition",
    "validate_condition",
]
