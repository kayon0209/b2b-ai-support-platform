"""Trusted worker entrypoints for signed knowledge release measurements."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from pydantic import SecretStr
from sqlalchemy import select

from platform_contracts.release_attestation import (
    ReleaseEvaluationArtifact,
    ReleasePostTestArtifact,
    SignedReleasePostTestArtifact,
)
from platform_core.evaluation.runner import AnswerFn
from platform_core.identity.tenant_context import TenantContext, tenant_repeatable_read_session
from platform_core.knowledge import flag_service
from platform_core.knowledge.release_artifacts import (
    persist_signed_release_evaluation,
    persist_signed_release_post_test,
)
from platform_core.knowledge.release_attestation_service import (
    verify_persisted_release_evaluation,
)
from platform_core.knowledge.release_evaluator import (
    ReleaseRetrievalConfig,
    load_approved_release_dataset,
    release_document_key,
    run_candidate_aware_evaluation,
    run_published_release_post_test,
)
from platform_core.knowledge.release_models import (
    KnowledgeReleaseEvaluation,
    KnowledgeReleasePostTest,
)
from platform_core.knowledge.release_service import FLAG_KNOWLEDGE_RELEASE_GATE
from platform_core.knowledge.release_signatures import (
    configured_approved_release_datasets,
    configured_evaluator_public_keys,
    require_signing_key_matches_trust_root,
    sign_release_evaluation_artifact,
    sign_release_post_test_artifact,
    verify_release_post_test_artifact,
)
from platform_core.retrieval.hybrid import Embedder


async def evaluate_and_sign_release_candidate(
    *,
    ctx: TenantContext,
    draft_id: uuid.UUID,
    knowledge_space_id: uuid.UUID,
    baseline_version_id: uuid.UUID,
    candidate_version_id: uuid.UUID,
    idempotency_key: str,
    answer_fn: AnswerFn,
    embedder: Embedder,
    embedding_model_id: str,
    embedding_dimensions: int,
    embedding_provider_endpoint: str,
    answerer_version: str,
    answerer_config_sha256: str,
    evaluator_version: str,
    commit_sha: str,
    key_id: str,
    private_key: SecretStr,
    max_cases_per_run: int,
    retrieval_config: ReleaseRetrievalConfig | None = None,
) -> tuple[uuid.UUID, str]:
    """Evaluate and persist only a fixed, signed tenant release candidate."""
    if (
        ctx.actor_kind not in {"system", "service"}
        or ctx.actor_id is None
        or ctx.role != "integration_service"
    ):
        raise RuntimeError("EVALUATOR_SERVICE_REQUIRED")
    approval = configured_approved_release_datasets().get((ctx.tenant_id, knowledge_space_id))
    if approval is None:
        raise RuntimeError("EVAL_DATASET_NOT_APPROVED")
    trusted_keys = configured_evaluator_public_keys()
    require_signing_key_matches_trust_root(
        key_id=key_id,
        private_key=private_key,
        trusted_public_keys=trusted_keys,
    )
    async with tenant_repeatable_read_session(ctx) as session:
        existing = (
            await session.execute(
                select(KnowledgeReleaseEvaluation).where(
                    KnowledgeReleaseEvaluation.tenant_id == ctx.tenant_id,
                    KnowledgeReleaseEvaluation.idempotency_key == idempotency_key,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            if existing.draft_id != draft_id or existing.knowledge_space_id != knowledge_space_id:
                raise RuntimeError("IDEMPOTENCY_CONFLICT")
            verify_persisted_release_evaluation(existing, tenant_id=ctx.tenant_id)
            return existing.id, existing.status
        enabled = await flag_service.evaluate(
            session,
            flag_key=FLAG_KNOWLEDGE_RELEASE_GATE,
            tenant_id=ctx.tenant_id,
            default=False,
        )
        if not enabled.enabled:
            raise RuntimeError("FEATURE_DISABLED")
        dataset = load_approved_release_dataset(
            tenant_id=ctx.tenant_id,
            knowledge_space_id=knowledge_space_id,
        )
        if len(dataset.cases) > max_cases_per_run:
            raise RuntimeError("EVAL_CASE_BUDGET_EXCEEDED")
        pair = await run_candidate_aware_evaluation(
            session,
            tenant_id=ctx.tenant_id,
            knowledge_space_id=knowledge_space_id,
            baseline_version_id=baseline_version_id,
            candidate_version_id=candidate_version_id,
            cases=dataset.cases,
            approved_dataset_sha256=dataset.approval.sha256,
            principal_scopes=dataset.principal_scopes,
            answer_fn=answer_fn,
            embedder=embedder,
            embedding_model_id=embedding_model_id,
            embedding_dimensions=embedding_dimensions,
            embedding_provider_endpoint=embedding_provider_endpoint,
            answerer_version=answerer_version,
            answerer_config_sha256=answerer_config_sha256,
            evaluator_version=evaluator_version,
            commit_sha=commit_sha,
            key_of=release_document_key,
            retrieval_config=retrieval_config,
        )
        artifact = ReleaseEvaluationArtifact(
            run_id=uuid.uuid4(),
            tenant_id=ctx.tenant_id,
            knowledge_space_id=knowledge_space_id,
            draft_id=draft_id,
            candidate_version_id=candidate_version_id,
            dataset_sha256=dataset.approval.sha256,
            dataset_approval_ref=dataset.approval.approval_ref,
            baseline_run=pair.baseline_run,
            candidate_run=pair.candidate_run,
            baseline_result_sha256=pair.baseline_result_sha256,
            candidate_result_sha256=pair.candidate_result_sha256,
            retrieval_at=pair.retrieval_at,
            issued_at=datetime.now(UTC),
            key_id=key_id,
        )
        signed = sign_release_evaluation_artifact(artifact, private_key=private_key)
        evaluation, _ = await persist_signed_release_evaluation(
            session,
            ctx=ctx,
            signed=signed,
            idempotency_key=idempotency_key,
        )
    return evaluation.id, evaluation.status


async def evaluate_and_sign_published_release(
    *,
    ctx: TenantContext,
    evaluation_id: uuid.UUID,
    idempotency_key: str,
    answer_fn: AnswerFn,
    embedder: Embedder,
    embedding_model_id: str,
    embedding_dimensions: int,
    embedding_provider_endpoint: str,
    answerer_version: str,
    answerer_config_sha256: str,
    evaluator_version: str,
    commit_sha: str,
    key_id: str,
    private_key: SecretStr,
    max_cases_per_run: int,
    retrieval_config: ReleaseRetrievalConfig | None = None,
) -> tuple[uuid.UUID, str]:
    """Run the signed post-test used for rollback-required decisions."""
    if (
        ctx.actor_kind not in {"system", "service"}
        or ctx.actor_id is None
        or ctx.role != "integration_service"
    ):
        raise RuntimeError("EVALUATOR_SERVICE_REQUIRED")
    trusted_keys = configured_evaluator_public_keys()
    require_signing_key_matches_trust_root(
        key_id=key_id,
        private_key=private_key,
        trusted_public_keys=trusted_keys,
    )
    async with tenant_repeatable_read_session(ctx) as session:
        existing_post = (
            await session.execute(
                select(KnowledgeReleasePostTest).where(
                    KnowledgeReleasePostTest.tenant_id == ctx.tenant_id,
                    KnowledgeReleasePostTest.evaluation_id == evaluation_id,
                    KnowledgeReleasePostTest.idempotency_key == idempotency_key,
                )
            )
        ).scalar_one_or_none()
        if existing_post is not None:
            if (
                existing_post.attestation_json is None
                or existing_post.attestation_sha256 is None
                or existing_post.run is None
            ):
                raise RuntimeError("EVALUATOR_PROVENANCE_UNAVAILABLE")
            signed_existing = SignedReleasePostTestArtifact.model_validate(
                existing_post.attestation_json
            )
            verify_release_post_test_artifact(
                signed_existing,
                trusted_public_keys=trusted_keys,
            )
            return existing_post.id, existing_post.status
        pre_evaluation = (
            await session.execute(
                select(KnowledgeReleaseEvaluation).where(
                    KnowledgeReleaseEvaluation.tenant_id == ctx.tenant_id,
                    KnowledgeReleaseEvaluation.id == evaluation_id,
                )
            )
        ).scalar_one_or_none()
        if pre_evaluation is None:
            raise RuntimeError("EVALUATION_NOT_FOUND")
        pre_artifact = verify_persisted_release_evaluation(pre_evaluation, tenant_id=ctx.tenant_id)
        dataset = load_approved_release_dataset(
            tenant_id=ctx.tenant_id,
            knowledge_space_id=pre_evaluation.knowledge_space_id,
        )
        if len(dataset.cases) > max_cases_per_run:
            raise RuntimeError("EVAL_CASE_BUDGET_EXCEEDED")
        result = await run_published_release_post_test(
            session,
            tenant_id=ctx.tenant_id,
            knowledge_space_id=pre_evaluation.knowledge_space_id,
            candidate_version_id=pre_evaluation.candidate_version_id,
            pre_publish_run=pre_artifact.candidate_run,
            cases=dataset.cases,
            approved_dataset_sha256=dataset.approval.sha256,
            principal_scopes=dataset.principal_scopes,
            answer_fn=answer_fn,
            embedder=embedder,
            embedding_model_id=embedding_model_id,
            embedding_dimensions=embedding_dimensions,
            embedding_provider_endpoint=embedding_provider_endpoint,
            answerer_version=answerer_version,
            answerer_config_sha256=answerer_config_sha256,
            evaluator_version=evaluator_version,
            commit_sha=commit_sha,
            key_of=release_document_key,
            retrieval_config=retrieval_config,
        )
        artifact = ReleasePostTestArtifact(
            run_id=uuid.uuid4(),
            evaluation_id=evaluation_id,
            tenant_id=ctx.tenant_id,
            knowledge_space_id=pre_evaluation.knowledge_space_id,
            candidate_version_id=pre_evaluation.candidate_version_id,
            candidate_fingerprint=pre_evaluation.candidate_fingerprint,
            dataset_sha256=dataset.approval.sha256,
            dataset_approval_ref=dataset.approval.approval_ref,
            post_run=result.post_run,
            result_sha256=result.result_sha256,
            retrieval_at=result.retrieval_at,
            issued_at=datetime.now(UTC),
            key_id=key_id,
        )
        signed = sign_release_post_test_artifact(artifact, private_key=private_key)
        post_test, _ = await persist_signed_release_post_test(
            session,
            ctx=ctx,
            evaluation_id=evaluation_id,
            signed=signed,
            idempotency_key=idempotency_key,
        )
    return post_test.id, post_test.status


def build_release_answerer(
    chat_provider: Any,
    *,
    primary_model: str,
    provider_base_url: str,
    timeout_seconds: float,
    max_retries: int,
    fallback_model: str | None = None,
) -> tuple[AnswerFn, str, str]:
    """Build the existing grounded answerer and a hash of its prompt settings."""
    import hashlib
    import json

    from platform_core.agent_runtime.generator import MAX_ANSWER_TOKENS, LlmAnswerGenerator
    from platform_core.agent_runtime.prompts import KNOWLEDGE_QA_PROMPT

    generator = LlmAnswerGenerator(chat_provider, fallback_model=fallback_model)
    config = {
        "template_name": KNOWLEDGE_QA_PROMPT.name,
        "template_version": KNOWLEDGE_QA_PROMPT.version,
        "template_body": KNOWLEDGE_QA_PROMPT.body,
        "primary_model": primary_model,
        "provider_base_url": provider_base_url.rstrip("/"),
        "max_tokens": MAX_ANSWER_TOKENS,
        "temperature": 0.0,
        "timeout_seconds": timeout_seconds,
        "max_retries": max_retries,
        "fallback_model": fallback_model,
    }
    digest = hashlib.sha256(
        json.dumps(config, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return generator.generate, f"{KNOWLEDGE_QA_PROMPT.name}-v{KNOWLEDGE_QA_PROMPT.version}", digest


__all__ = [
    "build_release_answerer",
    "evaluate_and_sign_published_release",
    "evaluate_and_sign_release_candidate",
]
