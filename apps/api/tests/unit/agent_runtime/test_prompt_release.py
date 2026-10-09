"""Unit tests: prompt version release gate (ticket 38).

The release gate is the only thing standing between an author with good
intentions and a prompt that degrades every customer-visible answer. These
tests pin the two distinct refusals - no evidence vs. P0 regression - and,
equally important, the things the gate must NOT block, because a gate that
refuses too much gets disabled.
"""

import time
import uuid

import pytest

from platform_core.agent_runtime.prompt_release import (
    P0_CATEGORIES,
    CategoryScore,
    EvaluationEvidence,
    Regression,
    ReleaseError,
    check_release_gate,
)


def _evidence(
    *,
    scores: list[CategoryScore] | None = None,
    regressions: list[Regression] | None = None,
) -> EvaluationEvidence:
    return EvaluationEvidence(
        eval_run_id="eval-1",
        scores=scores if scores is not None else [CategoryScore("citation", 18, 20)],
        regressions=regressions or [],
    )


def _human_review_evidence(
    *,
    population: int = 30,
    selected: int = 30,
    override_rate: float = 0.0,
    reason_counts: dict[str, int] | None = None,
    prompt_version_id: uuid.UUID | None = None,
) -> dict[str, object]:
    version_id = prompt_version_id or uuid.uuid4()
    overrides = int(population * override_rate)
    return {
        "evidence_id": str(uuid.uuid4()),
        "evidence_hash": "a" * 64,
        "target_prompt_version_id": str(version_id),
        "created_at": int(time.time()),
        "override_reason_counts": reason_counts
        or ({"wrong_route": overrides} if overrides else {}),
        "summary": {
            "status": "measured",
            "population_count": population,
            "selected_count": selected,
            "reviewed_count": selected,
            "weighted_override_rate": override_rate,
            "by_stratum": {
                "routine": {
                    "population": population,
                    "selected": selected,
                    "reviewed": selected,
                    "override_count": overrides,
                }
            },
        },
    }


# --- Refusals ---------------------------------------------------------------


def test_no_evidence_is_refused() -> None:
    """Promoting on a human's say-so is exactly what the gate forbids."""
    with pytest.raises(ReleaseError) as err:
        check_release_gate(None)

    assert err.value.code == "EVALUATION_REQUIRED"


def test_empty_score_list_is_refused() -> None:
    """An attached-but-empty report is not evidence."""
    with pytest.raises(ReleaseError) as err:
        check_release_gate(_evidence(scores=[]))

    assert err.value.code == "EVALUATION_REQUIRED"


def test_p0_regression_blocks_release() -> None:
    """ "no P0 safety regression" - docs/testing-and-evaluation.md."""
    evidence = _evidence(
        regressions=[
            Regression(
                category="forbidden_claim",
                baseline_rate=0.99,
                candidate_rate=0.90,
                p0=True,
            )
        ]
    )

    with pytest.raises(ReleaseError) as err:
        check_release_gate(evidence)

    assert err.value.code == "P0_REGRESSION"
    assert "forbidden_claim" in err.value.detail


def test_every_p0_category_blocks_release() -> None:
    """The P0 set is not decorative: each member must actually block."""
    for category in sorted(P0_CATEGORIES):
        evidence = _evidence(
            regressions=[
                Regression(category=category, baseline_rate=1.0, candidate_rate=0.5, p0=True)
            ]
        )
        with pytest.raises(ReleaseError) as err:
            check_release_gate(evidence)
        assert err.value.code == "P0_REGRESSION", f"{category} did not block"


def test_multiple_p0_regressions_are_all_named() -> None:
    """The operator needs to know everything that broke, not just the first."""
    evidence = _evidence(
        regressions=[
            Regression(category="injection", baseline_rate=1.0, candidate_rate=0.8, p0=True),
            Regression(category="cross_tenant", baseline_rate=1.0, candidate_rate=0.9, p0=True),
        ]
    )

    with pytest.raises(ReleaseError) as err:
        check_release_gate(evidence)

    assert "injection" in err.value.detail
    assert "cross_tenant" in err.value.detail


