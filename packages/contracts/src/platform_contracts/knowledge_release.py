"""Evidence contract for fixed-set knowledge releases and rollback decisions."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

MAX_ALLOWED_REGRESSION = 0.02
_FLOAT_EPSILON = 1e-12


class StrictReleaseModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class KnowledgeEvalMetrics(StrictReleaseModel):
    case_count: int = Field(ge=1)
    grounded_answer_rate: float = Field(ge=0, le=1)
    citation_support_rate: float = Field(ge=0, le=1)
    retrieval_recall_at_k: float = Field(ge=0, le=1)
    unsafe_answer_count: int = Field(ge=0)


class KnowledgeEvalRun(StrictReleaseModel):
    """One immutable evaluation result tied to both code and knowledge data."""

    tenant_id: UUID
    knowledge_space_id: UUID
    document_version_id: UUID
    knowledge_content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    knowledge_snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    parent_snapshot_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    dataset_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    retrieval_config_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    evaluator_version: str = Field(min_length=1, max_length=63)
    commit_sha: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    evaluated_at: datetime
    metrics: KnowledgeEvalMetrics

    @model_validator(mode="after")
    def time_is_utc(self) -> KnowledgeEvalRun:
        _require_utc(self.evaluated_at, "evaluated_at")
        return self

    def fingerprint(self) -> str:
        """Bind human approval to this complete, immutable run payload."""
        payload = self.model_dump(mode="json")
        serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


class KnowledgeReleaseApproval(StrictReleaseModel):
    reviewer_id: UUID
    reviewer_role: Literal["knowledge_manager", "tenant_owner"]
    candidate_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    approved_at: datetime

    @model_validator(mode="after")
    def time_is_utc(self) -> KnowledgeReleaseApproval:
        _require_utc(self.approved_at, "approved_at")
        return self


class KnowledgeReleaseGateResult(StrictReleaseModel):
    status: Literal["eligible", "blocked"]
    reason_code: Literal[
        "KNOWLEDGE_RELEASE_ELIGIBLE",
        "KNOWLEDGE_CANDIDATE_ELIGIBLE",
        "EVAL_INPUT_MISMATCH",
        "KNOWLEDGE_VERSION_UNCHANGED",
        "FOUR_EYES_REVIEW_REQUIRED",
        "UNSAFE_ANSWER_FOUND",
        "KNOWLEDGE_QUALITY_REGRESSION",
        "KNOWLEDGE_POST_TEST_PASSED",
        "POST_TEST_INPUT_MISMATCH",
        "POST_TEST_UNSAFE_ANSWER",
        "POST_TEST_QUALITY_REGRESSION",
    ]
    candidate_fingerprint: str
    max_allowed_regression: float
    metric_deltas: dict[str, float]


def evaluate_knowledge_release(
    baseline: KnowledgeEvalRun,
    candidate: KnowledgeEvalRun,
    *,
    author_id: UUID,
    approvals: tuple[KnowledgeReleaseApproval, ...],
) -> KnowledgeReleaseGateResult:
    """Fail closed unless the same authorized eval proves a safe knowledge delta."""
    fingerprint = candidate.fingerprint()
    candidate_gate = evaluate_knowledge_candidate(baseline, candidate)
    deltas = candidate_gate.metric_deltas
    if candidate_gate.status != "eligible":
        return candidate_gate
    approved_by = {
        approval.reviewer_id
        for approval in approvals
        if approval.candidate_fingerprint == fingerprint
        and approval.reviewer_id != author_id
        and approval.approved_at >= candidate.evaluated_at
    }
    if len(approved_by) < 2:
        return _gate("blocked", "FOUR_EYES_REVIEW_REQUIRED", fingerprint, deltas)
    return _gate("eligible", "KNOWLEDGE_RELEASE_ELIGIBLE", fingerprint, deltas)


def evaluate_knowledge_candidate(
    baseline: KnowledgeEvalRun,
    candidate: KnowledgeEvalRun,
) -> KnowledgeReleaseGateResult:
    """Check evidence comparability and metrics before any human approval."""
    fingerprint = candidate.fingerprint()
    deltas = _metric_deltas(baseline.metrics, candidate.metrics)
    if not _same_evaluation_basis(baseline, candidate):
        return _gate("blocked", "EVAL_INPUT_MISMATCH", fingerprint, deltas)
    if (
        baseline.parent_snapshot_sha256 is not None
        or candidate.parent_snapshot_sha256 != baseline.knowledge_snapshot_sha256
        or candidate.knowledge_snapshot_sha256 == baseline.knowledge_snapshot_sha256
    ):
        return _gate("blocked", "EVAL_INPUT_MISMATCH", fingerprint, deltas)
    if (
        baseline.document_version_id == candidate.document_version_id
        or baseline.knowledge_content_sha256 == candidate.knowledge_content_sha256
    ):
        return _gate("blocked", "KNOWLEDGE_VERSION_UNCHANGED", fingerprint, deltas)
    if candidate.metrics.unsafe_answer_count > 0:
        return _gate("blocked", "UNSAFE_ANSWER_FOUND", fingerprint, deltas)
    if any(value < -(MAX_ALLOWED_REGRESSION + _FLOAT_EPSILON) for value in deltas.values()):
        return _gate("blocked", "KNOWLEDGE_QUALITY_REGRESSION", fingerprint, deltas)
    return _gate("eligible", "KNOWLEDGE_CANDIDATE_ELIGIBLE", fingerprint, deltas)


def evaluate_knowledge_post_test(
    candidate_pre_publish: KnowledgeEvalRun,
    post_publish: KnowledgeEvalRun,
) -> KnowledgeReleaseGateResult:
    """Compare the live post-test against the exact candidate build reviewed."""
    fingerprint = post_publish.fingerprint()
    deltas = _metric_deltas(candidate_pre_publish.metrics, post_publish.metrics)
    same_candidate = (
        candidate_pre_publish.tenant_id == post_publish.tenant_id
        and candidate_pre_publish.knowledge_space_id == post_publish.knowledge_space_id
        and candidate_pre_publish.document_version_id == post_publish.document_version_id
        and candidate_pre_publish.knowledge_content_sha256 == post_publish.knowledge_content_sha256
        and candidate_pre_publish.knowledge_snapshot_sha256
        == post_publish.knowledge_snapshot_sha256
        and candidate_pre_publish.parent_snapshot_sha256 == post_publish.parent_snapshot_sha256
        and candidate_pre_publish.dataset_sha256 == post_publish.dataset_sha256
        and candidate_pre_publish.retrieval_config_sha256 == post_publish.retrieval_config_sha256
        and candidate_pre_publish.evaluator_version == post_publish.evaluator_version
        and candidate_pre_publish.commit_sha == post_publish.commit_sha
        and candidate_pre_publish.metrics.case_count == post_publish.metrics.case_count
        and post_publish.evaluated_at > candidate_pre_publish.evaluated_at
    )
    if not same_candidate:
        return _gate("blocked", "POST_TEST_INPUT_MISMATCH", fingerprint, deltas)
    if post_publish.metrics.unsafe_answer_count > 0:
        return _gate("blocked", "POST_TEST_UNSAFE_ANSWER", fingerprint, deltas)
    if any(value < -(MAX_ALLOWED_REGRESSION + _FLOAT_EPSILON) for value in deltas.values()):
        return _gate("blocked", "POST_TEST_QUALITY_REGRESSION", fingerprint, deltas)
    return _gate("eligible", "KNOWLEDGE_POST_TEST_PASSED", fingerprint, deltas)


def _same_evaluation_basis(baseline: KnowledgeEvalRun, candidate: KnowledgeEvalRun) -> bool:
    return (
        baseline.tenant_id == candidate.tenant_id
        and baseline.knowledge_space_id == candidate.knowledge_space_id
        and baseline.dataset_sha256 == candidate.dataset_sha256
        and baseline.retrieval_config_sha256 == candidate.retrieval_config_sha256
        and baseline.evaluator_version == candidate.evaluator_version
        and baseline.commit_sha == candidate.commit_sha
        and baseline.metrics.case_count == candidate.metrics.case_count
    )


def _metric_deltas(
    baseline: KnowledgeEvalMetrics, candidate: KnowledgeEvalMetrics
) -> dict[str, float]:
    return {
        "grounded_answer_rate": candidate.grounded_answer_rate - baseline.grounded_answer_rate,
        "citation_support_rate": candidate.citation_support_rate - baseline.citation_support_rate,
        "retrieval_recall_at_k": candidate.retrieval_recall_at_k - baseline.retrieval_recall_at_k,
    }


def _gate(
    status: Literal["eligible", "blocked"],
    reason_code: Literal[
        "KNOWLEDGE_RELEASE_ELIGIBLE",
        "KNOWLEDGE_CANDIDATE_ELIGIBLE",
        "EVAL_INPUT_MISMATCH",
        "KNOWLEDGE_VERSION_UNCHANGED",
        "FOUR_EYES_REVIEW_REQUIRED",
        "UNSAFE_ANSWER_FOUND",
        "KNOWLEDGE_QUALITY_REGRESSION",
        "KNOWLEDGE_POST_TEST_PASSED",
        "POST_TEST_INPUT_MISMATCH",
        "POST_TEST_UNSAFE_ANSWER",
        "POST_TEST_QUALITY_REGRESSION",
    ],
    fingerprint: str,
    deltas: dict[str, float],
) -> KnowledgeReleaseGateResult:
    return KnowledgeReleaseGateResult(
        status=status,
        reason_code=reason_code,
        candidate_fingerprint=fingerprint,
        max_allowed_regression=MAX_ALLOWED_REGRESSION,
        metric_deltas=deltas,
    )


def _require_utc(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{field_name} must be timezone-aware UTC")


__all__ = [
    "MAX_ALLOWED_REGRESSION",
    "KnowledgeEvalMetrics",
    "KnowledgeEvalRun",
    "KnowledgeReleaseApproval",
    "KnowledgeReleaseGateResult",
    "evaluate_knowledge_candidate",
    "evaluate_knowledge_post_test",
    "evaluate_knowledge_release",
]
