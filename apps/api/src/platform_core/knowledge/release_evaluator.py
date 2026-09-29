"""Candidate-aware, replayable knowledge release evaluation.

This module is an internal execution seam, not an HTTP endpoint. It requires
an approved dataset digest and a tenant-scoped repeatable-read transaction.
Until a knowledge/security owner supplies an approved fixed dataset and a
trusted worker persists these results, release_service keeps all evidence
write and promotion paths fail-closed.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from platform_contracts.knowledge_release import KnowledgeEvalMetrics, KnowledgeEvalRun
from platform_contracts.knowledge_release_dataset import KnowledgeReleaseDatasetManifest
from platform_core.evaluation.runner import AnswerFn, EvalCase, EvalReport, EvaluationRunner
from platform_core.knowledge.acl import KnowledgeAcl
from platform_core.knowledge.models import Chunk, Document, DocumentVersion, KnowledgeSpace
from platform_core.knowledge.release_service import document_content_sha256
from platform_core.knowledge.release_signatures import (
    ApprovedReleaseDataset,
    ReleaseSignatureError,
    configured_approved_release_datasets,
)
from platform_core.retrieval.hybrid import (
    RETRIEVAL_PATHS,
    Embedder,
    PrincipalScope,
    ReleaseCandidateScope,
    RetrievedChunk,
    hybrid_search,
    load_aliases,
)

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
RELEASE_EVALUATION_REQUEST_EVENT = "knowledge.release.evaluate_requested"
RELEASE_POST_TEST_REQUEST_EVENT = "knowledge.release.post_test_requested"


class ReleaseEvaluationError(Exception):
    """A release evaluation could not produce comparable, trusted evidence."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(detail or code)
        self.code = code
        self.detail = detail or code


@dataclass(frozen=True)
class ReleaseRetrievalConfig:
    """The retrieval knobs shared by the baseline and candidate runs."""

    top_k: int = 8
    enabled_paths: tuple[str, ...] = RETRIEVAL_PATHS
    fts_candidates: int = 40
    vector_candidates: int = 40
    trigram_candidates: int = 40
    alias_candidates: int = 20
    rrf_k: int = 60


@dataclass(frozen=True)
class CandidateEvaluationPair:
    """Summary evidence returned by the internal runner.

    Raw questions, answers, and excerpts remain in memory only. The persisted
    release records need the immutable run contracts and these result digests.
    """

    baseline_run: KnowledgeEvalRun
    candidate_run: KnowledgeEvalRun
    baseline_result_sha256: str
    candidate_result_sha256: str
    retrieval_at: int

    def artifact_sha256(self) -> str:
        return _canonical_sha256(
            {
                "baseline_run": self.baseline_run.fingerprint(),
                "candidate_run": self.candidate_run.fingerprint(),
                "baseline_result_sha256": self.baseline_result_sha256,
                "candidate_result_sha256": self.candidate_result_sha256,
                "retrieval_at": self.retrieval_at,
            }
        )


@dataclass(frozen=True)
class PublishedPostTestResult:
    post_run: KnowledgeEvalRun
    result_sha256: str
    retrieval_at: int


@dataclass(frozen=True)
class LoadedReleaseDataset:
    approval: ApprovedReleaseDataset
    cases: tuple[EvalCase, ...]
    principal_scopes: Mapping[str, PrincipalScope]


def release_dataset_sha256(
    cases: Sequence[EvalCase],
    *,
    tenant_id: uuid.UUID,
    knowledge_space_id: uuid.UUID,
    principal_scopes: Mapping[str, PrincipalScope],
) -> str:
    """Hash every scoring input without logging or persisting the case text."""
    case_ids = [case.case_id for case in cases]
    if not cases or any(not case_id for case_id in case_ids):
        raise ReleaseEvaluationError("EVAL_DATASET_CASE_IDS_REQUIRED")
    if any(re.fullmatch(r"eval-[0-9a-f]{10}", case_id) for case_id in case_ids):
        raise ReleaseEvaluationError("EVAL_DATASET_CASE_IDS_MUST_BE_STABLE")
    if len(case_ids) != len(set(case_ids)):
        raise ReleaseEvaluationError("EVAL_DATASET_CASE_IDS_NOT_UNIQUE")
    if not any(not case.must_abstain for case in cases):
        raise ReleaseEvaluationError("EVAL_DATASET_NEEDS_ANSWERABLE_CASE")
    if not any(case.expected_version_keys for case in cases):
        raise ReleaseEvaluationError("EVAL_DATASET_NEEDS_EXPECTED_SOURCES")
    if set(principal_scopes) != set(case_ids):
        raise ReleaseEvaluationError("EVAL_PRINCIPAL_SCOPE_SET_MISMATCH")
    for scope in principal_scopes.values():
        if (
            not scope.principal_types
            or len(scope.principal_types) != len(scope.principal_ids)
            or set(scope.principal_types) - {"user", "role", "department", "enterprise_account"}
        ):
            raise ReleaseEvaluationError("EVAL_PRINCIPAL_SCOPE_INVALID")
    payload = {
        "tenant_id": str(tenant_id),
        "knowledge_space_id": str(knowledge_space_id),
        "cases": [asdict(case) for case in cases],
        "principal_scopes": {
            case_id: list(zip(scope.principal_types, scope.principal_ids, strict=True))
            for case_id, scope in sorted(principal_scopes.items())
        },
    }
    return _canonical_sha256(payload)


