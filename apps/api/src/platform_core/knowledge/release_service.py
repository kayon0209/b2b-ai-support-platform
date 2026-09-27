"""Tenant-scoped persistence and publish/rollback rules for knowledge releases."""

from __future__ import annotations

import hashlib
import time
import uuid
from datetime import UTC, datetime
from typing import Any, Literal, cast

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from platform_contracts.knowledge_release import (
    KnowledgeEvalRun,
    evaluate_knowledge_candidate,
    evaluate_knowledge_post_test,
    evaluate_knowledge_release,
)
from platform_contracts.knowledge_release import KnowledgeReleaseApproval as ApprovalContract
from platform_core.audit import service as audit_service
from platform_core.identity.tenant_context import TenantContext
from platform_core.knowledge.gap_models import DraftStatus, KnowledgeDraft, KnowledgeGap
from platform_core.knowledge.models import Document, DocumentVersion
from platform_core.knowledge.release_models import (
    KnowledgeReleaseApproval,
    KnowledgeReleaseEvaluation,
    KnowledgeReleaseEvent,
    KnowledgeReleasePostTest,
)

FLAG_KNOWLEDGE_RELEASE_GATE = "knowledge.release_eval_gate"


class KnowledgeReleaseError(Exception):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(detail or code)
        self.code = code
        self.detail = detail or code


def draft_content_sha256(draft: KnowledgeDraft) -> str:
    """Match the immutable bytes uploaded by the gap-draft publisher."""
    return hashlib.sha256(draft.body.encode("utf-8")).hexdigest()


