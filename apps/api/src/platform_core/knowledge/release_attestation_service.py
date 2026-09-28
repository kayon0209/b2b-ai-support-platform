"""Verify persisted release evidence before human approval or publication."""

from __future__ import annotations

import hashlib
import json
import uuid

from pydantic import ValidationError

from platform_contracts.knowledge_release import evaluate_knowledge_candidate
from platform_contracts.release_attestation import (
    ReleaseEvaluationArtifact,
    SignedReleaseEvaluationArtifact,
)
from platform_core.knowledge.release_models import KnowledgeReleaseEvaluation
from platform_core.knowledge.release_signatures import (
    ReleaseSignatureError,
    configured_approved_release_datasets,
    configured_evaluator_public_keys,
    verify_release_evaluation_artifact,
)


class ReleaseAttestationError(Exception):
    """The stored result cannot be trusted as platform evaluator evidence."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def release_evidence_available(tenant_id: uuid.UUID) -> bool:
    """Whether this tenant has both a trusted key and an approved fixed set."""
    try:
        keys = configured_evaluator_public_keys()
        datasets = configured_approved_release_datasets()
    except ReleaseSignatureError:
        return False
    return bool(keys) and any(scope[0] == tenant_id for scope in datasets)


def verify_persisted_release_evaluation(
    evaluation: KnowledgeReleaseEvaluation,
    *,
    tenant_id: uuid.UUID,
) -> ReleaseEvaluationArtifact:
    """Re-check the signature, configured approvals and immutable row fields."""
    if (
        evaluation.tenant_id != tenant_id
        or evaluation.attestation_json is None
        or evaluation.attestation_sha256 is None
    ):
        raise ReleaseAttestationError("EVALUATOR_PROVENANCE_UNAVAILABLE")
    try:
        signed = SignedReleaseEvaluationArtifact.model_validate(evaluation.attestation_json)
        artifact = verify_release_evaluation_artifact(
            signed,
            trusted_public_keys=configured_evaluator_public_keys(),
        )
        approved = configured_approved_release_datasets().get(
            (tenant_id, artifact.knowledge_space_id)
        )
    except (ValidationError, ReleaseSignatureError) as exc:
        raise ReleaseAttestationError("EVALUATOR_PROVENANCE_INVALID") from exc
    if (
        approved is None
        or approved.sha256 != artifact.dataset_sha256
        or approved.approval_ref != artifact.dataset_approval_ref
    ):
        raise ReleaseAttestationError("EVAL_DATASET_NOT_APPROVED")
    attestation_hash = hashlib.sha256(
        json.dumps(evaluation.attestation_json, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()
    decision = evaluate_knowledge_candidate(artifact.baseline_run, artifact.candidate_run)
    if (
        attestation_hash != evaluation.attestation_sha256
        or artifact.tenant_id != evaluation.tenant_id
        or artifact.draft_id != evaluation.draft_id
        or artifact.knowledge_space_id != evaluation.knowledge_space_id
        or artifact.baseline_run.document_version_id != evaluation.baseline_version_id
        or artifact.candidate_version_id != evaluation.candidate_version_id
        or artifact.baseline_run.model_dump(mode="json") != evaluation.baseline_run
        or artifact.candidate_run.model_dump(mode="json") != evaluation.candidate_run
        or artifact.candidate_run.fingerprint() != evaluation.candidate_fingerprint
        or decision.status != evaluation.status
        or decision.reason_code != evaluation.reason_code
    ):
        raise ReleaseAttestationError("EVALUATOR_PROVENANCE_INVALID")
    return artifact


__all__ = [
    "ReleaseAttestationError",
    "release_evidence_available",
    "verify_persisted_release_evaluation",
]
