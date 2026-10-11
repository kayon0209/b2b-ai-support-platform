"""Manifest provenance stays attached to rules-only evaluation reports."""

from __future__ import annotations

from platform_core.evaluation.semantic_eval import Split

from .rules_baseline import frozen_cases, rules_baseline_report


def test_rules_baseline_report_preserves_production_claim_boundary() -> None:
    all_cases, holdout, _digest = frozen_cases(Split.HOLDOUT)
    cases = holdout[:3]
    outputs = {
        case.case_id: {
            "intents": sorted(case.expected_intents),
            "scene": case.expected_scene,
            "business_line": case.expected_business_line,
        }
        for case in cases
    }
    report = rules_baseline_report(
        split=Split.HOLDOUT,
        all_cases=all_cases,
        cases=cases,
        outputs=outputs,
        scores={case.case_id: 1.0 for case in cases},
        latencies_ms={case.case_id: 1 for case in cases},
        trace_results_directory="local-only",
    )

    assert report["independent_human_review"] == "pending"
    assert report["deep_eval_metric_threshold"] == 0.0
    assert report["deep_eval_status_is_quality_gate"] is False
    assert report["release_gate_status"] == (
        "not eligible for production quality claims until independent annotation review"
    )
    assert report["production_quality_claim_eligible"] is False
