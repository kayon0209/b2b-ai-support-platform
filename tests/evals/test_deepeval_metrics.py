from __future__ import annotations

import json

from deepeval.test_case import LLMTestCase

from .deepeval_metrics import TaskQualityRecordExactMetric


def _record(*, status: str = "passed", trajectory: bool | None = True) -> dict[str, object]:
    return {
        "case_id": "synthetic-case-001",
        "status": status,
        "required_components": ["task_trajectory"],
        "component_states": {"task_trajectory": trajectory},
    }


def _test_case(expected: dict[str, object], actual: dict[str, object]) -> LLMTestCase:
    return LLMTestCase(
        input="same synthetic evaluation case",
        expected_output=json.dumps({"task_quality": expected}),
        actual_output=json.dumps({"task_quality": actual}),
    )


def test_quality_metric_passes_same_case_complete_golden() -> None:
    metric = TaskQualityRecordExactMetric()

    assert metric.measure(_test_case(_record(), _record())) == 1.0
    assert metric.success is True
    assert metric.evaluation_cost == 0.0


def test_quality_metric_rejects_missing_or_failed_component_evidence() -> None:
    metric = TaskQualityRecordExactMetric()

    incomplete = _test_case(_record(), _record(status="incomplete", trajectory=None))
    assert metric.measure(incomplete) == 0.0
    assert metric.success is False
    assert metric.measure(_test_case(_record(), _record(status="failed", trajectory=False))) == 0.0


def test_quality_metric_rejects_mismatched_case_identity() -> None:
    expected = _record()
    actual = {**_record(), "case_id": "different-case"}

    assert TaskQualityRecordExactMetric().measure(_test_case(expected, actual)) == 0.0
