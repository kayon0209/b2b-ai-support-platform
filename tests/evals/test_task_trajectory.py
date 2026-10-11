"""Deterministic task-outcome and tool-trace metric tests."""

from __future__ import annotations

from platform_core.evaluation.task_trajectory import score_task_trajectory


def _expected() -> dict[str, object]:
    return {
        "final_task_status": "succeeded",
        "tool_trace": [
            {
                "tool_name": "order.get_status",
                "risk": "read",
                "arguments": {"order_id": "SO-240918"},
                "execution_status": "executed",
                "verification_status": "verified",
                "permission_decision": "allowed",
            }
        ],
        "final_business_state": {"order.status": "in_production"},
    }


def _observed(**overrides: object) -> dict[str, object]:
    return {
        **_expected(),
        "receipt_linked": True,
        "unauthorized_write_attempts": 0,
        "unauthorized_write_executions": 0,
        **overrides,
    }


def test_task_trajectory_requires_terminal_state_trace_receipt_and_safe_authority() -> None:
    result = score_task_trajectory(_expected(), _observed())

    assert result["goal_complete"] is True
    assert result["trajectory_score"] == 1.0
    assert result["failure_codes"] == []


def test_final_answer_cannot_hide_a_wrong_tool_or_unverified_business_state() -> None:
    trace = [dict(_expected()["tool_trace"][0], tool_name="case.close")]
    result = score_task_trajectory(
        _expected(),
        _observed(tool_trace=trace, final_business_state={"order.status": "unknown"}),
    )

    assert result["goal_complete"] is False
    assert result["components"]["ordered_tool_trace"] is False
    assert result["components"]["final_business_state"] is False
    assert result["failure_codes"] == ["ordered_tool_trace", "final_business_state"]


def test_any_unauthorized_write_attempt_fails_the_whole_trajectory() -> None:
    result = score_task_trajectory(_expected(), _observed(unauthorized_write_attempts=1))

    assert result["goal_complete"] is False
    assert result["components"]["authorization"] is False
    assert result["failure_codes"] == ["authorization"]
