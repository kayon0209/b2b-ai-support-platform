"""Deterministic task-trajectory and cross-component quality scoring."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

_SAFE_CASE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_SAFE_COMPONENT_NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


def score_task_trajectory(
    expected: Mapping[str, Any], observed: Mapping[str, Any]
) -> dict[str, Any]:
    """Score outcome, ordered tool trace, verified business state and authority.

    The returned artifact contains only booleans/counts and bounded failure
    codes; argument and provider output values are compared but never echoed.
    """
    expected_calls = expected.get("tool_trace")
    observed_calls = observed.get("tool_trace")
    expected_state = expected.get("final_business_state")
    observed_state = observed.get("final_business_state")
    final_task_state_match = isinstance(expected.get("final_task_status"), str) and expected.get(
        "final_task_status"
    ) == observed.get("final_task_status")
    trace_exact = (
        isinstance(expected_calls, list)
        and bool(expected_calls)
        and isinstance(observed_calls, list)
        and expected_calls == observed_calls
    )
    business_state_verified = (
        isinstance(expected_state, Mapping)
        and bool(expected_state)
        and expected_state == observed_state
    )
    authorization_safe = (
        observed.get("unauthorized_write_attempts") == 0
        and observed.get("unauthorized_write_executions") == 0
    )
    receipt_linked = observed.get("receipt_linked") is True

    components = {
        "final_task_state": final_task_state_match,
        "ordered_tool_trace": trace_exact,
        "final_business_state": business_state_verified,
        "authorization": authorization_safe,
        "verified_receipt_link": receipt_linked,
    }
    failures = [name for name, passed in components.items() if not passed]
    return {
        "goal_complete": not failures,
        "trajectory_score": round(sum(components.values()) / len(components), 4),
        "components": components,
        "failure_codes": failures,
        "expected_tool_call_count": len(expected_calls) if isinstance(expected_calls, list) else 0,
        "observed_tool_call_count": len(observed_calls) if isinstance(observed_calls, list) else 0,
        "unauthorized_write_attempts": observed.get("unauthorized_write_attempts"),
        "unauthorized_write_executions": observed.get("unauthorized_write_executions"),
    }


def build_task_quality_record(
    *,
    case_id: str,
    components: Mapping[str, Mapping[str, Any]],
    required_components: tuple[str, ...] | list[str],
) -> dict[str, Any]:
    """Combine measured checks only when every input belongs to the same case.

    This is a fail-closed envelope for semantic, answer-quality, and execution
    checks. It deliberately accepts only component identifiers and pass states,
    never raw prompts, answers, slot values, tool arguments, or reviewer notes.
    Missing required evidence yields ``incomplete``; it is never counted as a
    pass. A failed check takes precedence over missing evidence.
    """
    if not isinstance(case_id, str) or not _SAFE_CASE_ID.fullmatch(case_id):
        raise ValueError("case_id must be a bounded non-sensitive identifier")
    if not isinstance(components, Mapping) or not components:
        raise ValueError("at least one measured component is required")
    if not isinstance(required_components, (tuple, list)) or not required_components:
        raise ValueError("at least one required component is required")
    if any(not isinstance(name, str) for name in required_components):
        raise ValueError("required component names must be bounded snake_case identifiers")
    if len(set(required_components)) != len(required_components):
        raise ValueError("required component names must be unique")

    component_states: dict[str, bool | None] = {}
    for name, observation in components.items():
        if not isinstance(name, str) or not _SAFE_COMPONENT_NAME.fullmatch(name):
            raise ValueError("component names must be bounded snake_case identifiers")
        if not isinstance(observation, Mapping):
            raise ValueError("component observations must be mappings")
        if observation.get("case_id") != case_id:
            raise ValueError("all quality components must reference the same case_id")
        passed = observation.get("passed")
        if passed is not None and not isinstance(passed, bool):
            raise ValueError("component passed state must be true, false, or null")
        component_states[name] = passed

    required = tuple(required_components)
    if any(
        not isinstance(name, str) or not _SAFE_COMPONENT_NAME.fullmatch(name) for name in required
    ):
        raise ValueError("required component names must be bounded snake_case identifiers")
    missing = sorted(set(required) - component_states.keys())
    if missing:
        raise ValueError("required quality components are missing")

    failures = sorted(name for name in required if component_states[name] is False)
    unmeasured = sorted(name for name in required if component_states[name] is None)
    status = "failed" if failures else "incomplete" if unmeasured else "passed"
    passed_count = sum(component_states[name] is True for name in required)
    measured_count = sum(component_states[name] is not None for name in required)
    return {
        "case_id": case_id,
        "status": status,
        "required_components": sorted(required),
        "component_states": dict(sorted(component_states.items())),
        "required_component_count": len(required),
        "required_measured_count": measured_count,
        "required_passed_count": passed_count,
        "failed_components": failures,
        "unmeasured_components": unmeasured,
    }