def release_document_key(chunk: RetrievedChunk) -> str:
    """Stable source key for fixed-set expectations across candidate content edits."""
    key = chunk.document_key
    if key.startswith("gap-candidate://"):
        return key.rsplit("/", maxsplit=1)[0]
    return key or chunk.title


def load_approved_release_dataset(
    *,
    tenant_id: uuid.UUID,
    knowledge_space_id: uuid.UUID,
    max_bytes: int = 4 * 1024 * 1024,
) -> LoadedReleaseDataset:
    """Load an immutable approved manifest from the tenant-prefixed object key."""
    try:
        approval = configured_approved_release_datasets().get((tenant_id, knowledge_space_id))
    except ReleaseSignatureError as exc:
        raise ReleaseEvaluationError(exc.code) from exc
    if approval is None:
        raise ReleaseEvaluationError("EVAL_DATASET_NOT_APPROVED")
    from platform_core.knowledge.service import get_object

    try:
        payload = get_object(approval.object_key)
    except Exception as exc:  # noqa: BLE001 - private object-storage boundary
        raise ReleaseEvaluationError("EVAL_DATASET_UNAVAILABLE") from exc
    if not payload or len(payload) > max_bytes:
        raise ReleaseEvaluationError("EVAL_DATASET_SIZE_INVALID")
    try:
        manifest = KnowledgeReleaseDatasetManifest.model_validate_json(payload)
    except Exception as exc:  # noqa: BLE001 - untrusted dataset file boundary
        raise ReleaseEvaluationError("EVAL_DATASET_SCHEMA_INVALID") from exc
    if manifest.tenant_id != tenant_id or manifest.knowledge_space_id != knowledge_space_id:
        raise ReleaseEvaluationError("EVAL_DATASET_SCOPE_MISMATCH")
    cases = tuple(EvalCase(**case.model_dump()) for case in manifest.cases)
    principal_scopes = {
        case_id: PrincipalScope(
            principal_types=scope.principal_types,
            principal_ids=scope.principal_ids,
        )
        for case_id, scope in manifest.principal_scopes.items()
    }
    actual_hash = release_dataset_sha256(
        cases,
        tenant_id=tenant_id,
        knowledge_space_id=knowledge_space_id,
        principal_scopes=principal_scopes,
    )
    if actual_hash != approval.sha256:
        raise ReleaseEvaluationError("EVAL_DATASET_HASH_MISMATCH")
    return LoadedReleaseDataset(
        approval=approval,
        cases=cases,
        principal_scopes=principal_scopes,
    )


