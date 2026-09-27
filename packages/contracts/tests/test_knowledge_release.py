"""R2-03 fixed-set knowledge release evidence is comparable and two-person."""

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from pydantic import ValidationError

from platform_contracts.knowledge_release import (
    KnowledgeEvalMetrics,
    KnowledgeEvalRun,
    KnowledgeReleaseApproval,
    evaluate_knowledge_release,
)

TENANT = UUID("01900000-0000-7000-8000-000000000001")
SPACE = UUID("01900000-0000-7000-8000-000000000002")
AUTHOR = UUID("01900000-0000-7000-8000-000000000003")
REVIEWER_A = UUID("01900000-0000-7000-8000-000000000004")
REVIEWER_B = UUID("01900000-0000-7000-8000-000000000005")
NOW = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)


def _run(
    *,
    content_hash: str = "a",
    version: int = 1,
    metrics: KnowledgeEvalMetrics | None = None,
    **overrides,
):
    values = {
        "tenant_id": TENANT,
        "knowledge_space_id": SPACE,
        "document_version_id": UUID(f"01900000-0000-7000-8000-{version:012d}"),
        "knowledge_content_sha256": content_hash * 64,
        "dataset_sha256": "b" * 64,
        "retrieval_config_sha256": "c" * 64,
        "evaluator_version": "knowledge-eval-v1",
        "commit_sha": "d" * 40,
        "evaluated_at": NOW,
        "metrics": metrics
        or KnowledgeEvalMetrics(
            case_count=100,
            grounded_answer_rate=0.93,
            citation_support_rate=0.97,
            retrieval_recall_at_k=0.88,
            unsafe_answer_count=0,
        ),
    }
    values.update(overrides)
    return KnowledgeEvalRun(**values)


def _approvals(candidate: KnowledgeEvalRun) -> tuple[KnowledgeReleaseApproval, ...]:
    return (
        KnowledgeReleaseApproval(
            reviewer_id=REVIEWER_A,
            reviewer_role="knowledge_manager",
            candidate_fingerprint=candidate.fingerprint(),
            approved_at=NOW + timedelta(minutes=1),
        ),
        KnowledgeReleaseApproval(
            reviewer_id=REVIEWER_B,
            reviewer_role="tenant_owner",
            candidate_fingerprint=candidate.fingerprint(),
            approved_at=NOW + timedelta(minutes=2),
        ),
    )


def test_eligible_release_requires_same_eval_basis_two_reviewers_and_no_regression() -> None:
    baseline = _run(content_hash="a", version=1)
    candidate = _run(
        content_hash="e",
        version=2,
        metrics=KnowledgeEvalMetrics(
            case_count=100,
            grounded_answer_rate=0.92,
            citation_support_rate=0.97,
            retrieval_recall_at_k=0.87,
            unsafe_answer_count=0,
        ),
    )

    result = evaluate_knowledge_release(
        baseline,
        candidate,
        author_id=AUTHOR,
        approvals=_approvals(candidate),
    )

    assert result.status == "eligible"
    assert result.reason_code == "KNOWLEDGE_RELEASE_ELIGIBLE"
    assert result.candidate_fingerprint == candidate.fingerprint()


@pytest.mark.parametrize(
    "override",
    [
        {"dataset_sha256": "f" * 64},
        {"retrieval_config_sha256": "f" * 64},
        {"evaluator_version": "knowledge-eval-v2"},
        {"commit_sha": "e" * 40},
        {
            "metrics": KnowledgeEvalMetrics(
                case_count=99,
                grounded_answer_rate=0.93,
                citation_support_rate=0.97,
                retrieval_recall_at_k=0.88,
                unsafe_answer_count=0,
            )
        },
    ],
)
def test_changed_dataset_config_evaluator_code_or_case_count_blocks_comparison(override) -> None:
    baseline = _run(content_hash="a", version=1)
    candidate = _run(content_hash="e", version=2, **override)
    result = evaluate_knowledge_release(
        baseline,
        candidate,
        author_id=AUTHOR,
        approvals=_approvals(candidate),
    )

    assert result.status == "blocked"
    assert result.reason_code == "EVAL_INPUT_MISMATCH"


