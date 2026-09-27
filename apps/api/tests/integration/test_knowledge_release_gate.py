"""R2-03 publication gate, trusted evaluation, approval, post-test and rollback."""

from __future__ import annotations

import hashlib
import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from platform_contracts.knowledge_release import KnowledgeEvalMetrics, KnowledgeEvalRun
from platform_core.identity.middleware import TenantContextMiddleware
from platform_core.identity.tenant_context import TenantContext

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
        request_body = {
            "knowledge_space_id": str(space_id),
            "baseline_run": baseline_run.model_dump(mode="json"),
            "candidate_run": candidate_run.model_dump(mode="json"),
        }
        created = evaluator.post(
            f"/v1/knowledge/internal/drafts/{draft_id}/release-evaluations",
            headers=_headers("release-eval-1"),
            json=request_body,
        )
        assert created.status_code == 200, created.text[:300]
        release_eval_id = created.json()["evaluation_id"]
        candidate_version_id = uuid.UUID(created.json()["candidate_version_id"])
        candidate_run = candidate_run.model_copy(
            update={"document_version_id": candidate_version_id}
        )
        assert created.json()["status"] == "eligible"
        replay = evaluator.post(
            f"/v1/knowledge/internal/drafts/{draft_id}/release-evaluations",
            headers=_headers("release-eval-1"),
            json=request_body,
        )
        assert replay.status_code == 200, replay.text[:300]
        assert replay.json()["replayed"] is True

        publisher_client = _client(tenant_id, publisher_id, "tenant_owner")
        listing = publisher_client.get(
            f"/v1/knowledge/drafts/{draft_id}/release-evaluations",
            headers=_headers(),
        )
        assert listing.status_code == 200, listing.text[:300]
        assert listing.json()["items"][0]["approval_count"] == 0
        assert listing.json()["release_gate_enabled"] is True

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
        published = publisher_client.post(
            f"/v1/knowledge/drafts/{draft_id}/publish",
            headers=_headers("publish-release-1"),
            json=publish_body,
        )
        assert published.status_code == 200, published.text[:300]
        assert published.json()["document_version_id"] == str(candidate_version_id)
        published_replay = publisher_client.post(
            f"/v1/knowledge/drafts/{draft_id}/publish",
            headers=_headers("publish-release-1"),
            json=publish_body,
        )
        assert published_replay.status_code == 200, published_replay.text[:300]
        assert published_replay.json()["document_version_id"] == str(candidate_version_id)

        with admin.begin() as conn:
            conn.execute(
                text(
                    "UPDATE document_versions SET scan_status = 'clean' "
                    "WHERE tenant_id = :tenant AND id = :version"
                ),
                {"tenant": tenant_id, "version": candidate_version_id},
            )
        ready = publisher_client.post(
            f"/v1/knowledge/versions/{candidate_version_id}/ready",
            headers=_headers("release-ready-1"),
            json={},
        )
        assert ready.status_code == 200, ready.text[:300]
        assert ready.json()["status"] == "active"

        failed_post_run = KnowledgeEvalRun(
            **{
                **candidate_run.model_dump(),
                "evaluated_at": now + timedelta(hours=1),
                "metrics": {
                    "case_count": 100,
                    "grounded_answer_rate": 0.94,
                    "citation_support_rate": 0.95,
                    "retrieval_recall_at_k": 0.88,
                    "unsafe_answer_count": 1,
                },
            }
        )
        post_test = evaluator.post(
            f"/v1/knowledge/internal/releases/{release_eval_id}/post-test",
            headers=_headers("release-post-test-1"),
            json={"run": failed_post_run.model_dump(mode="json")},
        )
        assert post_test.status_code == 200, post_test.text[:300]
        assert post_test.json()["status"] == "blocked"
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
        with admin.begin() as conn:
            conn.execute(text("DELETE FROM audit_events WHERE tenant_id = :t"), {"t": tenant_id})
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
