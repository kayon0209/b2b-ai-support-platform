from __future__ import annotations

import pytest

from platform_core.evaluation.human_review_metrics import (
    ReviewDecision,
    ReviewSelection,
    summarize_stratified_review,
)


def test_stratified_override_rate_weights_each_stratum_to_its_population() -> None:
    selections = [
        ReviewSelection("a1", "abstained"),
        ReviewSelection("a2", "abstained"),
        *(ReviewSelection(f"r{index}", "routine") for index in range(10)),
    ]
    decisions = [
        ReviewDecision("a1", "agree"),
        ReviewDecision("a2", "override"),
        *(
            ReviewDecision(f"r{index}", "override" if index == 0 else "agree")
            for index in range(10)
        ),
    ]

    report = summarize_stratified_review({"abstained": 10, "routine": 90}, selections, decisions)

    assert report["status"] == "measured"
    assert report["completion_rate"] == 1.0
    assert report["weighted_override_rate"] == 0.14
    assert report["by_stratum"]["abstained"]["raw_override_rate"] == 0.5
    assert report["by_stratum"]["routine"]["raw_override_rate"] == 0.1


def test_missing_stratum_sample_withholds_population_estimate() -> None:
    report = summarize_stratified_review(
        {"abstained": 10, "routine": 90},
        [ReviewSelection("a1", "abstained")],
        [ReviewDecision("a1", "agree")],
    )

    assert report["status"] == "incomplete"
    assert report["weighted_override_rate"] is None


def test_partial_review_completion_withholds_weighted_estimate() -> None:
    report = summarize_stratified_review(
        {"routine": 2},
        [ReviewSelection("r1", "routine"), ReviewSelection("r2", "routine")],
        [ReviewDecision("r1", "override")],
    )

    assert report["completion_rate"] == 0.5
    assert report["status"] == "incomplete"
    assert report["weighted_override_rate"] is None


def test_duplicate_or_unselected_review_is_rejected() -> None:
    selection = ReviewSelection("r1", "routine")
    with pytest.raises(ValueError, match="unique"):
        summarize_stratified_review(
            {"routine": 2}, [selection, selection], [ReviewDecision("r1", "agree")]
        )
    with pytest.raises(ValueError, match="selected run"):
        summarize_stratified_review({"routine": 2}, [selection], [ReviewDecision("other", "agree")])


def test_empty_population_is_reported_as_unavailable() -> None:
    report = summarize_stratified_review({"routine": 0}, [], [])

    assert report["status"] == "unavailable"
    assert report["weighted_override_rate"] is None
