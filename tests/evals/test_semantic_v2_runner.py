"""Unit checks for the model-runner's safe metric projections."""

from __future__ import annotations

import pytest

from platform_core.agent_runtime.intent import IntentKind
from platform_core.agent_runtime.semantic.context import SYSTEM_PROMPT_VERSION
from platform_core.agent_runtime.semantic.contracts import (
    ModelSemanticOutput,
    SemanticIntent,
    SemanticSlot,
    SemanticTaskKind,
    SlotOrigin,
)
from platform_core.evaluation.semantic_eval import Split, assign_split

from .semantic_v2_dataset import semantic_v2_cases
from .semantic_v2_manifest import FROZEN_DATASET_HASH
from .semantic_v2_runner import (
    CaseRun,
    _capabilities_for_case,
    _confirmed_slots,
    _percentile,
    _safe_validation_bucket,
    select_dev_diagnostic_sample,
    tool_selection_report,
)

pytestmark = pytest.mark.eval


def test_percentiles_use_nearest_rank_and_keep_empty_unmeasured() -> None:
    assert _percentile([], 0.95) is None
    assert _percentile([8, 1, 3, 5, 2, 4, 6, 7, 9, 10], 0.50) == 5
    assert _percentile([8, 1, 3, 5, 2, 4, 6, 7, 9, 10], 0.95) == 10


def test_evaluation_runner_uses_frozen_dataset_and_current_prompt_identity() -> None:
    assert FROZEN_DATASET_HASH == "200cb1fa0a42da8c59083cbb150e8303b4eb4f379420dbed1e9029a532c13608"
    assert SYSTEM_PROMPT_VERSION == "semantic-v7"


def test_only_confirmed_customer_or_receipt_slots_are_scored() -> None:
    output = ModelSemanticOutput(
        primary_intent=IntentKind.BUSINESS_QUERY,
        intents=[
            SemanticIntent(
                task_kind=SemanticTaskKind.READ,
                source_turn_id="current-1",
                slots=[
                    SemanticSlot(
                        name="order_id",
                        value="SO-EV00001",
                        origin=SlotOrigin.CUSTOMER_STATED,
                        confirmed=True,
                    ),
                    SemanticSlot(
                        name="delivery_city",
                        value="上海",
                        origin=SlotOrigin.INFERRED,
                        confirmed=False,
                    ),
                ],
            )
        ],
    )

    assert _confirmed_slots(output) == {"order_id": "SO-EV00001"}


def test_conflicting_confirmed_values_are_withheld() -> None:
    output = ModelSemanticOutput(
        primary_intent=IntentKind.BUSINESS_QUERY,
        intents=[
            SemanticIntent(
                task_kind=SemanticTaskKind.READ,
                source_turn_id="current-1",
                slots=[
                    SemanticSlot(
                        name="order_id",
                        value="SO-EV00001",
                        origin=SlotOrigin.CUSTOMER_STATED,
                        confirmed=True,
                    )
                ],
            ),
            SemanticIntent(
                task_kind=SemanticTaskKind.READ,
                source_turn_id="history-1",
                slots=[
                    SemanticSlot(
                        name="order_id",
                        value="SO-EV00002",
                        origin=SlotOrigin.VERIFIED_RECEIPT,
                        confirmed=True,
                    )
                ],
            ),
        ],
    )

    assert _confirmed_slots(output) == {}


def test_safe_validation_buckets_keep_raw_output_out_of_reports() -> None:
    malformed = "No JSON here; this could echo customer input."
    bucket = _safe_validation_bucket(malformed, turns=[], capabilities={})
    assert bucket == "json_missing"
    assert "customer input" not in bucket

    missing_root_field = _safe_validation_bucket("{}", turns=[], capabilities={})
    assert missing_root_field == "schema_field:primary_intent"


def test_diagnostic_sample_uses_two_dev_families_per_slice() -> None:
    sample = select_dev_diagnostic_sample(semantic_v2_cases())
    groups: dict[str, set[str]] = {}
    for case in sample:
        assert assign_split(case.family) is Split.DEV
        group = case.case_id.split("-")[2]
        groups.setdefault(group, set()).add(case.family)

    assert len(sample) == 12
    assert set(groups) == {"human", "sensitive", "injection", "negation", "multi", "coreference"}
    assert all(len(families) == 2 for families in groups.values())


def test_evaluation_capabilities_reuse_registered_parameter_names() -> None:
    case = next(case for case in semantic_v2_cases() if "order.get_status" in case.available_tools)
    capability = _capabilities_for_case(case)["order.get_status"]
    assert capability.parameter_names == ("order_id",)
    assert capability.required_parameters == ("order_id",)


def test_tool_report_scores_unique_reads_and_counts_unavailable_suggestions() -> None:
    cases = semantic_v2_cases()
    unique_read = next(case for case in cases if case.expected_tools == ("order.get_status",))
    no_capability = next(
        case for case in cases if "injection_attempt" in case.slices and not case.available_tools
    )

    def result(case_id: str, candidates: tuple[str, ...], unavailable: int) -> CaseRun:
        return CaseRun(
            case_id=case_id,
            intents=[],
            scene="",
            business_line="",
            tool_candidates=candidates,
            unavailable_tool_candidate_count=unavailable,
            slots={},
            missing_slots=[],
            validation_status="valid",
            reason_codes=(),
            validation_bucket=None,
            latency_ms=1,
            queue_wait_ms=0,
            prompt_tokens=0,
            completion_tokens=0,
        )

    report = tool_selection_report(
        [unique_read, no_capability],
        [
            result(unique_read.case_id, ("order.get_status",), 0),
            result(no_capability.case_id, ("order.get_status",), 1),
        ],
    )
    assert report["top1_eligible_cases"] == 1
    assert report["top1_accuracy"] == 1.0
    assert report["unavailable_candidate_count"] == 1
    assert report["unavailable_candidate_by_slice"]["injection_attempt"] == 1
    assert report["execution_count"] == 0