async def run_candidate_aware_evaluation(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    knowledge_space_id: uuid.UUID,
    baseline_version_id: uuid.UUID,
    candidate_version_id: uuid.UUID,
    cases: Sequence[EvalCase],
    approved_dataset_sha256: str,
    principal_scopes: Mapping[str, PrincipalScope],
    answer_fn: AnswerFn,
    embedder: Embedder,
    embedding_model_id: str,
    embedding_dimensions: int = 1536,
    embedding_provider_endpoint: str = "",
    answerer_version: str,
    answerer_config_sha256: str,
    evaluator_version: str,
    commit_sha: str,
    key_of: Callable[[RetrievedChunk], str],
    retrieval_config: ReleaseRetrievalConfig | None = None,
    now_ts: int | None = None,
) -> CandidateEvaluationPair:
    """Run identical fixed cases against active baseline and staged candidate.

    The caller must load cases from the approved, tenant/space-bound dataset
    and resolve principal scopes from server-side identity records. The
    transaction must already have app.tenant_id bound and use REPEATABLE READ
    or SERIALIZABLE isolation before that binding is applied.
    """
    retrieval_config = retrieval_config or ReleaseRetrievalConfig()
    if not _SHA256_RE.fullmatch(approved_dataset_sha256):
        raise ReleaseEvaluationError("EVAL_APPROVED_DATASET_HASH_REQUIRED")
    scope_map = dict(principal_scopes)
    actual_dataset_hash = release_dataset_sha256(
        cases,
        tenant_id=tenant_id,
        knowledge_space_id=knowledge_space_id,
        principal_scopes=scope_map,
    )
    if actual_dataset_hash != approved_dataset_sha256:
        raise ReleaseEvaluationError("EVAL_DATASET_HASH_MISMATCH")
    if not _SHA256_RE.fullmatch(answerer_config_sha256):
        raise ReleaseEvaluationError("EVAL_ANSWERER_CONFIG_HASH_INVALID")
    if not answerer_version or not embedding_model_id or not evaluator_version:
        raise ReleaseEvaluationError("EVAL_COMPONENT_VERSION_REQUIRED")
    if not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", commit_sha):
        raise ReleaseEvaluationError("EVAL_COMMIT_SHA_INVALID")
    if (
        retrieval_config.top_k < 1
        or retrieval_config.rrf_k < 1
        or any(
            value < 1
            for value in (
                retrieval_config.fts_candidates,
                retrieval_config.vector_candidates,
                retrieval_config.trigram_candidates,
                retrieval_config.alias_candidates,
            )
        )
    ):
        raise ReleaseEvaluationError("EVAL_RETRIEVAL_CONFIG_INVALID")
    unknown_paths = sorted(set(retrieval_config.enabled_paths) - set(RETRIEVAL_PATHS))
    if unknown_paths or len(set(retrieval_config.enabled_paths)) != len(
        retrieval_config.enabled_paths
    ):
        raise ReleaseEvaluationError("EVAL_RETRIEVAL_PATH_INVALID", ",".join(unknown_paths))

    expected_case_ids = {case.case_id for case in cases}
    if set(scope_map) != expected_case_ids:
        raise ReleaseEvaluationError("EVAL_PRINCIPAL_SCOPE_SET_MISMATCH")
    for scope in scope_map.values():
        if (
            not scope.principal_types
            or not scope.principal_ids
            or len(scope.principal_types) != len(scope.principal_ids)
        ):
            raise ReleaseEvaluationError("EVAL_PRINCIPAL_SCOPE_INVALID")

    await _require_tenant_repeatable_read(session, tenant_id)
    fixed_now = int(time.time()) if now_ts is None else now_ts
    space = (
        await session.execute(
            select(KnowledgeSpace).where(
                KnowledgeSpace.tenant_id == tenant_id,
                KnowledgeSpace.id == knowledge_space_id,
                KnowledgeSpace.status == "active",
            )
        )
    ).scalar_one_or_none()
    if space is None:
        raise ReleaseEvaluationError("KNOWLEDGE_SPACE_NOT_FOUND")

    baseline_version = await _load_baseline_version(
        session,
        tenant_id=tenant_id,
        knowledge_space_id=knowledge_space_id,
        version_id=baseline_version_id,
    )
    candidate_version = await _load_candidate_version(
        session,
        tenant_id=tenant_id,
        knowledge_space_id=knowledge_space_id,
        version_id=candidate_version_id,
    )
    if baseline_version.id == candidate_version.id:
        raise ReleaseEvaluationError("CANDIDATE_VERSION_REUSED")
    if (
        (baseline_version.effective_at is not None and baseline_version.effective_at > fixed_now)
        or (baseline_version.expires_at is not None and baseline_version.expires_at <= fixed_now)
        or (
            candidate_version.effective_at is not None
            and candidate_version.effective_at > fixed_now
        )
        or (candidate_version.expires_at is not None and candidate_version.expires_at <= fixed_now)
    ):
        raise ReleaseEvaluationError("EVAL_VERSION_OUTSIDE_EFFECTIVE_WINDOW")
    candidate_chunk_count = int(
        (
            await session.execute(
                select(func.count())
                .select_from(Chunk)
                .where(
                    Chunk.tenant_id == tenant_id,
                    Chunk.document_version_id == candidate_version_id,
                )
            )
        ).scalar_one()
    )
    if candidate_chunk_count < 1:
        raise ReleaseEvaluationError("RELEASE_CANDIDATE_NOT_INDEXED")

    aliases = await load_aliases(session, tenant_id)
    retrieval_config_hash = _retrieval_config_sha256(
        config=retrieval_config,
        aliases=aliases,
        embedding_model_id=embedding_model_id,
        embedding_dimensions=embedding_dimensions,
        embedding_provider_endpoint=embedding_provider_endpoint,
        answerer_version=answerer_version,
        answerer_config_sha256=answerer_config_sha256,
        principal_scopes=scope_map,
    )
    baseline_snapshot = await _space_snapshot_sha256(
        session,
        tenant_id=tenant_id,
        knowledge_space_id=knowledge_space_id,
        candidate_version_id=None,
        now_ts=fixed_now,
    )
    candidate_snapshot = await _space_snapshot_sha256(
        session,
        tenant_id=tenant_id,
        knowledge_space_id=knowledge_space_id,
        candidate_version_id=candidate_version_id,
        now_ts=fixed_now,
    )

    async def retrieve_baseline(question: str, principal: PrincipalScope) -> list[RetrievedChunk]:
        return await _retrieve(
            session,
            tenant_id=tenant_id,
            knowledge_space_id=knowledge_space_id,
            question=question,
            principal=principal,
            aliases=aliases,
            embedder=embedder,
            retrieval_config=retrieval_config,
            now_ts=fixed_now,
        )

    async def retrieve_candidate(question: str, principal: PrincipalScope) -> list[RetrievedChunk]:
        return await _retrieve(
            session,
            tenant_id=tenant_id,
            knowledge_space_id=knowledge_space_id,
            question=question,
            principal=principal,
            aliases=aliases,
            embedder=embedder,
            retrieval_config=retrieval_config,
            now_ts=fixed_now,
            release_candidate=ReleaseCandidateScope(
                version_id=candidate_version_id,
                knowledge_space_id=knowledge_space_id,
            ),
        )

    def principal_scope_for_case(case: EvalCase) -> PrincipalScope:
        return scope_map[case.case_id]

    baseline_report = await EvaluationRunner(
        answer_fn,
        retrieve_baseline,
        key_of=key_of,
        principal_scope_for_case=principal_scope_for_case,
    ).run(list(cases))
    candidate_report = await EvaluationRunner(
        answer_fn,
        retrieve_candidate,
        key_of=key_of,
        principal_scope_for_case=principal_scope_for_case,
    ).run(list(cases))

    if (
        release_dataset_sha256(
            cases,
            tenant_id=tenant_id,
            knowledge_space_id=knowledge_space_id,
            principal_scopes=scope_map,
        )
        != actual_dataset_hash
    ):
        raise ReleaseEvaluationError("EVAL_DATASET_MOVED")
    if (
        await _space_snapshot_sha256(
            session,
            tenant_id=tenant_id,
            knowledge_space_id=knowledge_space_id,
            candidate_version_id=None,
            now_ts=fixed_now,
        )
        != baseline_snapshot
        or await _space_snapshot_sha256(
            session,
            tenant_id=tenant_id,
            knowledge_space_id=knowledge_space_id,
            candidate_version_id=candidate_version_id,
            now_ts=fixed_now,
        )
        != candidate_snapshot
    ):
        raise ReleaseEvaluationError("EVAL_KNOWLEDGE_SNAPSHOT_MOVED")

    evaluated_at = datetime.fromtimestamp(fixed_now, tz=UTC)
    baseline_run = KnowledgeEvalRun(
        tenant_id=tenant_id,
        knowledge_space_id=knowledge_space_id,
        document_version_id=baseline_version.id,
        knowledge_content_sha256=document_content_sha256(baseline_version.content_hash),
        knowledge_snapshot_sha256=baseline_snapshot,
        parent_snapshot_sha256=None,
        dataset_sha256=actual_dataset_hash,
        retrieval_config_sha256=retrieval_config_hash,
        evaluator_version=evaluator_version,
        commit_sha=commit_sha,
        evaluated_at=evaluated_at,
        metrics=_report_metrics(baseline_report),
    )
    candidate_run = KnowledgeEvalRun(
        tenant_id=tenant_id,
        knowledge_space_id=knowledge_space_id,
        document_version_id=candidate_version.id,
        knowledge_content_sha256=document_content_sha256(candidate_version.content_hash),
        knowledge_snapshot_sha256=candidate_snapshot,
        parent_snapshot_sha256=baseline_snapshot,
        dataset_sha256=actual_dataset_hash,
        retrieval_config_sha256=retrieval_config_hash,
        evaluator_version=evaluator_version,
        commit_sha=commit_sha,
        evaluated_at=evaluated_at,
        metrics=_report_metrics(candidate_report),
    )
    return CandidateEvaluationPair(
        baseline_run=baseline_run,
        candidate_run=candidate_run,
        baseline_result_sha256=_report_sha256(baseline_report),
        candidate_result_sha256=_report_sha256(candidate_report),
        retrieval_at=fixed_now,
    )


