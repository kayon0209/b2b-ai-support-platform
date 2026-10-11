"""R2-03 publication gate, trusted evaluation, approval, post-test and rollback."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import time
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import create_engine, text

from platform_contracts.knowledge_release import KnowledgeEvalMetrics, KnowledgeEvalRun
from platform_contracts.release_attestation import (
    SignedReleaseEvaluationArtifact,
    SignedReleasePostTestArtifact,
)
from platform_core.agent_runtime.qa_path import DraftAnswer
from platform_core.config import get_settings
from platform_core.evaluation.runner import EvalCase
from platform_core.identity.middleware import TenantContextMiddleware
from platform_core.identity.tenant_context import TenantContext, tenant_repeatable_read_session
from platform_core.knowledge import release_evaluator as api_release_evaluator
from platform_core.knowledge.release_evaluator import (
    ReleaseRetrievalConfig,
    _space_snapshot_sha256,
    release_dataset_sha256,
    release_document_key,
    run_candidate_aware_evaluation,
    run_published_release_post_test,
)
from platform_core.knowledge.release_signatures import (
    ApprovedReleaseDataset,
    sign_release_evaluation_artifact,
)
from platform_core.retrieval.hybrid import DeterministicEmbedder, PrincipalScope

pytestmark = pytest.mark.integration

ADMIN_URL = os.environ.get(
    "APP_ADMIN_DATABASE_URL",
    "postgresql+psycopg://platform:platform@localhost:5435/platform",
)


class _Resolver:
    def __init__(self, tenant_id: uuid.UUID, actor_id: uuid.UUID, role: str, actor_kind: str):
        self.tenant_id = tenant_id
        self.actor_id = actor_id
        self.role = role
        self.actor_kind = actor_kind

    async def __call__(self, _request: object) -> TenantContext:
        return TenantContext(
            tenant_id=self.tenant_id,
            actor_id=self.actor_id,
            actor_kind=self.actor_kind,
            role=self.role,
        )


def _client(
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID,
    role: str,
    actor_kind: str = "user",
) -> TestClient:
    import platform_core.main as main

    app = FastAPI()
    for route in main.app.router.routes:
        app.router.routes.append(route)
    app.add_middleware(
        TenantContextMiddleware,
        resolver=_Resolver(tenant_id, actor_id, role, actor_kind),
    )
    return TestClient(app, raise_server_exceptions=False)


def _headers(key: str | None = None) -> dict[str, str]:
    headers = {"Authorization": "Bearer pt_release_test"}
    if key:
        headers["Idempotency-Key"] = key
    return headers


def test_release_evaluation_requires_service_evidence_two_reviewers_and_supports_rollback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant_id = uuid.uuid4()
    author_id = uuid.uuid4()
    reviewer_a = uuid.uuid4()
    reviewer_b = uuid.uuid4()
    publisher_id = uuid.uuid4()
    evaluator_id = uuid.uuid4()
    space_id = uuid.uuid4()
    source_id = uuid.uuid4()
    baseline_document_id = uuid.uuid4()
    baseline_version_id = uuid.uuid4()
    gap_id = uuid.uuid4()
    draft_id = uuid.uuid4()
    candidate_version_id = uuid.uuid4()
    now = datetime.now(UTC).replace(microsecond=0)
    now_epoch = int(now.timestamp())
    draft_body = "For board revisions, verify the released drawing before production."
    body_hash = hashlib.sha256(draft_body.encode("utf-8")).hexdigest()
    baseline_hash = hashlib.sha256(b"The prior approved production guidance.").hexdigest()
    admin = create_engine(ADMIN_URL)

    class _NoopUpload:
        @staticmethod
        def upload_object(*_args, **_kwargs) -> None:
            return None

    import platform_core.knowledge.service as knowledge_service

    monkeypatch.setattr(knowledge_service, "upload_object", _NoopUpload.upload_object)

    with admin.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO tenants (id, slug, name, status) "
                "VALUES (:id, :slug, 'Knowledge Release Test', 'active')"
            ),
            {"id": tenant_id, "slug": f"knowledge-release-{tenant_id}"},
        )
        conn.execute(
            text(
                "INSERT INTO feature_flags "
                "(id, tenant_id, key, description, enabled, rollout_percent, created_at) "
                "VALUES (:id, :tenant, 'knowledge.release_eval_gate', 'test gate', true, 100, :now)"
            ),
            {"id": uuid.uuid4(), "tenant": tenant_id, "now": now_epoch},
        )
        conn.execute(
            text(
                "INSERT INTO knowledge_spaces (id, tenant_id, name, status) "
                "VALUES (:id, :tenant, 'Controlled knowledge', 'active')"
            ),
            {"id": space_id, "tenant": tenant_id},
        )
        conn.execute(
            text(
                "INSERT INTO knowledge_sources (id, tenant_id, space_id, type, name) "
                "VALUES (:id, :tenant, :space, 'upload', 'Release baseline')"
            ),
            {"id": source_id, "tenant": tenant_id, "space": space_id},
        )
        conn.execute(
            text(
                "INSERT INTO documents "
                "(id, tenant_id, space_id, source_id, canonical_uri, title) "
                "VALUES (:id, :tenant, :space, :source, 'kb://release-baseline', 'Baseline')"
            ),
            {
                "id": baseline_document_id,
                "tenant": tenant_id,
                "space": space_id,
                "source": source_id,
            },
        )
        conn.execute(
            text(
                "INSERT INTO document_versions "
                "(id, tenant_id, document_id, version_label, content_hash, status, object_uri, "
                "ingestion_status, scan_status, created_at, updated_at) VALUES "
                "(:id, :tenant, :document, 'v1', :hash, 'active', 'objects/baseline', 'ready', "
                "'clean', :now, :now)"
            ),
            {
                "id": baseline_version_id,
                "tenant": tenant_id,
                "document": baseline_document_id,
                "hash": baseline_hash,
                "now": now_epoch,
            },
        )
        conn.execute(
            text(
                "INSERT INTO knowledge_gaps "
                "(id, tenant_id, question_hash, sample_question, reason_code, status, "
                "first_seen_at, last_seen_at, target_space_id) VALUES "
                "(:id, :tenant, :hash, 'How do I confirm a board revision?', "
                "'NO_AUTHORIZED_EVIDENCE', "
                "'drafted', :now, :now, :space)"
            ),
            {
                "id": gap_id,
                "tenant": tenant_id,
                "hash": hashlib.sha256(str(gap_id).encode()).hexdigest(),
                "now": now_epoch,
                "space": space_id,
            },
        )
        conn.execute(
            text(
                "INSERT INTO knowledge_drafts "
                "(id, tenant_id, gap_id, title, body, status, author_kind, author_id, "
                "reviewed_by, reviewed_at, review_notes) VALUES "
                "(:id, :tenant, :gap, 'Board revision check', :body, 'approved', 'human', "
                ":author, :reviewer, :now, 'editorial review complete')"
            ),
            {
                "id": draft_id,
                "tenant": tenant_id,
                "gap": gap_id,
                "body": draft_body,
                "author": author_id,
                "reviewer": reviewer_a,
                "now": now_epoch,
            },
        )

    baseline_run = KnowledgeEvalRun(
        tenant_id=tenant_id,
        knowledge_space_id=space_id,
        document_version_id=baseline_version_id,
        knowledge_content_sha256=baseline_hash,
        knowledge_snapshot_sha256="a" * 64,
        parent_snapshot_sha256=None,
        dataset_sha256="b" * 64,
        retrieval_config_sha256="c" * 64,
        evaluator_version="knowledge-eval-v1",
        commit_sha="d" * 40,
        evaluated_at=now - timedelta(minutes=2),
        metrics=KnowledgeEvalMetrics(
            case_count=100,
            grounded_answer_rate=0.94,
            citation_support_rate=0.97,
            retrieval_recall_at_k=0.9,
            unsafe_answer_count=0,
        ),
    )
    candidate_run = KnowledgeEvalRun(
        tenant_id=tenant_id,
        knowledge_space_id=space_id,
        document_version_id=candidate_version_id,
        knowledge_content_sha256=body_hash,
        knowledge_snapshot_sha256="e" * 64,
        parent_snapshot_sha256="a" * 64,
        dataset_sha256="b" * 64,
        retrieval_config_sha256="c" * 64,
        evaluator_version="knowledge-eval-v1",
        commit_sha="d" * 40,
        evaluated_at=now - timedelta(minutes=1),
        metrics=KnowledgeEvalMetrics(
            case_count=100,
            grounded_answer_rate=0.95,
            citation_support_rate=0.97,
            retrieval_recall_at_k=0.9,
            unsafe_answer_count=0,
        ),
    )

    try:
        user_cannot_submit_metrics = _client(
            tenant_id, evaluator_id, "knowledge_manager", "user"
        ).post(
            f"/v1/knowledge/internal/drafts/{draft_id}/release-evaluations",
            headers=_headers("untrusted-eval"),
            json={
                "knowledge_space_id": str(space_id),
                "baseline_run": baseline_run.model_dump(mode="json"),
                "candidate_run": candidate_run.model_dump(mode="json"),
            },
        )
        assert user_cannot_submit_metrics.status_code == 403
        assert user_cannot_submit_metrics.json()["error"]["code"] == "EVALUATOR_SERVICE_REQUIRED"

        evaluator = _client(tenant_id, evaluator_id, "integration_service", "service")
        staged = evaluator.post(
            f"/v1/knowledge/internal/drafts/{draft_id}/release-candidates",
            headers=_headers("stage-release-candidate"),
            json={"knowledge_space_id": str(space_id)},
        )
        assert staged.status_code == 200, staged.text[:300]
        candidate_version_id = uuid.UUID(staged.json()["candidate_version_id"])
        with admin.begin() as conn:
            conn.execute(
                text(
                    "UPDATE document_versions SET ingestion_status = 'ready' "
                    "WHERE tenant_id = :tenant AND id = :version"
                ),
                {"tenant": tenant_id, "version": candidate_version_id},
            )
            conn.execute(
                text(
                    "INSERT INTO chunks (id, tenant_id, document_version_id, section_path, "
                    "ordinal, text, text_hash, metadata) VALUES "
                    "(:id, :tenant, :version, CAST(:section AS jsonb), 0, :text, "
                    ":hash, '{}'::jsonb)"
                ),
                {
                    "id": uuid.uuid4(),
                    "tenant": tenant_id,
                    "version": candidate_version_id,
                    "section": '["Board revision check"]',
                    "text": draft_body,
                    "hash": hashlib.sha256(draft_body.encode()).hexdigest(),
                },
            )
            baseline_excerpt = (
                "Prior production guidance says verify the released drawing before use."
            )
            conn.execute(
                text(
                    "INSERT INTO chunks (id, tenant_id, document_version_id, section_path, "
                    "ordinal, text, text_hash, metadata) VALUES "
                    "(:id, :tenant, :version, CAST(:section AS jsonb), 0, :text, "
                    ":hash, '{}'::jsonb)"
                ),
                {
                    "id": uuid.uuid4(),
                    "tenant": tenant_id,
                    "version": baseline_version_id,
                    "section": '["Prior production guidance"]',
                    "text": baseline_excerpt,
                    "hash": hashlib.sha256(baseline_excerpt.encode()).hexdigest(),
                },
            )

        async def _snapshots() -> tuple[str, str]:
            ctx = TenantContext(tenant_id, evaluator_id, "service", "integration_service")
            async with tenant_repeatable_read_session(ctx) as session:
                baseline = await _space_snapshot_sha256(
                    session,
                    tenant_id=tenant_id,
                    knowledge_space_id=space_id,
                    candidate_version_id=None,
                    now_ts=now_epoch,
                )
                candidate = await _space_snapshot_sha256(
                    session,
                    tenant_id=tenant_id,
                    knowledge_space_id=space_id,
                    candidate_version_id=candidate_version_id,
                    now_ts=now_epoch,
                )
            return baseline, candidate

        baseline_snapshot, candidate_snapshot = asyncio.run(_snapshots())

        candidate_source_key = f"gap-candidate://{draft_id}/{space_id}"
        retrieval_case = EvalCase(
            question="What does production guidance say about the released drawing?",
            required_claims=("released drawing",),
            forbidden_claims=("guaranteed delivery",),
            expected_version_keys=(candidate_source_key,),
            case_id="approved-candidate-read-v1",
        )
        retrieval_scope = PrincipalScope(principal_types=("role",), principal_ids=("tenant_owner",))
        retrieval_scopes = {retrieval_case.case_id: retrieval_scope}
        retrieval_dataset_sha256 = release_dataset_sha256(
            (retrieval_case,),
            tenant_id=tenant_id,
            knowledge_space_id=space_id,
            principal_scopes=retrieval_scopes,
        )
        retrieval_config = ReleaseRetrievalConfig(enabled_paths=("fts", "trigram"))

        async def answer_from_first_source(question, evidence):
            assert question and evidence
            source = evidence[0]
            return DraftAnswer(
                text=source.excerpt,
                claims={0: [source.chunk_id]},
                claim_texts={0: source.excerpt},
            )

        worker_answer_calls = 0

        async def worker_answer_with_post_test_sentinel(question, evidence):
            nonlocal worker_answer_calls
            worker_answer_calls += 1
            draft = await answer_from_first_source(question, evidence)
            if worker_answer_calls >= 3:
                draft.text = f"{draft.text} Guaranteed delivery."
                draft.claim_texts[0] = draft.text
            return draft

        async def _run_candidate_pair():
            ctx = TenantContext(tenant_id, evaluator_id, "service", "integration_service")
            async with tenant_repeatable_read_session(ctx) as session:
                return await run_candidate_aware_evaluation(
                    session,
                    tenant_id=tenant_id,
                    knowledge_space_id=space_id,
                    baseline_version_id=baseline_version_id,
                    candidate_version_id=candidate_version_id,
                    cases=(retrieval_case,),
                    approved_dataset_sha256=retrieval_dataset_sha256,
                    principal_scopes=retrieval_scopes,
                    answer_fn=answer_from_first_source,
                    embedder=DeterministicEmbedder(),
                    embedding_model_id="deterministic-test-embedding",
                    embedding_dimensions=1536,
                    embedding_provider_endpoint="test://local",
                    answerer_version="fake-evaluator-answer-v1",
                    answerer_config_sha256="f" * 64,
                    evaluator_version="candidate-runner-integration-v1",
                    commit_sha="d" * 40,
                    key_of=release_document_key,
                    retrieval_config=retrieval_config,
                    now_ts=now_epoch,
                )

        candidate_pair = asyncio.run(_run_candidate_pair())
        assert candidate_pair.baseline_run.metrics.case_count == 1
        assert candidate_pair.candidate_run.metrics.case_count == 1
        assert candidate_pair.baseline_run.metrics.retrieval_recall_at_k == 0.0
        assert candidate_pair.candidate_run.metrics.retrieval_recall_at_k == 1.0
        assert candidate_pair.candidate_run.metrics.grounded_answer_rate == 1.0
        assert candidate_pair.candidate_run.metrics.citation_support_rate == 1.0
        assert candidate_pair.candidate_run.metrics.unsafe_answer_count == 0
        assert candidate_pair.candidate_run.knowledge_snapshot_sha256 == candidate_snapshot
        assert candidate_pair.candidate_run.parent_snapshot_sha256 == baseline_snapshot

        baseline_run = candidate_pair.baseline_run
        candidate_run = candidate_pair.candidate_run

        signing_key = Ed25519PrivateKey.generate()
        private_bytes = signing_key.private_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PrivateFormat.Raw,
            encryption_algorithm=serialization.NoEncryption(),
        )
        public_bytes = signing_key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        key_id = "release-test-worker"
        signing_secret = SecretStr(
            base64.urlsafe_b64encode(private_bytes).rstrip(b"=").decode("ascii")
        )
        approval_ref = "release-gate-test-approval"
        monkeypatch.setenv(
            "APP_KNOWLEDGE_EVALUATOR_PUBLIC_KEYS_JSON",
            json.dumps(
                {key_id: base64.urlsafe_b64encode(public_bytes).rstrip(b"=").decode("ascii")}
            ),
        )
        monkeypatch.setenv(
            "APP_KNOWLEDGE_EVALUATOR_APPROVED_DATASETS_JSON",
            json.dumps(
                {
                    f"{tenant_id}/{space_id}": {
                        "sha256": retrieval_dataset_sha256,
                        "approval_ref": approval_ref,
                        "approved_by": [str(reviewer_a), str(reviewer_b)],
                        "object_key": f"{tenant_id}/release-datasets/fixed-v1.json",
                    }
                }
            ),
        )
        monkeypatch.setenv("APP_KNOWLEDGE_EVALUATOR_AUTO_RUN", "true")
        monkeypatch.setenv("APP_KNOWLEDGE_EVALUATOR_MAX_CASES_PER_RUN", "100")
        approved_dataset = ApprovedReleaseDataset(
            sha256=retrieval_dataset_sha256,
            approval_ref=approval_ref,
            reviewer_ids=(reviewer_a, reviewer_b),
            object_key=f"{tenant_id}/release-datasets/fixed-v1.json",
        )
        loaded_dataset = api_release_evaluator.LoadedReleaseDataset(
            approval=approved_dataset,
            cases=(retrieval_case,),
            principal_scopes=retrieval_scopes,
        )
        monkeypatch.setattr(
            api_release_evaluator,
            "load_approved_release_dataset",
            lambda **_kwargs: loaded_dataset,
        )
        from worker import release_evaluator as worker_evaluator

        monkeypatch.setattr(
            worker_evaluator,
            "load_approved_release_dataset",
            lambda **_kwargs: loaded_dataset,
        )
        get_settings.cache_clear()
        evaluation_request_body = {
            "knowledge_space_id": str(space_id),
            "baseline_version_id": str(baseline_version_id),
            "candidate_version_id": str(candidate_version_id),
        }
        evaluation_request = evaluator.post(
            f"/v1/knowledge/internal/drafts/{draft_id}/release-evaluation-requests",
            headers=_headers("queue-release-eval"),
            json=evaluation_request_body,
        )
        assert evaluation_request.status_code == 200, evaluation_request.text[:300]
        assert evaluation_request.json()["status"] == "queued"
        evaluation_request_replay = evaluator.post(
            f"/v1/knowledge/internal/drafts/{draft_id}/release-evaluation-requests",
            headers=_headers("queue-release-eval"),
            json=evaluation_request_body,
        )
        assert evaluation_request_replay.status_code == 200
        assert evaluation_request_replay.json()["replayed"] is True
        with admin.connect() as conn:
            queued_payload = conn.execute(
                text("SELECT event_type, payload, status FROM outbox_events WHERE event_id = :id"),
                {"id": evaluation_request.json()["event_id"]},
            ).one()
        assert queued_payload.event_type == "knowledge.release.evaluate_requested"
        assert queued_payload.status == "queued"
        assert set(queued_payload.payload) == {
            "schema_version",
            "draft_id",
            "knowledge_space_id",
            "baseline_version_id",
            "candidate_version_id",
            "dataset_sha256",
            "dataset_approval_ref",
            "idempotency_key_sha256",
            "request_hash",
        }
        assert "question" not in queued_payload.payload

        from worker import release_evaluation_consumer as release_worker

        worker_runtime = release_worker.ReleaseEvaluatorRuntime(
            answer_fn=worker_answer_with_post_test_sentinel,
            embedder=DeterministicEmbedder(),
            embedding_model_id="deterministic-test-embedding",
            embedding_dimensions=1536,
            embedding_provider_endpoint="test://local",
            answerer_version="fake-evaluator-answer-v1",
            answerer_config_sha256="f" * 64,
            evaluator_version="candidate-runner-integration-v1",
            commit_sha="d" * 40,
            key_id=key_id,
            private_key=signing_secret,
            max_cases_per_run=100,
        )
        worker = release_worker.ReleaseEvaluationWorker(worker_runtime)
        assert asyncio.run(worker.run_once()) == 1
        assert worker_answer_calls == 2
        with admin.connect() as conn:
            queue_receipt = conn.execute(
                text(
                    "SELECT status, attempts, processing_token, last_error, "
                    "first_attempt_at, deadline_at, external_attempt_limit "
                    "FROM outbox_events WHERE event_id = :id"
                ),
                {"id": evaluation_request.json()["event_id"]},
            ).one()
        assert queue_receipt.status == "sent"
        assert queue_receipt.attempts == 1
        assert queue_receipt.processing_token is None
        assert queue_receipt.last_error is None
        assert queue_receipt.first_attempt_at is not None
        assert queue_receipt.deadline_at > queue_receipt.first_attempt_at
        assert queue_receipt.external_attempt_limit > 0
        with admin.connect() as conn:
            release_row = conn.execute(
                text(
                    "SELECT id::text, status, attestation_json FROM knowledge_release_evaluations "
                    "WHERE tenant_id = :tenant AND candidate_version_id = :candidate"
                ),
                {"tenant": tenant_id, "candidate": candidate_version_id},
            ).one()
        assert release_row.status == "eligible"
        signed = SignedReleaseEvaluationArtifact.model_validate(release_row.attestation_json)
        baseline_run = signed.artifact.baseline_run
        candidate_run = signed.artifact.candidate_run
        release_eval_id = release_row.id
        request_body = {
            "knowledge_space_id": str(space_id),
            "baseline_run": baseline_run.model_dump(mode="json"),
            "candidate_run": candidate_run.model_dump(mode="json"),
        }
        unsigned = evaluator.post(
            f"/v1/knowledge/internal/drafts/{draft_id}/release-evaluations",
            headers=_headers("unsigned-eval"),
            json=request_body,
        )
        assert unsigned.status_code == 409
        assert unsigned.json()["error"]["code"] == "EVALUATOR_PROVENANCE_UNAVAILABLE"
        tampered_payload = signed.model_dump(mode="json")
        tampered_payload["artifact"]["candidate_run"]["metrics"]["grounded_answer_rate"] = 0.99
        tampered = evaluator.post(
            f"/v1/knowledge/internal/drafts/{draft_id}/signed-release-evaluations",
            headers=_headers("tampered-signed-eval"),
            json=tampered_payload,
        )
        assert tampered.status_code == 409
        assert tampered.json()["error"]["code"] == "EVALUATOR_SIGNATURE_INVALID"
        foreign_service = _client(uuid.uuid4(), evaluator_id, "integration_service", "service")
        foreign_submit = foreign_service.post(
            f"/v1/knowledge/internal/drafts/{draft_id}/signed-release-evaluations",
            headers=_headers("foreign-signed-eval"),
            json=signed.model_dump(mode="json"),
        )
        assert foreign_submit.status_code == 409
        assert foreign_submit.json()["error"]["code"] == "EVAL_TENANT_MISMATCH"
        worker_idempotency_key = f"release-eval:{evaluation_request.json()['event_id']}"
        created = evaluator.post(
            f"/v1/knowledge/internal/drafts/{draft_id}/signed-release-evaluations",
            headers=_headers(worker_idempotency_key),
            json=signed.model_dump(mode="json"),
        )
        assert created.status_code == 200, created.text[:300]
        assert created.json()["replayed"] is True
        assert created.json()["evaluation_id"] == release_eval_id
        assert created.json()["candidate_version_id"] == str(candidate_version_id)
        assert created.json()["status"] == "eligible"
        replay = evaluator.post(
            f"/v1/knowledge/internal/drafts/{draft_id}/signed-release-evaluations",
            headers=_headers(worker_idempotency_key),
            json=signed.model_dump(mode="json"),
        )
        assert replay.status_code == 200, replay.text[:300]
        assert replay.json()["replayed"] is True
        another_artifact = signed.artifact.model_copy(update={"run_id": uuid.uuid4()})
        another_signed = sign_release_evaluation_artifact(
            another_artifact, private_key=signing_secret
        )
        conflicting_replay = evaluator.post(
            f"/v1/knowledge/internal/drafts/{draft_id}/signed-release-evaluations",
            headers=_headers(worker_idempotency_key),
            json=another_signed.model_dump(mode="json"),
        )
        assert conflicting_replay.status_code == 409
        assert conflicting_replay.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"

        publisher_client = _client(tenant_id, publisher_id, "tenant_owner")
        listing = publisher_client.get(
            f"/v1/knowledge/drafts/{draft_id}/release-evaluations",
            headers=_headers(),
        )
        assert listing.status_code == 200, listing.text[:300]
        assert listing.json()["items"][0]["approval_count"] == 0
        assert listing.json()["release_gate_enabled"] is True
        assert listing.json()["release_evidence_available"] is True

        approval_path = (
            f"/v1/knowledge/drafts/{draft_id}/release-evaluations/{release_eval_id}/approve"
        )
        approved_a = _client(tenant_id, reviewer_a, "knowledge_manager").post(
            approval_path,
            headers=_headers("release-approval-a"),
        )
        assert approved_a.status_code == 200, approved_a.text[:300]
        approved_b = _client(tenant_id, reviewer_b, "tenant_owner").post(
            approval_path,
            headers=_headers("release-approval-b"),
        )
        assert approved_b.status_code == 200, approved_b.text[:300]
        author_approval = _client(tenant_id, author_id, "tenant_owner").post(
            approval_path,
            headers=_headers("release-approval-author"),
        )
        assert author_approval.status_code == 409
        assert author_approval.json()["error"]["code"] == "AUTHOR_CANNOT_APPROVE"

        publish_body = {
            "space_id": str(space_id),
            "version_label": "draft-candidate-v1",
            "release_evaluation_id": release_eval_id,
        }
        with admin.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO knowledge_acls "
                    "(id, tenant_id, resource_type, resource_id, principal_type, "
                    "principal_id, permission) VALUES "
                    "(:id, :tenant, 'space', :space, 'role', 'knowledge_manager', 'read')"
                ),
                {"id": uuid.uuid4(), "tenant": tenant_id, "space": space_id},
            )
        stale_publish = publisher_client.post(
            f"/v1/knowledge/drafts/{draft_id}/publish",
            headers=_headers("publish-stale-acl"),
            json=publish_body,
        )
        assert stale_publish.status_code == 409
        assert stale_publish.json()["error"]["code"] == "EVAL_KNOWLEDGE_SNAPSHOT_MOVED"
        with admin.begin() as conn:
            conn.execute(
                text("DELETE FROM knowledge_acls WHERE tenant_id = :tenant"),
                {"tenant": tenant_id},
            )
        published = publisher_client.post(
            f"/v1/knowledge/drafts/{draft_id}/publish",
            headers=_headers("publish-release-1"),
            json=publish_body,
        )
        assert published.status_code == 200, published.text[:300]
        assert published.json()["document_version_id"] == str(candidate_version_id)
        assert published.json()["status"] == "active"
        published_replay = publisher_client.post(
            f"/v1/knowledge/drafts/{draft_id}/publish",
            headers=_headers("publish-release-1"),
            json=publish_body,
        )
        assert published_replay.status_code == 200, published_replay.text[:300]
        assert published_replay.json()["document_version_id"] == str(candidate_version_id)
        post_event_id = uuid.uuid5(tenant_id, f"knowledge-release-post-test:{release_eval_id}")
        with admin.connect() as conn:
            post_event = conn.execute(
                text("SELECT event_type, payload, status FROM outbox_events WHERE event_id = :id"),
                {"id": post_event_id},
            ).one()
        assert post_event.event_type == "knowledge.release.post_test_requested"
        assert post_event.status == "queued"
        assert set(post_event.payload) == {
            "schema_version",
            "evaluation_id",
            "dataset_sha256",
            "dataset_approval_ref",
            "request_hash",
        }

        async def _published_snapshot(at_time: int) -> str:
            ctx = TenantContext(tenant_id, evaluator_id, "service", "integration_service")
            async with tenant_repeatable_read_session(ctx) as session:
                return await _space_snapshot_sha256(
                    session,
                    tenant_id=tenant_id,
                    knowledge_space_id=space_id,
                    candidate_version_id=None,
                    now_ts=at_time,
                )

        pre_test_epoch = int(candidate_run.evaluated_at.timestamp())
        post_epoch = int(datetime.now(UTC).timestamp())
        while post_epoch <= pre_test_epoch:
            time.sleep(0.05)
            post_epoch = int(datetime.now(UTC).timestamp())
        post_time = datetime.fromtimestamp(post_epoch, tz=UTC)
        post_snapshot = asyncio.run(_published_snapshot(post_epoch))
        assert post_snapshot == candidate_snapshot

        async def _run_published_post_test():
            ctx = TenantContext(tenant_id, evaluator_id, "service", "integration_service")
            async with tenant_repeatable_read_session(ctx) as session:
                return await run_published_release_post_test(
                    session,
                    tenant_id=tenant_id,
                    knowledge_space_id=space_id,
                    candidate_version_id=candidate_version_id,
                    pre_publish_run=candidate_pair.candidate_run,
                    cases=(retrieval_case,),
                    approved_dataset_sha256=retrieval_dataset_sha256,
                    principal_scopes=retrieval_scopes,
                    answer_fn=answer_from_first_source,
                    embedder=DeterministicEmbedder(),
                    embedding_model_id="deterministic-test-embedding",
                    embedding_dimensions=1536,
                    embedding_provider_endpoint="test://local",
                    answerer_version="fake-evaluator-answer-v1",
                    answerer_config_sha256="f" * 64,
                    evaluator_version="candidate-runner-integration-v1",
                    commit_sha="d" * 40,
                    key_of=release_document_key,
                    retrieval_config=retrieval_config,
                    now_ts=post_epoch,
                )

        actual_post_test = asyncio.run(_run_published_post_test())
        assert actual_post_test.post_run.metrics.case_count == 1
        assert actual_post_test.post_run.metrics.citation_support_rate == 1.0
        assert actual_post_test.post_run.metrics.unsafe_answer_count == 0
        assert actual_post_test.post_run.knowledge_snapshot_sha256 == candidate_snapshot
        assert (
            actual_post_test.post_run.retrieval_config_sha256
            == candidate_pair.candidate_run.retrieval_config_sha256
        )

        failed_post_run = KnowledgeEvalRun(
            **{
                **candidate_run.model_dump(),
                "evaluated_at": post_time,
                "knowledge_snapshot_sha256": post_snapshot,
                "metrics": {
                    "case_count": candidate_run.metrics.case_count,
                    "grounded_answer_rate": 0.94,
                    "citation_support_rate": 0.95,
                    "retrieval_recall_at_k": 0.88,
                    "unsafe_answer_count": 1,
                },
            }
        )
        unsigned_post_test = evaluator.post(
            f"/v1/knowledge/internal/releases/{release_eval_id}/post-test",
            headers=_headers("release-post-test-1"),
            json={"run": failed_post_run.model_dump(mode="json")},
        )
        assert unsigned_post_test.status_code == 409
        assert unsigned_post_test.json()["error"]["code"] == "EVALUATOR_PROVENANCE_UNAVAILABLE"
        assert asyncio.run(worker.run_once()) == 1
        assert worker_answer_calls == 3
        with admin.connect() as conn:
            post_event_receipt = conn.execute(
                text(
                    "SELECT status, attempts, processing_token, last_error FROM outbox_events "
                    "WHERE event_id = :id"
                ),
                {"id": post_event_id},
            ).one()
            post_test_row = conn.execute(
                text(
                    "SELECT id::text, status, reason_code, attestation_json "
                    "FROM knowledge_release_post_tests "
                    "WHERE tenant_id = :tenant AND evaluation_id = :evaluation"
                ),
                {"tenant": tenant_id, "evaluation": release_eval_id},
            ).one()
        assert post_event_receipt.status == "sent"
        assert post_event_receipt.attempts == 1
        assert post_event_receipt.processing_token is None
        assert post_event_receipt.last_error is None
        assert post_test_row.status == "blocked"
        assert post_test_row.reason_code == "POST_TEST_UNSAFE_ANSWER"
        signed_post = SignedReleasePostTestArtifact.model_validate(post_test_row.attestation_json)
        worker_post_idempotency_key = f"release-post-test:{post_event_id}"
        post_test = evaluator.post(
            f"/v1/knowledge/internal/releases/{release_eval_id}/signed-post-test",
            headers=_headers(worker_post_idempotency_key),
            json=signed_post.model_dump(mode="json"),
        )
        assert post_test.status_code == 200, post_test.text[:300]
        assert post_test.json()["status"] == "blocked"
        post_replay = evaluator.post(
            f"/v1/knowledge/internal/releases/{release_eval_id}/signed-post-test",
            headers=_headers(worker_post_idempotency_key),
            json=signed_post.model_dump(mode="json"),
        )
        assert post_replay.status_code == 200
        assert post_replay.json()["replayed"] is True
        release_listing = publisher_client.get(
            f"/v1/knowledge/drafts/{draft_id}/release-evaluations",
            headers=_headers(),
        )
        assert release_listing.status_code == 200, release_listing.text[:300]
        assert release_listing.json()["items"][0]["approval_count"] == 2
        assert release_listing.json()["items"][0]["post_test_status"] == "blocked"

        with admin.begin() as conn:
            conn.execute(
                text(
                    "UPDATE feature_flags SET enabled = false "
                    "WHERE tenant_id = :tenant AND key = 'knowledge.release_eval_gate'"
                ),
                {"tenant": tenant_id},
            )

        rollback = publisher_client.post(
            f"/v1/knowledge/releases/{release_eval_id}/rollback",
            headers=_headers("release-rollback-1"),
            json={"reason_code": "post_test_failed"},
        )
        assert rollback.status_code == 200, rollback.text[:300]
        assert rollback.json()["superseded_version_id"] == str(candidate_version_id)
        with admin.connect() as conn:
            statuses = conn.execute(
                text(
                    "SELECT id::text, status FROM document_versions "
                    "WHERE tenant_id = :tenant AND id IN (:baseline, :candidate)"
                ),
                {
                    "tenant": tenant_id,
                    "baseline": baseline_version_id,
                    "candidate": candidate_version_id,
                },
            ).all()
        status_by_id = {row[0]: row[1] for row in statuses}
        assert status_by_id[str(candidate_version_id)] == "superseded"
        assert status_by_id[str(baseline_version_id)] == "active"
    finally:
        get_settings.cache_clear()
        with admin.begin() as conn:
            conn.execute(text("DELETE FROM outbox_events WHERE tenant_id = :t"), {"t": tenant_id})
            conn.execute(text("DELETE FROM audit_events WHERE tenant_id = :t"), {"t": tenant_id})
            conn.execute(text("DELETE FROM knowledge_acls WHERE tenant_id = :t"), {"t": tenant_id})
            for table in (
                "knowledge_release_events",
                "knowledge_release_post_tests",
                "knowledge_release_approvals",
                "knowledge_release_evaluations",
            ):
                # The table name comes from this fixed test-local allowlist.
                conn.execute(
                    text(f"DELETE FROM {table} WHERE tenant_id = :t"),  # noqa: S608
                    {"t": tenant_id},
                )
            conn.execute(
                text(
                    "UPDATE knowledge_drafts SET published_document_id = NULL WHERE tenant_id = :t"
                ),
                {"t": tenant_id},
            )
            conn.execute(text("DELETE FROM chunks WHERE tenant_id = :t"), {"t": tenant_id})
            conn.execute(
                text("DELETE FROM knowledge_drafts WHERE tenant_id = :t"), {"t": tenant_id}
            )
            conn.execute(
                text("DELETE FROM document_versions WHERE tenant_id = :t"), {"t": tenant_id}
            )
            conn.execute(text("DELETE FROM documents WHERE tenant_id = :t"), {"t": tenant_id})
            conn.execute(text("DELETE FROM knowledge_gaps WHERE tenant_id = :t"), {"t": tenant_id})
            conn.execute(
                text("DELETE FROM knowledge_sources WHERE tenant_id = :t"), {"t": tenant_id}
            )
            conn.execute(text("DELETE FROM feature_flags WHERE tenant_id = :t"), {"t": tenant_id})
            conn.execute(
                text("DELETE FROM knowledge_spaces WHERE tenant_id = :t"), {"t": tenant_id}
            )
            conn.execute(text("DELETE FROM tenants WHERE id = :t"), {"t": tenant_id})
        admin.dispose()
