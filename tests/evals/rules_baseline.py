"""Local deterministic baseline helpers for the frozen semantic-v2 holdout."""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from platform_core.agent_runtime.intent import classify
from platform_core.agent_runtime.semantic.contracts import SCHEMA_VERSION
from platform_core.evaluation.semantic_eval import (
    EvalCase,
    Split,
    compare,
    dataset_hash,
    validate_dataset,
)

from .semantic_v2_dataset import cases_for_split, semantic_v2_cases
from .semantic_v2_manifest import FROZEN_DATASET_HASH
from .tracing import local_observe


def frozen_cases(split: Split = Split.HOLDOUT) -> tuple[list[EvalCase], list[EvalCase], str]:
    """Return all cases, one family-isolated split, and its frozen hash."""
    all_cases = semantic_v2_cases()
    digest = dataset_hash(all_cases)
    if digest != FROZEN_DATASET_HASH:
        raise ValueError("semantic-v2 dataset hash changed; review the manifest before evaluation")
    problems = validate_dataset(all_cases)
    if problems:
        raise ValueError("semantic-v2 dataset is invalid: " + "; ".join(problems))
    return all_cases, cases_for_split(split), digest


def expected_routing(case: EvalCase) -> dict[str, Any]:
    return {
        "case_id": case.case_id,
        "intents": sorted(case.expected_intents),
        "scene": case.expected_scene,
        "business_line": case.expected_business_line,
    }


@local_observe(span_type="agent", name="semantic_v2_rules_baseline_case")
def classify_for_baseline(
    *, case_id: str, text: str, expected_output: str, slices: list[str]
) -> str:
    """Trace one rules decision and attach its synthetic golden to the trace."""
    from deepeval.tracing import update_current_trace

    detection = classify(text)
    output = json.dumps(
        {
            "case_id": case_id,
            "intents": [
                detection.primary_kind.value,
                *(kind.value for kind in detection.secondary_kinds),
            ],
            "scene": detection.scene.value,
            "business_line": detection.business_line.value,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    update_current_trace(
        name=f"rules:{case_id}",
        input=text,
        output=output,
        expected_output=expected_output,
        tags=["semantic-v2", "rules-baseline", *slices],
        metadata={"case_id": case_id, "schema_version": SCHEMA_VERSION},
    )
    return output


def rules_baseline_report(
    *,
    split: Split,
    all_cases: list[EvalCase],
    cases: list[EvalCase],
    outputs: dict[str, dict[str, Any]],
    scores: dict[str, float],
    latencies_ms: dict[str, int],
    trace_results_directory: str,
) -> dict[str, Any]:
    """Aggregate local metrics without LLM judging or customer data."""
    digest = dataset_hash(all_cases)
    manifest = json.loads(
        (Path(__file__).parent / "data" / "semantic_v2_manifest.json").read_text(encoding="utf-8")
    )
    if manifest.get("dataset_hash") != digest:
        raise ValueError("semantic-v2 manifest hash does not match the evaluated cases")
    comparison = compare(
        all_cases,
        split=split,
        case_ids=[case.case_id for case in cases],
        rules_predictions={case_id: list(row["intents"]) for case_id, row in outputs.items()},
        rules_scene_predictions={case_id: str(row["scene"]) for case_id, row in outputs.items()},
        rules_business_line_predictions={
            case_id: str(row["business_line"]) for case_id, row in outputs.items()
        },
        model_predictions=None,
    )
    ordered_latencies = sorted(latencies_ms.values())

    def percentile(fraction: float) -> int | None:
        if not ordered_latencies:
            return None
        return ordered_latencies[max(0, math.ceil(fraction * len(ordered_latencies)) - 1)]

    return {
        "dataset_id": "semantic-v2-r1-balanced-highrisk-synthetic-2026-09-27",
        "dataset_hash": digest,
        "schema_version": SCHEMA_VERSION,
        "split": split.value,
        "case_count": len(cases),
        "provenance": manifest["provenance"],
        "independent_human_review": manifest["independent_human_review"],
        "release_gate_status": manifest["release_gate_status"],
        "production_quality_claim_eligible": False,
        "baseline": "deterministic_rules",
        "model": None,
        "prompt_version": None,
        "model_run_status": "not_run_no_external_model_calls",
        "model_request_attempts": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "exact_full_routing_cases": sum(score == 1.0 for score in scores.values()),
        "deep_eval_full_routing_exact_mean": (
            round(sum(scores.values()) / len(scores), 4) if scores else None
        ),
        "deep_eval_metric_threshold": 0.0,
        "deep_eval_status_is_quality_gate": False,
        "latency_ms": {
            "p50": percentile(0.50),
            "p95": percentile(0.95),
            "p99": percentile(0.99),
        },
        "comparison": comparison.as_dict(),
        "deep_eval_local_results_directory": trace_results_directory,
        "trace_case_count": len(scores),
        "trace_upload_enabled": False,
        "run_finished_at": datetime.now(UTC).isoformat(),
    }


__all__ = ["classify_for_baseline", "expected_routing", "frozen_cases", "rules_baseline_report"]