async def record_evaluation(
    session: AsyncSession,
    *,
    ctx: TenantContext,
    draft_id: uuid.UUID,
    knowledge_space_id: uuid.UUID,
    baseline_run: KnowledgeEvalRun,
    candidate_run: KnowledgeEvalRun,
    idempotency_key: str,
) -> tuple[KnowledgeReleaseEvaluation, bool]:
    """Store an evaluator-worker result. Human/browser principals are refused.

    The caller is an authenticated internal evaluator. The admin UI cannot
    submit its own metrics as release evidence.
    """
    _require_evaluator(ctx)
    _require_idempotency_key(idempotency_key)
    # Candidate ids are allocated by the platform and stable for a retry. The
    # evaluator cannot choose a UUID that collides with another tenant's
    # DocumentVersion or bind evidence to a caller-selected record.
    idempotency_digest = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()
    candidate_version_id = uuid.uuid5(
        ctx.tenant_id,
        f"knowledge-release-candidate:{draft_id}:{idempotency_digest}",
    )
    candidate_run = candidate_run.model_copy(update={"document_version_id": candidate_version_id})
    candidate_fingerprint = candidate_run.fingerprint()
    baseline_json = baseline_run.model_dump(mode="json")
    candidate_json = candidate_run.model_dump(mode="json")
    existing_key = (
        await session.execute(
            select(KnowledgeReleaseEvaluation).where(
                KnowledgeReleaseEvaluation.tenant_id == ctx.tenant_id,
                KnowledgeReleaseEvaluation.idempotency_key == idempotency_key,
            )
        )
    ).scalar_one_or_none()
    if existing_key is not None:
        if (
            existing_key.draft_id == draft_id
            and existing_key.knowledge_space_id == knowledge_space_id
            and existing_key.candidate_fingerprint == candidate_fingerprint
            and existing_key.baseline_run == baseline_json
        ):
            return existing_key, True
        raise KnowledgeReleaseError("IDEMPOTENCY_CONFLICT")
    if baseline_run.tenant_id != ctx.tenant_id or candidate_run.tenant_id != ctx.tenant_id:
        raise KnowledgeReleaseError("EVAL_TENANT_MISMATCH")
    if baseline_run.knowledge_space_id != knowledge_space_id:
        raise KnowledgeReleaseError("EVAL_SPACE_MISMATCH")
    if candidate_run.knowledge_space_id != knowledge_space_id:
        raise KnowledgeReleaseError("EVAL_SPACE_MISMATCH")

    draft = await _load_draft(session, tenant_id=ctx.tenant_id, draft_id=draft_id)
    if draft.status != DraftStatus.APPROVED.value:
        raise KnowledgeReleaseError("DRAFT_NOT_APPROVED")
    if draft.author_id is None:
        raise KnowledgeReleaseError("DRAFT_AUTHOR_MISSING")
    gap_space_id = (
        await session.execute(
            select(KnowledgeGap.target_space_id).where(
                KnowledgeGap.tenant_id == ctx.tenant_id,
                KnowledgeGap.id == draft.gap_id,
            )
        )
    ).scalar_one_or_none()
    if gap_space_id is not None and gap_space_id != knowledge_space_id:
        raise KnowledgeReleaseError("DRAFT_SPACE_MISMATCH")
    if candidate_run.knowledge_content_sha256 != draft_content_sha256(draft):
        raise KnowledgeReleaseError("DRAFT_CONTENT_MOVED")
    if candidate_run.document_version_id == baseline_run.document_version_id:
        raise KnowledgeReleaseError("CANDIDATE_VERSION_REUSED")
    candidate_exists = (
        await session.execute(
            select(DocumentVersion.id).where(
                DocumentVersion.tenant_id == ctx.tenant_id,
                DocumentVersion.id == candidate_run.document_version_id,
            )
        )
    ).scalar_one_or_none()
    if candidate_exists is not None:
        raise KnowledgeReleaseError("CANDIDATE_VERSION_EXISTS")

    baseline_version = await _load_space_version(
        session,
        tenant_id=ctx.tenant_id,
        space_id=knowledge_space_id,
        version_id=baseline_run.document_version_id,
        require_active=True,
    )
    if baseline_version.content_hash != baseline_run.knowledge_content_sha256:
        raise KnowledgeReleaseError("BASELINE_CONTENT_MOVED")
    decision = evaluate_knowledge_candidate(baseline_run, candidate_run)
    fingerprint = candidate_fingerprint

    values: dict[str, Any] = {
        "id": uuid.uuid4(),
        "tenant_id": ctx.tenant_id,
        "draft_id": draft_id,
        "knowledge_space_id": knowledge_space_id,
        "baseline_version_id": baseline_run.document_version_id,
        "candidate_version_id": candidate_run.document_version_id,
        "author_id": draft.author_id,
        "baseline_run": baseline_json,
        "candidate_run": candidate_json,
        "candidate_fingerprint": fingerprint,
        "status": decision.status,
        "reason_code": decision.reason_code,
        "idempotency_key": idempotency_key,
        "created_by": ctx.actor_id,
        "created_at": int(time.time()),
    }
    statement = (
        pg_insert(KnowledgeReleaseEvaluation)
        .values(**values)
        .on_conflict_do_nothing()
        .returning(KnowledgeReleaseEvaluation.id)
    )
    created_id = (await session.execute(statement)).scalar_one_or_none()
    if created_id is None:
        prior = (
            await session.execute(
                select(KnowledgeReleaseEvaluation).where(
                    KnowledgeReleaseEvaluation.tenant_id == ctx.tenant_id,
                    KnowledgeReleaseEvaluation.draft_id == draft_id,
                    KnowledgeReleaseEvaluation.candidate_fingerprint == fingerprint,
                )
            )
        ).scalar_one_or_none()
        if prior is not None:
            return prior, True
        raise KnowledgeReleaseError("IDEMPOTENCY_CONFLICT")

    row = await _load_evaluation(session, tenant_id=ctx.tenant_id, evaluation_id=created_id)
    await _record_event(
        session,
        ctx=ctx,
        evaluation_id=row.id,
        action="evaluation_created",
        reason_code=row.reason_code,
        from_version_id=row.baseline_version_id,
        to_version_id=row.candidate_version_id,
        idempotency_key=f"release-evaluation-created:{row.id}",
    )
    await audit_service.record(
        session,
        ctx=ctx,
        action="knowledge.release_evaluated",
        resource_type="knowledge_draft",
        resource_id=draft.id,
        decision="completed" if row.status == "eligible" else "denied",
        reason_code=row.reason_code,
        metadata={"evaluation_id": str(row.id), "candidate_fingerprint": fingerprint},
    )
    return row, False


