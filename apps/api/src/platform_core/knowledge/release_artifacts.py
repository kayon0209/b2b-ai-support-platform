"""Append-only persistence of independently signed release evaluations."""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from platform_contracts.knowledge_release import (
    evaluate_knowledge_candidate,
    evaluate_knowledge_post_test,
)
from platform_contracts.release_attestation import (
    SignedReleaseEvaluationArtifact,
    SignedReleasePostTestArtifact,
)
from platform_core.audit import service as audit_service
from platform_core.identity.tenant_context import TenantContext
from platform_core.knowledge import flag_service, release_service
from platform_core.knowledge.gap_models import DraftStatus, KnowledgeGap
from platform_core.knowledge.release_attestation_service import (
    ReleaseAttestationError,
    verify_persisted_release_evaluation,
)
from platform_core.knowledge.release_evaluator import (
    ReleaseEvaluationError,
    _load_baseline_version,
    _load_candidate_version,
    _require_tenant_repeatable_read,
    _space_snapshot_sha256,
)
from platform_core.knowledge.release_models import (
    KnowledgeReleaseEvaluation,
    KnowledgeReleaseEvent,
    KnowledgeReleasePostTest,
)
from platform_core.knowledge.release_signatures import (
    ReleaseSignatureError,
    configured_approved_release_datasets,
    configured_evaluator_public_keys,
    verify_release_evaluation_artifact,
    verify_release_post_test_artifact,
)