def test_release_approval_cannot_be_reused_after_candidate_fingerprint_changes() -> None:
    baseline = _run(content_hash="a", version=1)
    candidate = _run(content_hash="e", version=2)
    changed = _run(
        content_hash="e",
        version=2,
        metrics=KnowledgeEvalMetrics(
            case_count=100,
            grounded_answer_rate=0.94,
            citation_support_rate=0.97,
            retrieval_recall_at_k=0.88,
            unsafe_answer_count=0,
        ),
    )
    result = evaluate_knowledge_release(
        baseline,
        changed,
        author_id=AUTHOR,
        approvals=_approvals(candidate),
    )

    assert result.reason_code == "FOUR_EYES_REVIEW_REQUIRED"


def test_author_or_one_reviewer_cannot_satisfy_four_eyes_gate() -> None:
    baseline = _run(content_hash="a", version=1)
    candidate = _run(content_hash="e", version=2)
    one_approval = _approvals(candidate)[:1]
    result = evaluate_knowledge_release(
        baseline,
        candidate,
        author_id=AUTHOR,
        approvals=one_approval,
    )
    self_approvals = (
        KnowledgeReleaseApproval(
            reviewer_id=AUTHOR,
            reviewer_role="knowledge_manager",
            candidate_fingerprint=candidate.fingerprint(),
            approved_at=NOW + timedelta(minutes=1),
        ),
        one_approval[0],
    )
    self_review = evaluate_knowledge_release(
        baseline,
        candidate,
        author_id=AUTHOR,
        approvals=self_approvals,
    )

    assert result.reason_code == "FOUR_EYES_REVIEW_REQUIRED"
    assert self_review.reason_code == "FOUR_EYES_REVIEW_REQUIRED"


def test_unsafe_answers_and_over_threshold_regressions_block_promotion() -> None:
    baseline = _run(content_hash="a", version=1)
    unsafe = _run(
        content_hash="e",
        version=2,
        metrics=KnowledgeEvalMetrics(
            case_count=100,
            grounded_answer_rate=0.93,
            citation_support_rate=0.97,
            retrieval_recall_at_k=0.88,
            unsafe_answer_count=1,
        ),
    )
    regressed = _run(
        content_hash="f",
        version=3,
        metrics=KnowledgeEvalMetrics(
            case_count=100,
            grounded_answer_rate=0.90,
            citation_support_rate=0.97,
            retrieval_recall_at_k=0.88,
            unsafe_answer_count=0,
        ),
    )

    assert (
        evaluate_knowledge_release(
            baseline, unsafe, author_id=AUTHOR, approvals=_approvals(unsafe)
        ).reason_code
        == "UNSAFE_ANSWER_FOUND"
    )
    assert (
        evaluate_knowledge_release(
            baseline, regressed, author_id=AUTHOR, approvals=_approvals(regressed)
        ).reason_code
        == "KNOWLEDGE_QUALITY_REGRESSION"
    )


def test_regression_at_the_declared_two_percentage_point_boundary_is_allowed() -> None:
    baseline = _run(content_hash="a", version=1)
    candidate = _run(
        content_hash="e",
        version=2,
        metrics=KnowledgeEvalMetrics(
            case_count=100,
            grounded_answer_rate=0.91,
            citation_support_rate=0.95,
            retrieval_recall_at_k=0.86,
            unsafe_answer_count=0,
        ),
    )

    result = evaluate_knowledge_release(
        baseline,
        candidate,
        author_id=AUTHOR,
        approvals=_approvals(candidate),
    )
    assert result.status == "eligible"


def test_same_content_cannot_be_released_as_a_new_version_and_time_must_be_utc() -> None:
    baseline = _run(content_hash="a", version=1)
    same_content = _run(content_hash="a", version=2)
    result = evaluate_knowledge_release(
        baseline,
        same_content,
        author_id=AUTHOR,
        approvals=_approvals(same_content),
    )
    assert result.reason_code == "KNOWLEDGE_VERSION_UNCHANGED"

    with pytest.raises(ValidationError, match="timezone-aware UTC"):
        _run(evaluated_at=datetime(2026, 9, 27, 12, 0))