async def approve_evaluation(
    session: AsyncSession,
    *,
    ctx: TenantContext,
    evaluation_id: uuid.UUID,
    idempotency_key: str,
    draft_id: uuid.UUID | None = None,
) -> tuple[KnowledgeReleaseApproval, bool]:
    if ctx.actor_kind != "user" or ctx.actor_id is None:
        raise KnowledgeReleaseError("REVIEWER_REQUIRED")
    if ctx.role not in {"knowledge_manager", "tenant_owner"}:
        raise KnowledgeReleaseError("REVIEWER_ROLE_DENIED")
    _require_idempotency_key(idempotency_key)
    evaluation = await _load_evaluation(
        session, tenant_id=ctx.tenant_id, evaluation_id=evaluation_id
    )
    if draft_id is not None and evaluation.draft_id != draft_id:
        raise KnowledgeReleaseError("EVALUATION_NOT_FOUND")
    if evaluation.status != "eligible":
        raise KnowledgeReleaseError("EVALUATION_BLOCKED", evaluation.reason_code)
    if ctx.actor_id == evaluation.author_id:
        raise KnowledgeReleaseError("AUTHOR_CANNOT_APPROVE")
    if evaluation.candidate_fingerprint != _candidate_run(evaluation).fingerprint():
        raise KnowledgeReleaseError("EVALUATION_FINGERPRINT_MISMATCH")

    existing_key = (
        await session.execute(
            select(KnowledgeReleaseApproval).where(
                KnowledgeReleaseApproval.tenant_id == ctx.tenant_id,
                KnowledgeReleaseApproval.idempotency_key == idempotency_key,
            )
        )
    ).scalar_one_or_none()
    if existing_key is not None:
        if (
            existing_key.evaluation_id == evaluation_id
            and existing_key.reviewer_id == ctx.actor_id
            and existing_key.candidate_fingerprint == evaluation.candidate_fingerprint
        ):
            return existing_key, True
        raise KnowledgeReleaseError("IDEMPOTENCY_CONFLICT")

    values = {
        "id": uuid.uuid4(),
        "tenant_id": ctx.tenant_id,
        "evaluation_id": evaluation_id,
        "reviewer_id": ctx.actor_id,
        "reviewer_role": cast(Literal["knowledge_manager", "tenant_owner"], ctx.role),
        "candidate_fingerprint": evaluation.candidate_fingerprint,
        "idempotency_key": idempotency_key,
        "created_at": int(time.time()),
    }
    statement = (
        pg_insert(KnowledgeReleaseApproval)
        .values(**values)
        .on_conflict_do_nothing()
        .returning(KnowledgeReleaseApproval.id)
    )
    approval_id = (await session.execute(statement)).scalar_one_or_none()
    if approval_id is None:
        prior = (
            await session.execute(
                select(KnowledgeReleaseApproval).where(
                    KnowledgeReleaseApproval.tenant_id == ctx.tenant_id,
                    KnowledgeReleaseApproval.evaluation_id == evaluation_id,
                    KnowledgeReleaseApproval.reviewer_id == ctx.actor_id,
                )
            )
        ).scalar_one_or_none()
        if prior is not None:
            raise KnowledgeReleaseError("ALREADY_REVIEWED")
        raise KnowledgeReleaseError("IDEMPOTENCY_CONFLICT")
    approval = (
        await session.execute(
            select(KnowledgeReleaseApproval).where(
                KnowledgeReleaseApproval.tenant_id == ctx.tenant_id,
                KnowledgeReleaseApproval.id == approval_id,
            )
        )
    ).scalar_one()
    await _record_event(
        session,
        ctx=ctx,
        evaluation_id=evaluation_id,
        action="approval_recorded",
        reason_code="FOUR_EYES_APPROVAL_RECORDED",
        from_version_id=evaluation.baseline_version_id,
        to_version_id=evaluation.candidate_version_id,
        idempotency_key=f"release-approval:{approval.id}",
    )
    await audit_service.record(
        session,
        ctx=ctx,
        action="knowledge.release_approved",
        resource_type="knowledge_release_evaluation",
        resource_id=evaluation_id,
        metadata={
            "candidate_fingerprint": evaluation.candidate_fingerprint,
            "reviewer_role": ctx.role,
        },
    )
    return approval, False


