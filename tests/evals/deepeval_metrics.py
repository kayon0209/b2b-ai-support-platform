"""Model-free DeepEval metrics for local deterministic baselines."""

from __future__ import annotations

import json
from typing import Any

from deepeval.metrics import BaseMetric
from deepeval.test_case import LLMTestCase, SingleTurnParams

from platform_core.evaluation.task_trajectory import score_task_trajectory


class SemanticRoutingExactMatchMetric(BaseMetric):
    """Compare deterministic routing output with the frozen case annotation."""

    _required_params = [
        SingleTurnParams.INPUT,
        SingleTurnParams.ACTUAL_OUTPUT,
        SingleTurnParams.EXPECTED_OUTPUT,
    ]

    def __init__(self) -> None:
        # The first baseline is descriptive, not a release gate: zero makes
        # assert_test record every score without asserting a quality floor.
        self.threshold = 0.0
        self.async_mode = False
        self.verbose_mode = False
        self.include_reason = True
        self.requires_trace = True
        self.evaluation_model = "local-deterministic"
        self.using_native_model = True

    @property
    def __name__(self) -> str:
        return "SemanticRoutingExactMatchMetric"

    def measure(
        self,
        test_case: LLMTestCase,
        *_args: Any,
        **_kwargs: Any,
    ) -> float:
        self.evaluation_cost = 0.0
        self.input_tokens = 0
        self.output_tokens = 0
        try:
            expected = json.loads(test_case.expected_output or "")
            actual = json.loads(test_case.actual_output or "")
        except (TypeError, ValueError):
            self.score = 0.0
            self.success = False
            self.reason = "expected or actual routing output was not valid JSON"
            self.score_breakdown = {"valid_json": 0.0}
            return self.score

        matches = {
            "intents": set(expected.get("intents", [])) == set(actual.get("intents", [])),
            "scene": expected.get("scene") == actual.get("scene"),
            "business_line": expected.get("business_line") == actual.get("business_line"),
        }
        self.score_breakdown = {key: float(value) for key, value in matches.items()}
        self.score = float(all(matches.values()))
        self.success = self.score >= self.threshold
        self.reason = (
            "exact routing contract matched"
            if self.success
            else "one or more routing labels differed from the frozen annotation"
        )
        return self.score

    async def a_measure(
        self,
        test_case: LLMTestCase,
        *_args: Any,
        **_kwargs: Any,
    ) -> float:
        return self.measure(test_case)


class TaskTrajectoryExactMetric(BaseMetric):
    """Score final task state, ordered verified tools and business outcome."""

    _required_params = [
        SingleTurnParams.INPUT,
        SingleTurnParams.ACTUAL_OUTPUT,
        SingleTurnParams.EXPECTED_OUTPUT,
    ]

    def __init__(self) -> None:
        self.threshold = 1.0
        self.async_mode = False
        self.verbose_mode = False
        self.include_reason = True
        self.requires_trace = True
        self.evaluation_model = "local-deterministic"
        self.using_native_model = True

    @property
    def __name__(self) -> str:
        return "TaskTrajectoryExactMetric"

    def measure(
        self,
        test_case: LLMTestCase,
        *_args: Any,
        **_kwargs: Any,
    ) -> float:
        self.evaluation_cost = 0.0
        self.input_tokens = 0
        self.output_tokens = 0
        try:
            expected = json.loads(test_case.expected_output or "")
            observed = json.loads(test_case.actual_output or "")
        except (TypeError, ValueError):
            self.score = 0.0
            self.success = False
            self.reason = "expected or observed task trajectory was not valid JSON"
            self.score_breakdown = {"valid_json": 0.0}
            return self.score

        scored = score_task_trajectory(expected, observed)
        self.score = float(scored["goal_complete"])
        self.success = self.score >= self.threshold
        self.score_breakdown = {key: float(value) for key, value in scored["components"].items()}
        self.reason = (
            "verified task trajectory matched"
            if self.success
            else ";".join(scored["failure_codes"])
        )
        return self.score

    async def a_measure(
        self,
        test_case: LLMTestCase,
        *_args: Any,
        **_kwargs: Any,
    ) -> float:
        return self.measure(test_case)


class TaskQualityRecordExactMetric(BaseMetric):
    """Require a complete, same-case quality record to match its golden."""

    _required_params = [
        SingleTurnParams.INPUT,
        SingleTurnParams.ACTUAL_OUTPUT,
        SingleTurnParams.EXPECTED_OUTPUT,
    ]

    def __init__(self) -> None:
        self.threshold = 1.0
        self.async_mode = False
        self.verbose_mode = False
        self.include_reason = True
        self.requires_trace = True
        self.evaluation_model = "local-deterministic"
        self.using_native_model = True

    @property
    def __name__(self) -> str:
        return "TaskQualityRecordExactMetric"

    def measure(
        self,
        test_case: LLMTestCase,
        *_args: Any,
        **_kwargs: Any,
    ) -> float:
        self.evaluation_cost = 0.0
        self.input_tokens = 0
        self.output_tokens = 0
        try:
            expected = json.loads(test_case.expected_output or "")
            actual = json.loads(test_case.actual_output or "")
            expected_quality = expected["task_quality"]
            actual_quality = actual["task_quality"]
            expected_record = {
                "case_id": expected_quality["case_id"],
                "status": expected_quality["status"],
                "required_components": expected_quality["required_components"],
                "component_states": expected_quality["component_states"],
            }
            actual_record = {
                "case_id": actual_quality["case_id"],
                "status": actual_quality["status"],
                "required_components": actual_quality["required_components"],
                "component_states": actual_quality["component_states"],
            }
        except (KeyError, TypeError, ValueError):
            self.score = 0.0
            self.success = False
            self.reason = "expected or actual task quality record was incomplete"
            self.score_breakdown = {"valid_task_quality_record": 0.0}
            return self.score

        self.score = float(expected_record == actual_record)
        self.success = self.score >= self.threshold
        self.score_breakdown = {
            key: float(expected_record[key] == actual_record[key]) for key in expected_record
        }
        self.reason = (
            "same-case quality record matched"
            if self.success
            else "task quality state or required evidence differed from the golden"
        )
        return self.score

    async def a_measure(
        self,
        test_case: LLMTestCase,
        *_args: Any,
        **_kwargs: Any,
    ) -> float:
        return self.measure(test_case)
