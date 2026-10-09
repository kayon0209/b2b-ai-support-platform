"""DeepEval-traced, deterministic rules baseline over semantic-v2 holdout."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

if os.getenv("DEEPEVAL_LOCAL_EVAL_RUN") != "1":
    pytest.skip(
        "run with scripts/run_local_evals.sh to enable local DeepEval tracing",
        allow_module_level=True,
    )

pytest.importorskip("deepeval")

from deepeval import assert_test
from deepeval.dataset import Golden

from platform_core.evaluation.semantic_eval import Split

from .deepeval_metrics import SemanticRoutingExactMatchMetric
from .rules_baseline import (
    classify_for_baseline,
    expected_routing,
    frozen_cases,
    rules_baseline_report,
)
from .tracing import require_tracing

pytestmark = pytest.mark.eval

_ALL_CASES, _HOLDOUT_CASES, _DATASET_HASH = frozen_cases(Split.HOLDOUT)
_CASE_BY_ID = {case.case_id: case for case in _HOLDOUT_CASES}
_GOLDEN_BY_ID = {
    case.case_id: Golden(
        name=case.case_id,
        input=case.text,
        expected_output=json.dumps(expected_routing(case), ensure_ascii=False, sort_keys=True),
        additional_metadata={"case_id": case.case_id, "slices": list(case.slices)},
    )
    for case in _HOLDOUT_CASES
}
_OUTPUTS: dict[str, dict[str, object]] = {}
_SCORES: dict[str, float] = {}
_LATENCIES: dict[str, int] = {}

require_tracing()


@pytest.mark.parametrize("case_id", [case.case_id for case in _HOLDOUT_CASES])
def test_rules_routing_trace(case_id: str) -> None:
    """Run one frozen Golden through the real deterministic classifier."""
    case = _CASE_BY_ID[case_id]
    golden = _GOLDEN_BY_ID[case_id]
    metric = SemanticRoutingExactMatchMetric()
    started = time.monotonic()
    actual_json = classify_for_baseline(
        case_id=case_id,
        text=golden.input,
        expected_output=str(golden.expected_output),
        slices=list(case.slices),
    )
    assert_test(golden=golden, metrics=[metric])
    _OUTPUTS[case_id] = json.loads(actual_json)
    _SCORES[case_id] = float(metric.score or 0.0)
    _LATENCIES[case_id] = int((time.monotonic() - started) * 1000)


def test_zz_rules_baseline_summary_is_local_and_versioned() -> None:
    """Persist one redacted summary after all per-Golden traces are measured."""
    assert len(_OUTPUTS) == len(_HOLDOUT_CASES)
    report = rules_baseline_report(
        split=Split.HOLDOUT,
        all_cases=_ALL_CASES,
        cases=_HOLDOUT_CASES,
        outputs=_OUTPUTS,
        scores=_SCORES,
        latencies_ms=_LATENCIES,
        trace_results_directory=os.environ["DEEPEVAL_RESULTS_FOLDER"],
    )
    assert report["dataset_hash"] == _DATASET_HASH
    assert report["case_count"] == 340
    assert report["trace_case_count"] == 340
    assert report["model_run_status"] == "not_run_no_external_model_calls"
    assert report["trace_upload_enabled"] is False
    assert report["deep_eval_metric_threshold"] == 0.0
    assert report["deep_eval_status_is_quality_gate"] is False
    assert report["independent_human_review"] == "pending"
    assert report["release_gate_status"] == (
        "not eligible for production quality claims until independent annotation review"
    )

    artifact_root = Path(os.environ.get("APP_EVAL_ARTIFACT_DIR", "tests/artifacts"))
    report_path = artifact_root / "semantic-v2-rules-baseline" / "holdout.json"
    report_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    report_path.chmod(0o600)