async def list_release_evaluations(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    draft_id: uuid.UUID,
    reviewer_id: uuid.UUID | None = None,
) -> list[dict[str, Any]]:
    rows = (
        await session.execute(
            select(KnowledgeReleaseEvaluation)
            .where(
                KnowledgeReleaseEvaluation.tenant_id == tenant_id,
                KnowledgeReleaseEvaluation.draft_id == draft_id,
            )
            .order_by(KnowledgeReleaseEvaluation.created_at.desc(), KnowledgeReleaseEvaluation.id)
            .limit(25)
        )
    ).scalars()
    result = []
    for row in rows:
        approvals = (
            (
                await session.execute(
                    select(KnowledgeReleaseApproval).where(
                        KnowledgeReleaseApproval.tenant_id == tenant_id,
                        KnowledgeReleaseApproval.evaluation_id == row.id,
                    )
                )
            )
            .scalars()
            .all()
        )
        post_test = (
            await session.execute(
                select(KnowledgeReleasePostTest)
                .where(
                    KnowledgeReleasePostTest.tenant_id == tenant_id,
                    KnowledgeReleasePostTest.evaluation_id == row.id,
                )
                .order_by(KnowledgeReleasePostTest.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        result.append(
            {
                "evaluation_id": str(row.id),
                "candidate_version_id": str(row.candidate_version_id),
                "candidate_fingerprint": row.candidate_fingerprint,
                "status": row.status,
                "reason_code": row.reason_code,
                "approval_count": len(approvals),
                "current_user_approved": reviewer_id is not None
                and any(row.reviewer_id == reviewer_id for row in approvals),
                "post_test_status": post_test.status if post_test is not None else None,
                "created_at": row.created_at,
            }
        )
    return result


async def require_publish_evaluation(
    session: AsyncSession,
    *,
    ctx: TenantContext,
    draft_id: uuid.UUID,
    space_id: uuid.UUID,
    evaluation_id: uuid.UUID,
) -> KnowledgeReleaseEvaluation:
    evaluation = await _load_evaluation(
        session, tenant_id=ctx.tenant_id, evaluation_id=evaluation_id
    )
    draft = await _load_draft(session, tenant_id=ctx.tenant_id, draft_id=draft_id)
    if evaluation.draft_id != draft.id or evaluation.knowledge_space_id != space_id:
        raise KnowledgeReleaseError("EVALUATION_NOT_FOUND")
    if evaluation.author_id != draft.author_id or draft.author_id is None:
        raise KnowledgeReleaseError("DRAFT_AUTHOR_MOVED")
    if draft.status != DraftStatus.APPROVED.value:
        raise KnowledgeReleaseError("DRAFT_NOT_APPROVED")
    if ctx.actor_id is not None and ctx.actor_id == draft.author_id:
        raise KnowledgeReleaseError("AUTHOR_CANNOT_PUBLISH")
    if evaluation.status != "eligible":
        raise KnowledgeReleaseError("EVALUATION_BLOCKED", evaluation.reason_code)
    if draft_content_sha256(draft) != _candidate_run(evaluation).knowledge_content_sha256:
        raise KnowledgeReleaseError("DRAFT_CONTENT_MOVED")
    baseline = _baseline_run(evaluation)
    candidate = _candidate_run(evaluation)
    baseline_version = await _load_space_version(
        session,
        tenant_id=ctx.tenant_id,
        space_id=space_id,
        version_id=evaluation.baseline_version_id,
        require_active=True,
        for_update=True,
    )
    if baseline_version.content_hash != baseline.knowledge_content_sha256:
        raise KnowledgeReleaseError("EVAL_BASELINE_STALE")
    approval_rows = (
        await session.execute(
            select(KnowledgeReleaseApproval).where(
                KnowledgeReleaseApproval.tenant_id == ctx.tenant_id,
                KnowledgeReleaseApproval.evaluation_id == evaluation.id,
            )
        )
    ).scalars()
    approvals = tuple(
        ApprovalContract(
            reviewer_id=row.reviewer_id,
            reviewer_role=cast(Literal["knowledge_manager", "tenant_owner"], row.reviewer_role),
            candidate_fingerprint=row.candidate_fingerprint,
            approved_at=datetime.fromtimestamp(row.created_at, tz=UTC),
        )
        for row in approval_rows
    )
    decision = evaluate_knowledge_release(
        baseline,
        candidate,
        author_id=draft.author_id,
        approvals=approvals,
    )
    if decision.status != "eligible":
        raise KnowledgeReleaseError(decision.reason_code)
    return evaluation


async def record_publish_requested(
    session: AsyncSession,
    *,
    ctx: TenantContext,
    evaluation: KnowledgeReleaseEvaluation,
    idempotency_key: str,
) -> None:
    await _record_event(
        session,
        ctx=ctx,
        evaluation_id=evaluation.id,
        action="publish_requested",
        reason_code="KNOWLEDGE_RELEASE_APPROVED",
        from_version_id=evaluation.baseline_version_id,
        to_version_id=evaluation.candidate_version_id,
        idempotency_key=(
            "knowledge-publish:" + hashlib.sha256(idempotency_key.encode()).hexdigest()
        ),
    )


async def find_published_release_version(
    session: AsyncSession,
    *,
    ctx: TenantContext,
    draft: KnowledgeDraft,
    evaluation_id: uuid.UUID,
) -> DocumentVersion | None:
    """Return the same version for a retry after a committed publish response was lost."""
    evaluation = await _load_evaluation(
        session, tenant_id=ctx.tenant_id, evaluation_id=evaluation_id
    )
    if evaluation.draft_id != draft.id or draft.published_document_id is None:
        return None
    return (
        await session.execute(
            select(DocumentVersion).where(
                DocumentVersion.tenant_id == ctx.tenant_id,
                DocumentVersion.id == evaluation.candidate_version_id,
                DocumentVersion.document_id == draft.published_document_id,
            )
        )
    ).scalar_one_or_none()


async def record_activation(
    session: AsyncSession,
    *,
    ctx: TenantContext,
    version_id: uuid.UUID,
) -> bool:
    evaluation = (
        await session.execute(
            select(KnowledgeReleaseEvaluation).where(
                KnowledgeReleaseEvaluation.tenant_id == ctx.tenant_id,
                KnowledgeReleaseEvaluation.candidate_version_id == version_id,
            )
        )
    ).scalar_one_or_none()
    if evaluation is None:
        return False
    await _record_event(
        session,
        ctx=ctx,
        evaluation_id=evaluation.id,
        action="activated",
        reason_code="KNOWLEDGE_VERSION_ACTIVE",
        from_version_id=evaluation.baseline_version_id,
        to_version_id=version_id,
        idempotency_key=f"knowledge-release-activated:{evaluation.id}",
    )
    await audit_service.record(
        session,
        ctx=ctx,
        action="knowledge.release_activated",
        resource_type="knowledge_release_evaluation",
        resource_id=evaluation.id,
        metadata={"candidate_version_id": str(version_id)},
    )
    return True


async def record_post_test(
    session: AsyncSession,
    *,
    ctx: TenantContext,
    evaluation_id: uuid.UUID,
    post_run: KnowledgeEvalRun,
    idempotency_key: str,
) -> tuple[KnowledgeReleasePostTest, bool]:
    _require_evaluator(ctx)
    _require_idempotency_key(idempotency_key)
    post_run_json = post_run.model_dump(mode="json")
    existing = (
        await session.execute(
            select(KnowledgeReleasePostTest).where(
                KnowledgeReleasePostTest.tenant_id == ctx.tenant_id,
                KnowledgeReleasePostTest.idempotency_key == idempotency_key,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        if existing.evaluation_id == evaluation_id and existing.run == post_run_json:
            return existing, True
        raise KnowledgeReleaseError("IDEMPOTENCY_CONFLICT")
    evaluation = await _load_evaluation(
        session, tenant_id=ctx.tenant_id, evaluation_id=evaluation_id
    )
    post_version = await _load_space_version(
        session,
        tenant_id=ctx.tenant_id,
        space_id=evaluation.knowledge_space_id,
        version_id=post_run.document_version_id,
        require_active=True,
    )
    if post_version.content_hash != post_run.knowledge_content_sha256:
        raise KnowledgeReleaseError("POST_TEST_CONTENT_MISMATCH")
    decision = evaluate_knowledge_post_test(_candidate_run(evaluation), post_run)
    values = {
        "id": uuid.uuid4(),
        "tenant_id": ctx.tenant_id,
        "evaluation_id": evaluation_id,
        "run": post_run_json,
        "status": "passed" if decision.status == "eligible" else "blocked",
        "reason_code": decision.reason_code,
        "idempotency_key": idempotency_key,
        "created_by": ctx.actor_id,
        "created_at": int(time.time()),
    }
    post_test_id = (
        await session.execute(
            pg_insert(KnowledgeReleasePostTest)
            .values(**values)
            .on_conflict_do_nothing()
            .returning(KnowledgeReleasePostTest.id)
        )
    ).scalar_one_or_none()
    if post_test_id is None:
        raise KnowledgeReleaseError("IDEMPOTENCY_CONFLICT")
    post_test = (
        await session.execute(
            select(KnowledgeReleasePostTest).where(
                KnowledgeReleasePostTest.tenant_id == ctx.tenant_id,
                KnowledgeReleasePostTest.id == post_test_id,
            )
        )
    ).scalar_one()
    action = "post_test_passed" if decision.status == "eligible" else "rollback_required"
    await _record_event(
        session,
        ctx=ctx,
        evaluation_id=evaluation_id,
        action=action,
        reason_code=decision.reason_code,
        from_version_id=evaluation.baseline_version_id,
        to_version_id=evaluation.candidate_version_id,
        idempotency_key=f"knowledge-post-test-event:{post_test.id}",
    )
    await audit_service.record(
        session,
        ctx=ctx,
        action="knowledge.release_post_tested",
        resource_type="knowledge_release_evaluation",
        resource_id=evaluation_id,
        decision="completed" if decision.status == "eligible" else "denied",
        reason_code=decision.reason_code,
        metadata={"post_test_id": str(post_test.id)},
    )
    return post_test, False


async def rollback_release(
    session: AsyncSession,
    *,
    ctx: TenantContext,
    evaluation_id: uuid.UUID,
    idempotency_key: str,
    reason_code: Literal["post_test_failed", "manual_quality_issue"],
) -> tuple[uuid.UUID, uuid.UUID, bool]:
    if ctx.actor_kind != "user" or ctx.actor_id is None:
        raise KnowledgeReleaseError("PUBLISHER_REQUIRED")
    if ctx.role not in {"knowledge_manager", "tenant_owner"}:
        raise KnowledgeReleaseError("PUBLISHER_ROLE_DENIED")
    _require_idempotency_key(idempotency_key)
    evaluation = await _load_evaluation(
        session, tenant_id=ctx.tenant_id, evaluation_id=evaluation_id
    )
    prior = (
        await session.execute(
            select(KnowledgeReleaseEvent).where(
                KnowledgeReleaseEvent.tenant_id == ctx.tenant_id,
                KnowledgeReleaseEvent.idempotency_key == idempotency_key,
            )
        )
    ).scalar_one_or_none()
    if prior is not None:
        if prior.evaluation_id == evaluation_id and prior.action == "rollback_completed":
            return evaluation.candidate_version_id, evaluation.baseline_version_id, True
        raise KnowledgeReleaseError("IDEMPOTENCY_CONFLICT")
    if reason_code == "post_test_failed":
        latest = (
            await session.execute(
                select(KnowledgeReleasePostTest)
                .where(
                    KnowledgeReleasePostTest.tenant_id == ctx.tenant_id,
                    KnowledgeReleasePostTest.evaluation_id == evaluation_id,
                )
                .order_by(KnowledgeReleasePostTest.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if latest is None or latest.status != "blocked":
            raise KnowledgeReleaseError("ROLLBACK_NOT_REQUIRED")

    candidate = await _load_space_version(
        session,
        tenant_id=ctx.tenant_id,
        space_id=evaluation.knowledge_space_id,
        version_id=evaluation.candidate_version_id,
        require_active=True,
        for_update=True,
    )
    baseline = await _load_space_version(
        session,
        tenant_id=ctx.tenant_id,
        space_id=evaluation.knowledge_space_id,
        version_id=evaluation.baseline_version_id,
        require_active=False,
        for_update=True,
    )
    if (
        baseline.status not in {"active", "superseded"}
        or baseline.ingestion_status != "ready"
        or baseline.scan_status != "clean"
        or baseline.bytes_deleted_at is not None
    ):
        raise KnowledgeReleaseError("ROLLBACK_BASELINE_UNAVAILABLE")
    now = int(time.time())
    candidate.status = "superseded"
    candidate.expires_at = now
    baseline.status = "active"
    baseline.expires_at = None
    await session.flush()
    await _record_event(
        session,
        ctx=ctx,
        evaluation_id=evaluation_id,
        action="rollback_completed",
        reason_code=reason_code,
        from_version_id=candidate.id,
        to_version_id=baseline.id,
        idempotency_key=idempotency_key,
    )
    await audit_service.record(
        session,
        ctx=ctx,
        action="knowledge.release_rolled_back",
        resource_type="knowledge_release_evaluation",
        resource_id=evaluation_id,
        reason_code=reason_code,
        after={
            "superseded_version_id": str(candidate.id),
            "restored_version_id": str(baseline.id),
        },
    )
    return candidate.id, baseline.id, False


async def _record_event(
    session: AsyncSession,
    *,
    ctx: TenantContext,
    evaluation_id: uuid.UUID,
    action: str,
    reason_code: str,
    from_version_id: uuid.UUID | None,
    to_version_id: uuid.UUID | None,
    idempotency_key: str,
) -> None:
    await session.execute(
        pg_insert(KnowledgeReleaseEvent)
        .values(
            id=uuid.uuid4(),
            tenant_id=ctx.tenant_id,
            evaluation_id=evaluation_id,
            action=action,
            actor_id=ctx.actor_id,
            from_version_id=from_version_id,
            to_version_id=to_version_id,
            reason_code=reason_code,
            idempotency_key=idempotency_key,
            created_at=int(time.time()),
        )
        .on_conflict_do_nothing()
    )


async def _load_draft(
    session: AsyncSession, *, tenant_id: uuid.UUID, draft_id: uuid.UUID
) -> KnowledgeDraft:
    row = (
        await session.execute(
            select(KnowledgeDraft).where(
                KnowledgeDraft.tenant_id == tenant_id,
                KnowledgeDraft.id == draft_id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise KnowledgeReleaseError("DRAFT_NOT_FOUND")
    return row


async def _load_evaluation(
    session: AsyncSession, *, tenant_id: uuid.UUID, evaluation_id: uuid.UUID
) -> KnowledgeReleaseEvaluation:
    row = (
        await session.execute(
            select(KnowledgeReleaseEvaluation).where(
                KnowledgeReleaseEvaluation.tenant_id == tenant_id,
                KnowledgeReleaseEvaluation.id == evaluation_id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise KnowledgeReleaseError("EVALUATION_NOT_FOUND")
    return row


async def _load_space_version(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    space_id: uuid.UUID,
    version_id: uuid.UUID,
    require_active: bool,
    for_update: bool = False,
) -> DocumentVersion:
    stmt = (
        select(DocumentVersion)
        .join(Document, Document.id == DocumentVersion.document_id)
        .where(
            DocumentVersion.tenant_id == tenant_id,
            DocumentVersion.id == version_id,
            Document.space_id == space_id,
            Document.tenant_id == tenant_id,
        )
    )
    if require_active:
        stmt = stmt.where(
            DocumentVersion.status == "active",
            DocumentVersion.ingestion_status == "ready",
            DocumentVersion.scan_status == "clean",
        )
    if for_update:
        stmt = stmt.with_for_update()
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise KnowledgeReleaseError("KNOWLEDGE_VERSION_NOT_FOUND")
    return row


def _baseline_run(evaluation: KnowledgeReleaseEvaluation) -> KnowledgeEvalRun:
    return KnowledgeEvalRun.model_validate(evaluation.baseline_run)


def _candidate_run(evaluation: KnowledgeReleaseEvaluation) -> KnowledgeEvalRun:
    return KnowledgeEvalRun.model_validate(evaluation.candidate_run)


def _require_evaluator(ctx: TenantContext) -> None:
    if (
        ctx.actor_kind not in {"system", "service"}
        or ctx.actor_id is None
        or ctx.role != "integration_service"
    ):
        raise KnowledgeReleaseError("EVALUATOR_SERVICE_REQUIRED")


def _require_idempotency_key(value: str) -> None:
    if not value or len(value) > 255:
        raise KnowledgeReleaseError("IDEMPOTENCY_KEY_INVALID")


__all__ = [
    "FLAG_KNOWLEDGE_RELEASE_GATE",
    "KnowledgeReleaseError",
    "approve_evaluation",
    "draft_content_sha256",
    "find_published_release_version",
    "list_release_evaluations",
    "record_activation",
    "record_evaluation",
    "record_post_test",
    "record_publish_requested",
    "require_publish_evaluation",
    "rollback_release",
]
