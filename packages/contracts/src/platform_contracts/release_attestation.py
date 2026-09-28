"""Signed provenance contract for a candidate-aware knowledge evaluation."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta

from pydantic import Field, model_validator

from platform_contracts.knowledge_release import KnowledgeEvalRun, StrictReleaseModel


class ReleaseEvaluationArtifact(StrictReleaseModel):
    """The exact result a dedicated evaluator worker attests to."""

    artifact_version: int = Field(default=1, ge=1, le=1)
    run_id: uuid.UUID
    tenant_id: uuid.UUID
    knowledge_space_id: uuid.UUID
    draft_id: uuid.UUID
    candidate_version_id: uuid.UUID
    dataset_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    dataset_approval_ref: str = Field(min_length=1, max_length=127)
    baseline_run: KnowledgeEvalRun
    candidate_run: KnowledgeEvalRun
    baseline_result_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_result_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    retrieval_at: int = Field(ge=0)
    issued_at: datetime
    key_id: str = Field(pattern=r"^[A-Za-z0-9._-]{1,63}$")

    @model_validator(mode="after")
    def comparable_runs(self) -> ReleaseEvaluationArtifact:
        if self.issued_at.tzinfo is None or self.issued_at.utcoffset() != timedelta(0):
            raise ValueError("issued_at must be timezone-aware UTC")
        baseline = self.baseline_run
        candidate = self.candidate_run
        if (
            baseline.tenant_id != self.tenant_id
            or candidate.tenant_id != self.tenant_id
            or baseline.knowledge_space_id != self.knowledge_space_id
            or candidate.knowledge_space_id != self.knowledge_space_id
            or candidate.document_version_id != self.candidate_version_id
            or baseline.document_version_id == self.candidate_version_id
            or baseline.dataset_sha256 != self.dataset_sha256
            or candidate.dataset_sha256 != self.dataset_sha256
            or baseline.parent_snapshot_sha256 is not None
            or candidate.parent_snapshot_sha256 != baseline.knowledge_snapshot_sha256
            or baseline.retrieval_config_sha256 != candidate.retrieval_config_sha256
            or baseline.evaluator_version != candidate.evaluator_version
            or baseline.commit_sha != candidate.commit_sha
            or baseline.metrics.case_count != candidate.metrics.case_count
            or baseline.evaluated_at > candidate.evaluated_at
            or candidate.evaluated_at > self.issued_at
        ):
            raise ValueError("release evaluation runs are not bound to one comparable candidate")
        return self

    def canonical_bytes(self) -> bytes:
        """Canonical JSON bytes covered by the worker signature."""
        return json.dumps(
            self.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")


class SignedReleaseEvaluationArtifact(StrictReleaseModel):
    artifact: ReleaseEvaluationArtifact
    signature_b64url: str = Field(pattern=r"^[A-Za-z0-9_-]{86}$")


class ReleasePostTestArtifact(StrictReleaseModel):
    """The published-version measurement signed by the same worker trust root."""

    artifact_version: int = Field(default=1, ge=1, le=1)
    run_id: uuid.UUID
    evaluation_id: uuid.UUID
    tenant_id: uuid.UUID
    knowledge_space_id: uuid.UUID
    candidate_version_id: uuid.UUID
    candidate_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    dataset_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    dataset_approval_ref: str = Field(min_length=1, max_length=127)
    post_run: KnowledgeEvalRun
    result_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    retrieval_at: int = Field(ge=0)
    issued_at: datetime
    key_id: str = Field(pattern=r"^[A-Za-z0-9._-]{1,63}$")

    @model_validator(mode="after")
    def bound_to_published_version(self) -> ReleasePostTestArtifact:
        if self.issued_at.tzinfo is None or self.issued_at.utcoffset() != timedelta(0):
            raise ValueError("issued_at must be timezone-aware UTC")
        if (
            self.post_run.tenant_id != self.tenant_id
            or self.post_run.knowledge_space_id != self.knowledge_space_id
            or self.post_run.document_version_id != self.candidate_version_id
            or self.post_run.dataset_sha256 != self.dataset_sha256
            or self.post_run.evaluated_at > self.issued_at
        ):
            raise ValueError("post-test run is not bound to the published candidate")
        return self

    def canonical_bytes(self) -> bytes:
        return json.dumps(
            self.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")


class SignedReleasePostTestArtifact(StrictReleaseModel):
    artifact: ReleasePostTestArtifact
    signature_b64url: str = Field(pattern=r"^[A-Za-z0-9_-]{86}$")


__all__ = [
    "ReleaseEvaluationArtifact",
    "ReleasePostTestArtifact",
    "SignedReleaseEvaluationArtifact",
    "SignedReleasePostTestArtifact",
]