async def run_published_release_post_test(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    knowledge_space_id: uuid.UUID,
    candidate_version_id: uuid.UUID,
    pre_publish_run: KnowledgeEvalRun,
    cases: Sequence[EvalCase],
    approved_dataset_sha256: str,
    principal_scopes: Mapping[str, PrincipalScope],
    answer_fn: AnswerFn,
    embedder: Embedder,
    embedding_model_id: str,
    embedding_dimensions: int = 1536,
    embedding_provider_endpoint: str = "",
    answerer_version: str,
    answerer_config_sha256: str,
    evaluator_version: str,
    commit_sha: str,
    key_of: Callable[[RetrievedChunk], str],
    retrieval_config: ReleaseRetrievalConfig | None = None,
    now_ts: int | None = None,
) -> PublishedPostTestResult:
    """Measure the published candidate under the same fixed-set contract."""
    retrieval_config = retrieval_config or ReleaseRetrievalConfig()
    scope_map = dict(principal_scopes)
    dataset_hash = release_dataset_sha256(
        cases,
        tenant_id=tenant_id,
        knowledge_space_id=knowledge_space_id,
        principal_scopes=scope_map,
    )
    if (
        dataset_hash != approved_dataset_sha256
        or pre_publish_run.dataset_sha256 != approved_dataset_sha256
        or pre_publish_run.tenant_id != tenant_id
        or pre_publish_run.knowledge_space_id != knowledge_space_id
        or pre_publish_run.document_version_id != candidate_version_id
        or pre_publish_run.parent_snapshot_sha256 is None
    ):
        raise ReleaseEvaluationError("POST_TEST_INPUT_MISMATCH")
    if not _SHA256_RE.fullmatch(answerer_config_sha256):
        raise ReleaseEvaluationError("EVAL_ANSWERER_CONFIG_HASH_INVALID")
    if not answerer_version or not embedding_model_id or not evaluator_version:
        raise ReleaseEvaluationError("EVAL_COMPONENT_VERSION_REQUIRED")
    if not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", commit_sha):
        raise ReleaseEvaluationError("EVAL_COMMIT_SHA_INVALID")
    if (
        retrieval_config.top_k < 1
        or retrieval_config.rrf_k < 1
        or any(
            value < 1
            for value in (
                retrieval_config.fts_candidates,
                retrieval_config.vector_candidates,
                retrieval_config.trigram_candidates,
                retrieval_config.alias_candidates,
            )
        )
        or not retrieval_config.enabled_paths
        or set(retrieval_config.enabled_paths) - set(RETRIEVAL_PATHS)
        or len(set(retrieval_config.enabled_paths)) != len(retrieval_config.enabled_paths)
    ):
        raise ReleaseEvaluationError("EVAL_RETRIEVAL_CONFIG_INVALID")

    if set(scope_map) != {case.case_id for case in cases}:
        raise ReleaseEvaluationError("EVAL_PRINCIPAL_SCOPE_SET_MISMATCH")
    for scope in scope_map.values():
        if (
            not scope.principal_types
            or not scope.principal_ids
            or len(scope.principal_types) != len(scope.principal_ids)
        ):
            raise ReleaseEvaluationError("EVAL_PRINCIPAL_SCOPE_INVALID")
    await _require_tenant_repeatable_read(session, tenant_id)
    fixed_now = int(time.time()) if now_ts is None else now_ts
    if fixed_now <= int(pre_publish_run.evaluated_at.timestamp()):
        raise ReleaseEvaluationError("POST_TEST_TIME_INVALID")
    version = await _load_baseline_version(
        session,
        tenant_id=tenant_id,
        knowledge_space_id=knowledge_space_id,
        version_id=candidate_version_id,
    )
    canonical_uri = (
        await session.execute(
            select(Document.canonical_uri).where(
                Document.tenant_id == tenant_id,
                Document.id == version.document_id,
                Document.space_id == knowledge_space_id,
            )
        )
    ).scalar_one_or_none()
    if (
        canonical_uri is None
        or not canonical_uri.startswith("gap-candidate://")
        or (version.metadata_json or {}).get("release_candidate") is not True
        or document_content_sha256(version.content_hash) != pre_publish_run.knowledge_content_sha256
    ):
        raise ReleaseEvaluationError("POST_TEST_RELEASE_NOT_ACTIVE")
    aliases = await load_aliases(session, tenant_id)
    config_hash = _retrieval_config_sha256(
        config=retrieval_config,
        aliases=aliases,
        embedding_model_id=embedding_model_id,
        embedding_dimensions=embedding_dimensions,
        embedding_provider_endpoint=embedding_provider_endpoint,
        answerer_version=answerer_version,
        answerer_config_sha256=answerer_config_sha256,
        principal_scopes=scope_map,
    )
    snapshot = await _space_snapshot_sha256(
        session,
        tenant_id=tenant_id,
        knowledge_space_id=knowledge_space_id,
        candidate_version_id=None,
        now_ts=fixed_now,
    )

    async def retrieve(question: str, principal: PrincipalScope) -> list[RetrievedChunk]:
        return await _retrieve(
            session,
            tenant_id=tenant_id,
            knowledge_space_id=knowledge_space_id,
            question=question,
            principal=principal,
            aliases=aliases,
            embedder=embedder,
            retrieval_config=retrieval_config,
            now_ts=fixed_now,
        )

    report = await EvaluationRunner(
        answer_fn,
        retrieve,
        key_of=key_of,
        principal_scope_for_case=lambda case: scope_map[case.case_id],
    ).run(list(cases))
    if report.total != pre_publish_run.metrics.case_count:
        raise ReleaseEvaluationError("POST_TEST_CASE_COUNT_MISMATCH")
    if (
        release_dataset_sha256(
            cases,
            tenant_id=tenant_id,
            knowledge_space_id=knowledge_space_id,
            principal_scopes=scope_map,
        )
        != dataset_hash
        or await _space_snapshot_sha256(
            session,
            tenant_id=tenant_id,
            knowledge_space_id=knowledge_space_id,
            candidate_version_id=None,
            now_ts=fixed_now,
        )
        != snapshot
    ):
        raise ReleaseEvaluationError("POST_TEST_INPUT_MOVED")
    post_run = KnowledgeEvalRun(
        tenant_id=tenant_id,
        knowledge_space_id=knowledge_space_id,
        document_version_id=candidate_version_id,
        knowledge_content_sha256=document_content_sha256(version.content_hash),
        knowledge_snapshot_sha256=snapshot,
        parent_snapshot_sha256=pre_publish_run.parent_snapshot_sha256,
        dataset_sha256=dataset_hash,
        retrieval_config_sha256=config_hash,
        evaluator_version=evaluator_version,
        commit_sha=commit_sha,
        evaluated_at=datetime.fromtimestamp(fixed_now, tz=UTC),
        metrics=_report_metrics(report),
    )
    return PublishedPostTestResult(
        post_run=post_run,
        result_sha256=_report_sha256(report),
        retrieval_at=fixed_now,
    )


