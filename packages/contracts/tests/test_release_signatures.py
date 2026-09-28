"""A generic service cannot turn invented release metrics into trusted evidence."""

import base64
import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import SecretStr, ValidationError

from platform_contracts.knowledge_release import KnowledgeEvalMetrics, KnowledgeEvalRun
from platform_contracts.knowledge_release_dataset import KnowledgeReleaseDatasetManifest
from platform_contracts.release_attestation import (
    ReleaseEvaluationArtifact,
    ReleasePostTestArtifact,
    SignedReleaseEvaluationArtifact,
    SignedReleasePostTestArtifact,
)
from platform_core.knowledge.release_signatures import (
    ReleaseSignatureError,
    parse_approved_release_datasets,
    sign_release_evaluation_artifact,
    sign_release_post_test_artifact,
    verify_release_evaluation_artifact,
    verify_release_post_test_artifact,
)

NOW = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)
TENANT = uuid.UUID("01900000-0000-7000-8000-000000000001")
SPACE = uuid.UUID("01900000-0000-7000-8000-000000000002")
DRAFT = uuid.UUID("01900000-0000-7000-8000-000000000003")
BASELINE = uuid.UUID("01900000-0000-7000-8000-000000000004")
CANDIDATE = uuid.UUID("01900000-0000-7000-8000-000000000005")


def _run(version_id: uuid.UUID, *, candidate: bool) -> KnowledgeEvalRun:
    return KnowledgeEvalRun(
        tenant_id=TENANT,
        knowledge_space_id=SPACE,
        document_version_id=version_id,
        knowledge_content_sha256=("e" if candidate else "a") * 64,
        knowledge_snapshot_sha256=("d" if candidate else "c") * 64,
        parent_snapshot_sha256="c" * 64 if candidate else None,
        dataset_sha256="b" * 64,
        retrieval_config_sha256="f" * 64,
        evaluator_version="release-eval-v1",
        commit_sha="1" * 40,
        evaluated_at=NOW - timedelta(minutes=1 if candidate else 2),
        metrics=KnowledgeEvalMetrics(
            case_count=100,
            grounded_answer_rate=0.95,
            citation_support_rate=0.97,
            retrieval_recall_at_k=0.92,
            unsafe_answer_count=0,
        ),
    )


def _artifact() -> ReleaseEvaluationArtifact:
    return ReleaseEvaluationArtifact(
        run_id=uuid.uuid4(),
        tenant_id=TENANT,
        knowledge_space_id=SPACE,
        draft_id=DRAFT,
        candidate_version_id=CANDIDATE,
        dataset_sha256="b" * 64,
        dataset_approval_ref="KA-APPROVAL-001",
        baseline_run=_run(BASELINE, candidate=False),
        candidate_run=_run(CANDIDATE, candidate=True),
        baseline_result_sha256="2" * 64,
        candidate_result_sha256="3" * 64,
        retrieval_at=int((NOW - timedelta(minutes=2)).timestamp()),
        issued_at=NOW,
        key_id="worker-2026-09",
    )


def _key_pair() -> tuple[SecretStr, str]:
    key = Ed25519PrivateKey.generate()
    private_bytes = key.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_bytes = key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    private = SecretStr(base64.urlsafe_b64encode(private_bytes).rstrip(b"=").decode("ascii"))
    public = base64.urlsafe_b64encode(public_bytes).rstrip(b"=").decode("ascii")
    return private, public


def test_signed_artifact_verifies_only_for_pinned_worker_key() -> None:
    private, public = _key_pair()
    artifact = _artifact()
    signed = sign_release_evaluation_artifact(artifact, private_key=private)

    verified = verify_release_evaluation_artifact(
        signed,
        trusted_public_keys={artifact.key_id: public},
        now=NOW,
        max_age_seconds=3600,
    )
    assert verified == artifact

    _, another_public = _key_pair()
    with pytest.raises(ReleaseSignatureError, match="EVALUATOR_SIGNATURE_INVALID"):
        verify_release_evaluation_artifact(
            signed, trusted_public_keys={artifact.key_id: another_public}, now=NOW
        )
    with pytest.raises(ReleaseSignatureError, match="EVALUATOR_KEY_NOT_TRUSTED"):
        verify_release_evaluation_artifact(signed, trusted_public_keys={}, now=NOW)


