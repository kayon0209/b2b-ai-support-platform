"""Release dataset hashes include the tenant-bound ACL evaluation identities."""

import json
from uuid import uuid4

import pytest

from platform_contracts.knowledge_release_dataset import KnowledgeReleaseDatasetManifest
from platform_core.evaluation.runner import EvalCase
from platform_core.knowledge.release_evaluator import (
    ReleaseEvaluationError,
    load_approved_release_dataset,
    release_dataset_sha256,
)
from platform_core.knowledge.release_signatures import ApprovedReleaseDataset
from platform_core.retrieval.hybrid import PrincipalScope


def test_release_dataset_hash_binds_tenant_space_sample_and_principal_scope() -> None:
    case = EvalCase(
        question="How do I confirm a board revision?",
        role="support_agent",
        case_id="revision-check-01",
        expected_version_keys=("kb://board-revisions",),
    )
    tenant_id = uuid4()
    knowledge_space_id = uuid4()
    scopes = {
        case.case_id: PrincipalScope(
            principal_types=("role",),
            principal_ids=("support_agent",),
        )
    }

    dataset_hash = release_dataset_sha256(
        [case],
        tenant_id=tenant_id,
        knowledge_space_id=knowledge_space_id,
        principal_scopes=scopes,
    )
    changed_scope_hash = release_dataset_sha256(
        [case],
        tenant_id=tenant_id,
        knowledge_space_id=knowledge_space_id,
        principal_scopes={
            case.case_id: PrincipalScope(
                principal_types=("role",),
                principal_ids=("knowledge_manager",),
            )
        },
    )

    assert dataset_hash != changed_scope_hash
    assert dataset_hash != release_dataset_sha256(
        [case],
        tenant_id=uuid4(),
        knowledge_space_id=knowledge_space_id,
        principal_scopes=scopes,
    )


def test_approved_dataset_loader_checks_manifest_and_scope_hash(monkeypatch) -> None:
    tenant_id = uuid4()
    space_id = uuid4()
    manifest = {
        "schema_version": 1,
        "tenant_id": str(tenant_id),
        "knowledge_space_id": str(space_id),
        "cases": [
            {
                "case_id": "revision-check-01",
                "question": "How do I confirm a board revision?",
                "role": "support_agent",
                "expected_version_keys": ["kb://board-revisions"],
            }
        ],
        "principal_scopes": {
            "revision-check-01": {
                "principal_types": ["role"],
                "principal_ids": ["support_agent"],
            }
        },
    }
    validated = KnowledgeReleaseDatasetManifest.model_validate(manifest)
    cases = [EvalCase(**case.model_dump()) for case in validated.cases]
    scopes = {
        case_id: PrincipalScope(
            principal_types=scope.principal_types,
            principal_ids=scope.principal_ids,
        )
        for case_id, scope in validated.principal_scopes.items()
    }
    approved_hash = release_dataset_sha256(
        cases,
        tenant_id=tenant_id,
        knowledge_space_id=space_id,
        principal_scopes=scopes,
    )
    approval = ApprovedReleaseDataset(
        sha256=approved_hash,
        approval_ref="approved-fixed-set-1",
        reviewer_ids=(uuid4(), uuid4()),
        object_key=f"{tenant_id}/release-datasets/fixed-v1.json",
    )
    monkeypatch.setattr(
        "platform_core.knowledge.release_evaluator.configured_approved_release_datasets",
        lambda: {(tenant_id, space_id): approval},
    )
    monkeypatch.setattr(
        "platform_core.knowledge.service.get_object",
        lambda _key: json.dumps(manifest).encode("utf-8"),
    )

    loaded = load_approved_release_dataset(
        tenant_id=tenant_id,
        knowledge_space_id=space_id,
    )
    assert loaded.approval.sha256 == approved_hash
    assert loaded.cases[0].case_id == "revision-check-01"
    assert loaded.principal_scopes["revision-check-01"] == scopes["revision-check-01"]

    monkeypatch.setattr(
        "platform_core.knowledge.release_evaluator.configured_approved_release_datasets",
        lambda: {
            (tenant_id, space_id): ApprovedReleaseDataset(
                sha256="f" * 64,
                approval_ref="wrong-fixed-set",
                reviewer_ids=(uuid4(), uuid4()),
                object_key=f"{tenant_id}/release-datasets/fixed-v1.json",
            )
        },
    )
    with pytest.raises(ReleaseEvaluationError, match="EVAL_DATASET_HASH_MISMATCH"):
        load_approved_release_dataset(
            tenant_id=tenant_id,
            knowledge_space_id=space_id,
        )