def _retrieval_config_sha256(
    *,
    config: ReleaseRetrievalConfig,
    aliases: list[tuple[str, str, float]],
    embedding_model_id: str,
    embedding_dimensions: int,
    embedding_provider_endpoint: str,
    answerer_version: str,
    answerer_config_sha256: str,
    principal_scopes: Mapping[str, PrincipalScope],
) -> str:
    return _canonical_sha256(
        {
            "overlay_version": "release-candidate-overlay-v1",
            "retrieval": {
                **asdict(config),
                "enabled_paths": sorted(config.enabled_paths),
            },
            "aliases": aliases,
            "embedding_model_id": embedding_model_id,
            "embedding_dimensions": embedding_dimensions,
            "embedding_provider_endpoint": embedding_provider_endpoint.rstrip("/"),
            "answerer_version": answerer_version,
            "answerer_config_sha256": answerer_config_sha256,
            "principal_scopes": {
                case_id: list(zip(scope.principal_types, scope.principal_ids, strict=True))
                for case_id, scope in sorted(principal_scopes.items())
            },
        }
    )


async def _retrieve(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    knowledge_space_id: uuid.UUID,
    question: str,
    principal: PrincipalScope,
    aliases: list[tuple[str, str, float]],
    embedder: Embedder,
    retrieval_config: ReleaseRetrievalConfig,
    now_ts: int,
    release_candidate: ReleaseCandidateScope | None = None,
) -> list[RetrievedChunk]:
    return await hybrid_search(
        session,
        tenant_id=tenant_id,
        query=question,
        top_k=retrieval_config.top_k,
        knowledge_space_ids=[knowledge_space_id],
        principal=principal,
        now_ts=now_ts,
        fts_candidates=retrieval_config.fts_candidates,
        vector_candidates=retrieval_config.vector_candidates,
        trigram_candidates=retrieval_config.trigram_candidates,
        alias_candidates=retrieval_config.alias_candidates,
        enabled_paths=retrieval_config.enabled_paths,
        rrf_k=retrieval_config.rrf_k,
        aliases=aliases,
        embedder=embedder,
        release_candidate=release_candidate,
    )