def test_signature_rejects_metric_tampering_and_stale_or_future_artifacts() -> None:
    private, public = _key_pair()
    artifact = _artifact()
    signed = sign_release_evaluation_artifact(artifact, private_key=private)
    changed = artifact.model_dump(mode="json")
    changed["candidate_run"]["metrics"]["grounded_answer_rate"] = 0.99
    tampered = SignedReleaseEvaluationArtifact(
        artifact=ReleaseEvaluationArtifact.model_validate(changed),
        signature_b64url=signed.signature_b64url,
    )
    trusted = {artifact.key_id: public}
    with pytest.raises(ReleaseSignatureError, match="EVALUATOR_SIGNATURE_INVALID"):
        verify_release_evaluation_artifact(tampered, trusted_public_keys=trusted, now=NOW)
    with pytest.raises(ReleaseSignatureError, match="EVALUATOR_ATTESTATION_EXPIRED"):
        verify_release_evaluation_artifact(
            signed,
            trusted_public_keys=trusted,
            now=NOW + timedelta(hours=2),
            max_age_seconds=3600,
        )
    with pytest.raises(ReleaseSignatureError, match="EVALUATOR_ATTESTATION_FROM_FUTURE"):
        verify_release_evaluation_artifact(
            signed, trusted_public_keys=trusted, now=NOW - timedelta(hours=1)
        )


def test_artifact_contract_refuses_foreign_tenant_and_mismatched_dataset() -> None:
    data = _artifact().model_dump(mode="json")
    data["tenant_id"] = str(uuid.uuid4())
    with pytest.raises(ValidationError):
        ReleaseEvaluationArtifact.model_validate(data)
    data = _artifact().model_dump(mode="json")
    data["candidate_run"]["dataset_sha256"] = "7" * 64
    with pytest.raises(ValidationError):
        ReleaseEvaluationArtifact.model_validate(data)


def test_fixed_dataset_config_requires_two_reviewers_and_exact_tenant_space() -> None:
    reviewer_a = uuid.uuid4()
    reviewer_b = uuid.uuid4()
    scope = f"{TENANT}/{SPACE}"
    entry = {
        "sha256": "b" * 64,
        "approval_ref": "KA-APPROVAL-001",
        "approved_by": [str(reviewer_a), str(reviewer_b)],
        "object_key": f"{TENANT}/release-datasets/fixed-v1.json",
    }
    approved = parse_approved_release_datasets(json.dumps({scope: entry}))
    assert approved[(TENANT, SPACE)].reviewer_ids == (reviewer_a, reviewer_b)
    assert (TENANT, uuid.uuid4()) not in approved
    assert parse_approved_release_datasets("") == {}

    entry["approved_by"] = [str(reviewer_a), str(reviewer_a)]
    with pytest.raises(ReleaseSignatureError, match="EVAL_DATASET_APPROVAL_CONFIG_INVALID"):
        parse_approved_release_datasets(json.dumps({scope: entry}))


def test_signed_post_test_rejects_changed_unsafe_count() -> None:
    private, public = _key_pair()
    post_run = _run(CANDIDATE, candidate=True)
    artifact = ReleasePostTestArtifact(
        run_id=uuid.uuid4(),
        evaluation_id=uuid.uuid4(),
        tenant_id=TENANT,
        knowledge_space_id=SPACE,
        candidate_version_id=CANDIDATE,
        candidate_fingerprint=post_run.fingerprint(),
        dataset_sha256="b" * 64,
        dataset_approval_ref="KA-APPROVAL-001",
        post_run=post_run,
        result_sha256="4" * 64,
        retrieval_at=int((NOW - timedelta(minutes=1)).timestamp()),
        issued_at=NOW,
        key_id="worker-2026-09",
    )
    signed = sign_release_post_test_artifact(artifact, private_key=private)
    assert (
        verify_release_post_test_artifact(
            signed, trusted_public_keys={artifact.key_id: public}, now=NOW
        )
        == artifact
    )
    changed = artifact.model_dump(mode="json")
    changed["post_run"]["metrics"]["unsafe_answer_count"] = 1
    tampered = SignedReleasePostTestArtifact(
        artifact=ReleasePostTestArtifact.model_validate(changed),
        signature_b64url=signed.signature_b64url,
    )
    with pytest.raises(ReleaseSignatureError, match="EVALUATOR_SIGNATURE_INVALID"):
        verify_release_post_test_artifact(
            tampered, trusted_public_keys={artifact.key_id: public}, now=NOW
        )


def test_dataset_manifest_binds_every_case_to_an_explicit_principal_scope() -> None:
    case = {
        "case_id": "revision-check-01",
        "question": "How do I confirm a board revision?",
        "role": "support_agent",
        "expected_version_keys": ["kb://board-revisions"],
    }
    manifest = KnowledgeReleaseDatasetManifest.model_validate(
        {
            "tenant_id": str(TENANT),
            "knowledge_space_id": str(SPACE),
            "cases": [case],
            "principal_scopes": {
                "revision-check-01": {
                    "principal_types": ["role"],
                    "principal_ids": ["support_agent"],
                }
            },
        }
    )
    assert manifest.cases[0].case_id == "revision-check-01"
    assert manifest.principal_scopes["revision-check-01"].principal_ids == ("support_agent",)

    with pytest.raises(ValidationError):
        KnowledgeReleaseDatasetManifest.model_validate(
            {
                "tenant_id": str(TENANT),
                "knowledge_space_id": str(SPACE),
                "cases": [case],
                "principal_scopes": {},
            }
        )
