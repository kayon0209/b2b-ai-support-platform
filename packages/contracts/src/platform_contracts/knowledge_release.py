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
        "EVAL_INPUT_MISMATCH",
        "KNOWLEDGE_VERSION_UNCHANGED",
        "FOUR_EYES_REVIEW_REQUIRED",
        "UNSAFE_ANSWER_FOUND",
        "KNOWLEDGE_QUALITY_REGRESSION",
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
    deltas = _metric_deltas(baseline.metrics, candidate.metrics)
    if not _same_evaluation_basis(baseline, candidate):
        return _gate("blocked", "EVAL_INPUT_MISMATCH", fingerprint, deltas)
    if (
        baseline.document_version_id == candidate.document_version_id
        or baseline.knowledge_content_sha256 == candidate.knowledge_content_sha256
    ):
        return _gate("blocked", "KNOWLEDGE_VERSION_UNCHANGED", fingerprint, deltas)
    approved_by = {
        approval.reviewer_id
        for approval in approvals
        if approval.candidate_fingerprint == fingerprint
        and approval.reviewer_id != author_id
        and approval.approved_at >= candidate.evaluated_at
    }
    if len(approved_by) < 2:
        return _gate("blocked", "FOUR_EYES_REVIEW_REQUIRED", fingerprint, deltas)
    if candidate.metrics.unsafe_answer_count > 0:
        return _gate("blocked", "UNSAFE_ANSWER_FOUND", fingerprint, deltas)
    if any(value < -(MAX_ALLOWED_REGRESSION + _FLOAT_EPSILON) for value in deltas.values()):
        return _gate("blocked", "KNOWLEDGE_QUALITY_REGRESSION", fingerprint, deltas)
    return _gate("eligible", "KNOWLEDGE_RELEASE_ELIGIBLE", fingerprint, deltas)


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
        "EVAL_INPUT_MISMATCH",
        "KNOWLEDGE_VERSION_UNCHANGED",
        "FOUR_EYES_REVIEW_REQUIRED",
        "UNSAFE_ANSWER_FOUND",
        "KNOWLEDGE_QUALITY_REGRESSION",
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
    "evaluate_knowledge_release",
]