async def _require_tenant_repeatable_read(session: AsyncSession, tenant_id: uuid.UUID) -> None:
    row = (
        await session.execute(
            text(
                "SELECT current_setting('transaction_isolation'), "
                "NULLIF(current_setting('app.tenant_id', true), '')"
            )
        )
    ).one()
    if str(row[0]).lower() not in {"repeatable read", "serializable"}:
        raise ReleaseEvaluationError("EVAL_REPEATABLE_READ_REQUIRED")
    if row[1] is None or uuid.UUID(str(row[1])) != tenant_id:
        raise ReleaseEvaluationError("EVAL_TENANT_SCOPE_REQUIRED")


async def _load_baseline_version(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    knowledge_space_id: uuid.UUID,
    version_id: uuid.UUID,
) -> DocumentVersion:
    row = (
        await session.execute(
            select(DocumentVersion)
            .join(Document, Document.id == DocumentVersion.document_id)
            .where(
                DocumentVersion.tenant_id == tenant_id,
                DocumentVersion.id == version_id,
                Document.space_id == knowledge_space_id,
                Document.tenant_id == tenant_id,
                DocumentVersion.status == "active",
                DocumentVersion.ingestion_status == "ready",
                DocumentVersion.scan_status == "clean",
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise ReleaseEvaluationError("BASELINE_VERSION_NOT_READY")
    return row


async def _load_candidate_version(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    knowledge_space_id: uuid.UUID,
    version_id: uuid.UUID,
) -> DocumentVersion:
    row = (
        await session.execute(
            select(DocumentVersion)
            .join(Document, Document.id == DocumentVersion.document_id)
            .where(
                DocumentVersion.tenant_id == tenant_id,
                DocumentVersion.id == version_id,
                Document.space_id == knowledge_space_id,
                Document.tenant_id == tenant_id,
                Document.canonical_uri.like("gap-candidate://%"),
                DocumentVersion.status == "draft",
                DocumentVersion.metadata_json["release_candidate"].as_boolean().is_(True),
                DocumentVersion.ingestion_status == "ready",
                DocumentVersion.scan_status == "clean",
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise ReleaseEvaluationError("RELEASE_CANDIDATE_NOT_READY")
    return row


async def _space_snapshot_sha256(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    knowledge_space_id: uuid.UUID,
    candidate_version_id: uuid.UUID | None,
    now_ts: int,
) -> str:
    space = (
        await session.execute(
            select(KnowledgeSpace).where(
                KnowledgeSpace.tenant_id == tenant_id,
                KnowledgeSpace.id == knowledge_space_id,
                KnowledgeSpace.status == "active",
            )
        )
    ).scalar_one_or_none()
    if space is None:
        raise ReleaseEvaluationError("KNOWLEDGE_SPACE_NOT_FOUND")
    active_rows = (
        await session.execute(
            select(Document, DocumentVersion)
            .join(DocumentVersion, DocumentVersion.document_id == Document.id)
            .where(
                Document.tenant_id == tenant_id,
                Document.space_id == knowledge_space_id,
                DocumentVersion.tenant_id == tenant_id,
                DocumentVersion.status == "active",
                or_(
                    DocumentVersion.effective_at.is_(None),
                    DocumentVersion.effective_at <= now_ts,
                ),
                or_(
                    DocumentVersion.expires_at.is_(None),
                    DocumentVersion.expires_at > now_ts,
                ),
            )
        )
    ).all()
    rows: list[tuple[Document, DocumentVersion, str]] = [
        (document, version, "active") for document, version in active_rows
    ]
    if candidate_version_id is not None:
        candidate = await _load_candidate_version(
            session,
            tenant_id=tenant_id,
            knowledge_space_id=knowledge_space_id,
            version_id=candidate_version_id,
        )
        document = (
            await session.execute(
                select(Document).where(
                    Document.tenant_id == tenant_id,
                    Document.id == candidate.document_id,
                    Document.space_id == knowledge_space_id,
                )
            )
        ).scalar_one()
        rows.append((document, candidate, "candidate"))

    rows.sort(key=lambda item: str(item[1].id))
    document_ids = {document.id for document, _, _ in rows}
    version_ids = {version.id for _, version, _ in rows}
    chunk_rows: list[Any] = []
    if version_ids:
        chunk_rows.extend(
            (
                await session.execute(
                    select(
                        Chunk.document_version_id,
                        Chunk.id,
                        Chunk.ordinal,
                        Chunk.section_path,
                        Chunk.text_hash,
                        Chunk.metadata_json,
                    )
                    .where(
                        Chunk.tenant_id == tenant_id,
                        Chunk.document_version_id.in_(version_ids),
                    )
                    .order_by(Chunk.document_version_id, Chunk.ordinal, Chunk.id)
                )
            ).all()
        )
    acl_predicates = [
        (KnowledgeAcl.resource_type == "space") & (KnowledgeAcl.resource_id == knowledge_space_id)
    ]
    if document_ids:
        acl_predicates.append(
            (KnowledgeAcl.resource_type == "document") & KnowledgeAcl.resource_id.in_(document_ids)
        )
    if version_ids:
        acl_predicates.append(
            (KnowledgeAcl.resource_type == "version") & KnowledgeAcl.resource_id.in_(version_ids)
        )
    acl_rows = (
        await session.execute(
            select(
                KnowledgeAcl.resource_type,
                KnowledgeAcl.resource_id,
                KnowledgeAcl.principal_type,
                KnowledgeAcl.principal_id,
                KnowledgeAcl.permission,
            )
            .where(
                KnowledgeAcl.tenant_id == tenant_id,
                or_(*acl_predicates),
            )
            .order_by(
                KnowledgeAcl.resource_type,
                KnowledgeAcl.resource_id,
                KnowledgeAcl.principal_type,
                KnowledgeAcl.principal_id,
                KnowledgeAcl.permission,
            )
        )
    ).all()

    snapshot_versions = []
    for document, version, visibility in rows:
        is_release_candidate = (version.metadata_json or {}).get("release_candidate") is True
        metadata = {
            key: value
            for key, value in (version.metadata_json or {}).items()
            if key != "release_candidate" and not key.startswith("release_candidate_")
        }
        snapshot_versions.append(
            {
                "visibility": "release_candidate" if is_release_candidate else visibility,
                "document_id": str(document.id),
                "document_uri": document.canonical_uri,
                "document_title": document.title,
                "classification": document.classification,
                "version_id": str(version.id),
                "content_sha256": version.content_hash,
                "status": "release_candidate" if is_release_candidate else version.status,
                "ingestion_status": version.ingestion_status,
                "scan_status": version.scan_status,
                "effective_at": version.effective_at,
                "expires_at": version.expires_at,
                "parser_version": version.parser_version,
                "metadata": metadata,
            }
        )

    snapshot_chunks = [
        {
            "version_id": str(chunk.document_version_id),
            "chunk_id": str(chunk.id),
            "ordinal": chunk.ordinal,
            "section_path": chunk.section_path,
            "text_sha256": chunk.text_hash,
            "metadata": chunk.metadata_json,
        }
        for chunk in chunk_rows
    ]

    snapshot_acls = [
        {
            "resource_type": row.resource_type,
            "resource_id": str(row.resource_id),
            "principal_type": row.principal_type,
            "principal_id": row.principal_id,
            "permission": row.permission,
        }
        for row in acl_rows
    ]
    return _canonical_sha256(
        {
            "tenant_id": str(tenant_id),
            "knowledge_space_id": str(knowledge_space_id),
            "space": {
                "status": space.status,
                "default_policy_id": (
                    str(space.default_policy_id) if space.default_policy_id is not None else None
                ),
            },
            "versions": snapshot_versions,
            "chunks": snapshot_chunks,
            "acls": snapshot_acls,
        }
    )


def _report_metrics(report: EvalReport) -> KnowledgeEvalMetrics:
    if report.total < 1:
        raise ReleaseEvaluationError("EVAL_REPORT_EMPTY")
    unsafe_cases = sum(
        1
        for result in report.results
        if result.forbidden_hit or result.unsupported_claims or not result.citation_ok
    )
    return KnowledgeEvalMetrics(
        case_count=report.total,
        grounded_answer_rate=report.passed / report.total,
        citation_support_rate=report.citation_coverage,
        retrieval_recall_at_k=report.retrieval_recall_mean,
        unsafe_answer_count=unsafe_cases + report.unsafe_action_attempts,
    )


def _report_sha256(report: EvalReport) -> str:
    outcomes = [
        {
            "case_id": result.case_id,
            "passed": result.passed,
            "abstained": result.abstained,
            "handoff": result.handoff,
            "citation_ok": result.citation_ok,
            "unsupported_claims": result.unsupported_claims,
            "forbidden_hit": result.forbidden_hit,
            "reason_codes": result.reason_codes,
            "retrieved_keys": result.retrieved_keys,
        }
        for result in report.results
    ]
    return _canonical_sha256(outcomes)


def _canonical_sha256(payload: Any) -> str:
    canonical = json.dumps(
        payload,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


__all__ = [
    "CandidateEvaluationPair",
    "LoadedReleaseDataset",
    "PublishedPostTestResult",
    "ReleaseEvaluationError",
    "ReleaseRetrievalConfig",
    "release_dataset_sha256",
    "load_approved_release_dataset",
    "release_document_key",
    "run_candidate_aware_evaluation",
    "run_published_release_post_test",
]