async def persist_signed_release_evaluation(
    session: AsyncSession,
    *,
    ctx: TenantContext,
    signed: SignedReleaseEvaluationArtifact,
    idempotency_key: str,
) -> tuple[KnowledgeReleaseEvaluation, bool]:
    """Accept only a signed worker result from an approved tenant fixed set.

    The caller owns this repeatable-read tenant transaction. No HTTP route
    accepts unsigned metrics or a caller-selected dataset approval.
    """
    release_service._require_evaluator(ctx)
    release_service._require_idempotency_key(idempotency_key)
    try:
        artifact = verify_release_evaluation_artifact(
            signed,
            trusted_public_keys=configured_evaluator_public_keys(),
            max_age_seconds=3600,
        )
        approval = configured_approved_release_datasets().get(
            (artifact.tenant_id, artifact.knowledge_space_id)
        )
    except ReleaseSignatureError as exc:
        raise release_service.KnowledgeReleaseError(exc.code) from exc
    if artifact.tenant_id != ctx.tenant_id:
        raise release_service.KnowledgeReleaseError("EVAL_TENANT_MISMATCH")
    if approval is None:
        raise release_service.KnowledgeReleaseError("EVAL_DATASET_NOT_APPROVED")
    if (
        artifact.dataset_sha256 != approval.sha256
        or artifact.dataset_approval_ref != approval.approval_ref
    ):
        raise release_service.KnowledgeReleaseError("EVAL_DATASET_NOT_APPROVED")

    try:
        await _require_tenant_repeatable_read(session, ctx.tenant_id)
    except ReleaseEvaluationError as exc:
        raise release_service.KnowledgeReleaseError(exc.code) from exc
    enabled = await flag_service.evaluate(
        session,
        flag_key=release_service.FLAG_KNOWLEDGE_RELEASE_GATE,
        tenant_id=ctx.tenant_id,
        default=False,
    )
    if not enabled.enabled:
        raise release_service.KnowledgeReleaseError("FEATURE_DISABLED")

    draft = await release_service._load_draft(
        session, tenant_id=ctx.tenant_id, draft_id=artifact.draft_id
    )
    if draft.status != DraftStatus.APPROVED.value or draft.author_id is None:
        raise release_service.KnowledgeReleaseError("DRAFT_NOT_APPROVED")
    if release_service.draft_content_sha256(draft) != (
        artifact.candidate_run.knowledge_content_sha256
    ):
        raise release_service.KnowledgeReleaseError("DRAFT_CONTENT_MOVED")
    target_space = (
        await session.execute(
            select(KnowledgeGap.target_space_id).where(
                KnowledgeGap.tenant_id == ctx.tenant_id,
                KnowledgeGap.id == draft.gap_id,
            )
        )
    ).scalar_one_or_none()
    if target_space is not None and target_space != artifact.knowledge_space_id:
        raise release_service.KnowledgeReleaseError("DRAFT_SPACE_MISMATCH")
    expected_candidate_id = uuid.uuid5(
        ctx.tenant_id,
        f"knowledge-release-candidate:{draft.id}:{artifact.knowledge_space_id}:"
        f"{artifact.candidate_run.knowledge_content_sha256}",
    )
    if artifact.candidate_version_id != expected_candidate_id:
        raise release_service.KnowledgeReleaseError("CANDIDATE_VERSION_MISMATCH")

    try:
        baseline_version = await _load_baseline_version(
            session,
            tenant_id=ctx.tenant_id,
            knowledge_space_id=artifact.knowledge_space_id,
            version_id=artifact.baseline_run.document_version_id,
        )
        candidate_version = await _load_candidate_version(
            session,
            tenant_id=ctx.tenant_id,
            knowledge_space_id=artifact.knowledge_space_id,
            version_id=expected_candidate_id,
        )
        baseline_snapshot = await _space_snapshot_sha256(
            session,
            tenant_id=ctx.tenant_id,
            knowledge_space_id=artifact.knowledge_space_id,
            candidate_version_id=None,
            now_ts=artifact.retrieval_at,
        )
        candidate_snapshot = await _space_snapshot_sha256(
            session,
            tenant_id=ctx.tenant_id,
            knowledge_space_id=artifact.knowledge_space_id,
            candidate_version_id=expected_candidate_id,
            now_ts=artifact.retrieval_at,
        )
    except ReleaseEvaluationError as exc:
        raise release_service.KnowledgeReleaseError(exc.code) from exc
    if (
        release_service.document_content_sha256(baseline_version.content_hash)
        != artifact.baseline_run.knowledge_content_sha256
        or release_service.document_content_sha256(candidate_version.content_hash)
        != artifact.candidate_run.knowledge_content_sha256
        or baseline_snapshot != artifact.baseline_run.knowledge_snapshot_sha256
        or candidate_snapshot != artifact.candidate_run.knowledge_snapshot_sha256
    ):
        raise release_service.KnowledgeReleaseError("EVAL_KNOWLEDGE_SNAPSHOT_MOVED")

    baseline_json = artifact.baseline_run.model_dump(mode="json")
    candidate_json = artifact.candidate_run.model_dump(mode="json")
    fingerprint = artifact.candidate_run.fingerprint()
    signed_json = signed.model_dump(mode="json")
    attestation_hash = hashlib.sha256(
        json.dumps(signed_json, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    existing = (
        await session.execute(
            select(KnowledgeReleaseEvaluation).where(
                KnowledgeReleaseEvaluation.tenant_id == ctx.tenant_id,
                KnowledgeReleaseEvaluation.idempotency_key == idempotency_key,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        if (
            existing.draft_id == artifact.draft_id
            and existing.knowledge_space_id == artifact.knowledge_space_id
            and existing.candidate_fingerprint == fingerprint
            and existing.baseline_run == baseline_json
            and existing.attestation_sha256 == attestation_hash
            and existing.attestation_json == signed_json
        ):
            return existing, True
        raise release_service.KnowledgeReleaseError("IDEMPOTENCY_CONFLICT")

    decision = evaluate_knowledge_candidate(artifact.baseline_run, artifact.candidate_run)
    values: dict[str, Any] = {
        "id": uuid.uuid4(),
        "tenant_id": ctx.tenant_id,
        "draft_id": artifact.draft_id,
        "knowledge_space_id": artifact.knowledge_space_id,
        "baseline_version_id": artifact.baseline_run.document_version_id,
        "candidate_version_id": expected_candidate_id,
        "author_id": draft.author_id,
        "baseline_run": baseline_json,
        "candidate_run": candidate_json,
        "attestation_json": signed_json,
        "attestation_sha256": attestation_hash,
        "candidate_fingerprint": fingerprint,
        "status": decision.status,
        "reason_code": decision.reason_code,
        "idempotency_key": idempotency_key,
        "created_by": ctx.actor_id,
        "created_at": int(time.time()),
    }
    inserted_id = (
        await session.execute(
            pg_insert(KnowledgeReleaseEvaluation)
            .values(**values)
            .on_conflict_do_nothing()
            .returning(KnowledgeReleaseEvaluation.id)
        )
    ).scalar_one_or_none()
    if inserted_id is None:
        prior = (
            await session.execute(
                select(KnowledgeReleaseEvaluation).where(
                    KnowledgeReleaseEvaluation.tenant_id == ctx.tenant_id,
                    KnowledgeReleaseEvaluation.attestation_sha256 == attestation_hash,
                )
            )
        ).scalar_one_or_none()
        if prior is not None and prior.attestation_json == signed_json:
            return prior, True
        raise release_service.KnowledgeReleaseError("IDEMPOTENCY_CONFLICT")

    row = await release_service._load_evaluation(
        session, tenant_id=ctx.tenant_id, evaluation_id=inserted_id
    )
    await release_service._record_event(
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
        metadata={
            "evaluation_id": str(row.id),
            "candidate_fingerprint": fingerprint,
            "attestation_sha256": attestation_hash,
            "dataset_approval_ref": artifact.dataset_approval_ref,
        },
    )
    return row, False


async def persist_signed_release_post_test(
    session: AsyncSession,
    *,
    ctx: TenantContext,
    evaluation_id: uuid.UUID,
    signed: SignedReleasePostTestArtifact,
    idempotency_key: str,
) -> tuple[KnowledgeReleasePostTest, bool]:
    """Record only signed measurements of the already active release version."""
    release_service._require_evaluator(ctx)
    release_service._require_idempotency_key(idempotency_key)
    try:
        artifact = verify_release_post_test_artifact(
            signed,
            trusted_public_keys=configured_evaluator_public_keys(),
            max_age_seconds=3600,
        )
        approval = configured_approved_release_datasets().get(
            (artifact.tenant_id, artifact.knowledge_space_id)
        )
    except ReleaseSignatureError as exc:
        raise release_service.KnowledgeReleaseError(exc.code) from exc
    if artifact.tenant_id != ctx.tenant_id or artifact.evaluation_id != evaluation_id:
        raise release_service.KnowledgeReleaseError("POST_TEST_INPUT_MISMATCH")
    if (
        approval is None
        or artifact.dataset_sha256 != approval.sha256
        or artifact.dataset_approval_ref != approval.approval_ref
    ):
        raise release_service.KnowledgeReleaseError("EVAL_DATASET_NOT_APPROVED")
    try:
        await _require_tenant_repeatable_read(session, ctx.tenant_id)
    except ReleaseEvaluationError as exc:
        raise release_service.KnowledgeReleaseError(exc.code) from exc
    evaluation = await release_service._load_evaluation(
        session, tenant_id=ctx.tenant_id, evaluation_id=evaluation_id
    )
    try:
        pre_artifact = verify_persisted_release_evaluation(evaluation, tenant_id=ctx.tenant_id)
    except ReleaseAttestationError as exc:
        raise release_service.KnowledgeReleaseError(exc.code) from exc
    if (
        artifact.knowledge_space_id != evaluation.knowledge_space_id
        or artifact.candidate_version_id != evaluation.candidate_version_id
        or artifact.candidate_fingerprint != evaluation.candidate_fingerprint
        or artifact.dataset_sha256 != pre_artifact.dataset_sha256
    ):
        raise release_service.KnowledgeReleaseError("POST_TEST_INPUT_MISMATCH")
    activated = (
        await session.execute(
            select(KnowledgeReleaseEvent.id).where(
                KnowledgeReleaseEvent.tenant_id == ctx.tenant_id,
                KnowledgeReleaseEvent.evaluation_id == evaluation_id,
                KnowledgeReleaseEvent.action == "activated",
            )
        )
    ).scalar_one_or_none()
    if activated is None:
        raise release_service.KnowledgeReleaseError("POST_TEST_RELEASE_NOT_ACTIVE")
    version = await release_service._load_space_version(
        session,
        tenant_id=ctx.tenant_id,
        space_id=evaluation.knowledge_space_id,
        version_id=evaluation.candidate_version_id,
        require_active=True,
    )
    if (
        release_service.document_content_sha256(version.content_hash)
        != artifact.post_run.knowledge_content_sha256
    ):
        raise release_service.KnowledgeReleaseError("POST_TEST_CONTENT_MISMATCH")
    try:
        current_snapshot = await _space_snapshot_sha256(
            session,
            tenant_id=ctx.tenant_id,
            knowledge_space_id=evaluation.knowledge_space_id,
            candidate_version_id=None,
            now_ts=artifact.retrieval_at,
        )
    except ReleaseEvaluationError as exc:
        raise release_service.KnowledgeReleaseError(exc.code) from exc
    if current_snapshot != artifact.post_run.knowledge_snapshot_sha256:
        raise release_service.KnowledgeReleaseError("POST_TEST_SNAPSHOT_MOVED")

    post_run_json = artifact.post_run.model_dump(mode="json")
    signed_json = signed.model_dump(mode="json")
    attestation_hash = hashlib.sha256(
        json.dumps(signed_json, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    existing = (
        await session.execute(
            select(KnowledgeReleasePostTest).where(
                KnowledgeReleasePostTest.tenant_id == ctx.tenant_id,
                KnowledgeReleasePostTest.idempotency_key == idempotency_key,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        if (
            existing.evaluation_id == evaluation_id
            and existing.run == post_run_json
            and existing.attestation_sha256 == attestation_hash
            and existing.attestation_json == signed_json
        ):
            return existing, True
        raise release_service.KnowledgeReleaseError("IDEMPOTENCY_CONFLICT")
    decision = evaluate_knowledge_post_test(pre_artifact.candidate_run, artifact.post_run)
    values: dict[str, Any] = {
        "id": uuid.uuid4(),
        "tenant_id": ctx.tenant_id,
        "evaluation_id": evaluation_id,
        "run": post_run_json,
        "attestation_json": signed_json,
        "attestation_sha256": attestation_hash,
        "status": "passed" if decision.status == "eligible" else "blocked",
        "reason_code": decision.reason_code,
        "idempotency_key": idempotency_key,
        "created_by": ctx.actor_id,
        "created_at": int(time.time()),
    }
    inserted_id = (
        await session.execute(
            pg_insert(KnowledgeReleasePostTest)
            .values(**values)
            .on_conflict_do_nothing()
            .returning(KnowledgeReleasePostTest.id)
        )
    ).scalar_one_or_none()
    if inserted_id is None:
        prior = (
            await session.execute(
                select(KnowledgeReleasePostTest).where(
                    KnowledgeReleasePostTest.tenant_id == ctx.tenant_id,
                    KnowledgeReleasePostTest.attestation_sha256 == attestation_hash,
                )
            )
        ).scalar_one_or_none()
        if prior is not None and prior.attestation_json == signed_json:
            return prior, True
        raise release_service.KnowledgeReleaseError("IDEMPOTENCY_CONFLICT")
    row = (
        await session.execute(
            select(KnowledgeReleasePostTest).where(
                KnowledgeReleasePostTest.tenant_id == ctx.tenant_id,
                KnowledgeReleasePostTest.id == inserted_id,
            )
        )
    ).scalar_one()
    action = "post_test_passed" if decision.status == "eligible" else "rollback_required"
    await release_service._record_event(
        session,
        ctx=ctx,
        evaluation_id=evaluation_id,
        action=action,
        reason_code=decision.reason_code,
        from_version_id=evaluation.baseline_version_id,
        to_version_id=evaluation.candidate_version_id,
        idempotency_key=f"knowledge-post-test-event:{row.id}",
    )
    await audit_service.record(
        session,
        ctx=ctx,
        action="knowledge.release_post_tested",
        resource_type="knowledge_release_evaluation",
        resource_id=evaluation_id,
        decision="completed" if decision.status == "eligible" else "denied",
        reason_code=decision.reason_code,
        metadata={
            "post_test_id": str(row.id),
            "attestation_sha256": attestation_hash,
        },
    )
    return row, False


__all__ = ["persist_signed_release_evaluation", "persist_signed_release_post_test"]
