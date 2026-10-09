from __future__ import annotations

import pytest

from platform_core.evaluation.task_trajectory import build_task_quality_record

CASE_ID = "synthetic-case-001"


def test_task_quality_record_requires_all_same_case_checks_to_pass() -> None:
    record = build_task_quality_record(
        case_id=CASE_ID,
        components={
            "semantic_route": {"case_id": CASE_ID, "passed": True},
            "citation_support": {"case_id": CASE_ID, "passed": True},
            "task_trajectory": {"case_id": CASE_ID, "passed": True},
        },
        required_components=["semantic_route", "citation_support", "task_trajectory"],
    )

    assert record["status"] == "passed"
    assert record["required_component_count"] == 3
    assert record["required_measured_count"] == 3
    assert record["required_passed_count"] == 3
    assert record["failed_components"] == []


def test_failed_check_takes_precedence_over_missing_evidence() -> None:
    record = build_task_quality_record(
        case_id=CASE_ID,
        components={
            "semantic_route": {"case_id": CASE_ID, "passed": False},
            "task_trajectory": {"case_id": CASE_ID, "passed": None},
        },
        required_components=["semantic_route", "task_trajectory"],
    )

    assert record["status"] == "failed"
    assert record["failed_components"] == ["semantic_route"]
    assert record["unmeasured_components"] == ["task_trajectory"]


def test_missing_required_measurement_is_incomplete_not_a_pass() -> None:
    record = build_task_quality_record(
        case_id=CASE_ID,
        components={"citation_support": {"case_id": CASE_ID, "passed": None}},
        required_components=["citation_support"],
    )

    assert record["status"] == "incomplete"
    assert record["required_passed_count"] == 0
    assert record["unmeasured_components"] == ["citation_support"]


def test_components_cannot_be_joined_across_different_cases() -> None:
    with pytest.raises(ValueError, match="same case_id"):
        build_task_quality_record(
            case_id=CASE_ID,
            components={
                "semantic_route": {"case_id": CASE_ID, "passed": True},
                "task_trajectory": {"case_id": "another-case", "passed": True},
            },
            required_components=["semantic_route", "task_trajectory"],
        )


def test_raw_text_cannot_be_used_as_case_identity_or_component_evidence() -> None:
    with pytest.raises(ValueError, match="non-sensitive identifier"):
        build_task_quality_record(
            case_id="customer@example.invalid",
            components={"task_trajectory": {"case_id": "customer@example.invalid", "passed": True}},
            required_components=["task_trajectory"],
        )
    with pytest.raises(ValueError, match="true, false, or null"):
        build_task_quality_record(
            case_id=CASE_ID,
            components={"task_trajectory": {"case_id": CASE_ID, "passed": "yes"}},
            required_components=["task_trajectory"],
        )


def test_required_component_names_must_be_unique_and_present() -> None:
    observation = {"case_id": CASE_ID, "passed": True}
    with pytest.raises(ValueError, match="unique"):
        build_task_quality_record(
            case_id=CASE_ID,
            components={"task_trajectory": observation},
            required_components=["task_trajectory", "task_trajectory"],
        )
    with pytest.raises(ValueError, match="required quality components are missing"):
        build_task_quality_record(
            case_id=CASE_ID,
            components={"task_trajectory": observation},
            required_components=["citation_support"],
        )