# --- Allowed ----------------------------------------------------------------


def test_evidence_without_regressions_is_allowed() -> None:
    assessment = check_release_gate(_evidence(), human_review_evidence=_human_review_evidence())
    assert assessment["passed"] is True


def test_non_p0_regression_does_not_block() -> None:
    """docs/development-plan.md gates on P0 specifically.

    Blocking every metric movement would make the gate unusable, and an
    unusable gate gets worked around.
    """
    evidence = _evidence(
        regressions=[
            Regression(category="latency", baseline_rate=0.95, candidate_rate=0.90, p0=False)
        ]
    )

    assessment = check_release_gate(
        evidence,
        human_review_evidence=_human_review_evidence(),
    )
    assert assessment["passed"] is True


def test_p0_improvement_is_allowed() -> None:
    """A regression list is about getting worse; a same-category gain is fine."""
    evidence = _evidence(
        scores=[CategoryScore("forbidden_claim", 20, 20)],
        regressions=[],
    )

    assessment = check_release_gate(
        evidence,
        human_review_evidence=_human_review_evidence(),
    )
    assert assessment["passed"] is True


def test_missing_human_review_evidence_fails_closed() -> None:
    with pytest.raises(ReleaseError) as err:
        check_release_gate(_evidence())

    assert err.value.code == "HUMAN_REVIEW_REQUIRED"


def test_evaluation_summary_can_report_before_a_candidate_canary() -> None:
    assessment = check_release_gate(_evidence(), require_human_review=False)

    assert assessment["passed"] is False
    assert assessment["code"] == "HUMAN_REVIEW_REQUIRED"


def test_human_review_requires_sample_per_stratum_and_minimum_coverage() -> None:
    evidence = _human_review_evidence(population=30, selected=10)

    with pytest.raises(ReleaseError) as err:
        check_release_gate(_evidence(), human_review_evidence=evidence)

    assert err.value.code == "HUMAN_REVIEW_SAMPLE_TOO_SMALL"


def test_high_risk_override_blocks_even_below_weighted_threshold() -> None:
    evidence = _human_review_evidence(
        override_rate=0.0333,
        reason_counts={"unsafe_action": 1},
    )
    evidence["summary"]["by_stratum"]["routine"]["override_count"] = 1

    with pytest.raises(ReleaseError) as err:
        check_release_gate(_evidence(), human_review_evidence=evidence)

    assert err.value.code == "HUMAN_REVIEW_SAFETY_OVERRIDE"


def test_weighted_review_override_above_ten_percent_blocks() -> None:
    evidence = _human_review_evidence(population=30, selected=30, override_rate=0.2)

    with pytest.raises(ReleaseError) as err:
        check_release_gate(_evidence(), human_review_evidence=evidence)

    assert err.value.code == "HUMAN_REVIEW_OVERRIDE_THRESHOLD"


# --- Evidence summary -------------------------------------------------------


def test_summary_records_the_decision_rationale() -> None:
    """The audit trail must explain the release without re-running evals."""
    evidence = _evidence(
        scores=[
            CategoryScore("citation", 18, 20),
            CategoryScore("forbidden_claim", 20, 20),
        ],
        regressions=[
            Regression(category="latency", baseline_rate=0.95, candidate_rate=0.9, p0=False)
        ],
    )

    summary = evidence.p0_score_summary()

    assert summary["eval_run_id"] == "eval-1"
    assert summary["p0_regressions"] == []
    assert summary["total_regressions"] == 1
    assert summary["categories"]["citation"]["rate"] == 0.9
    assert "forbidden_claim" in summary["p0_categories"]


def test_summary_lists_p0_regressions_when_present() -> None:
    evidence = _evidence(
        regressions=[
            Regression(category="injection", baseline_rate=1.0, candidate_rate=0.7, p0=True)
        ]
    )

    assert evidence.p0_score_summary()["p0_regressions"] == ["injection"]


def test_zero_total_category_scores_full_not_zero() -> None:
    """A category with no cases is not a failure - treat it as unaffected."""
    score = CategoryScore("citation", passed=0, total=0)

    assert score.pass_rate == 1.0
