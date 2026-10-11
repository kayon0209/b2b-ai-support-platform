"""Dedicated, tenant-isolated worker for signed knowledge release jobs.

The worker is a separate role because it holds a signing key and may call paid
model endpoints. It is inert unless both auto-run and a positive per-run case
ceiling are explicitly configured.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time
import uuid
from dataclasses import dataclass
from typing import Any

from pydantic import SecretStr, ValidationError
from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from observability import JsonLogger
from observability_metrics import get_metrics
from platform_contracts.knowledge_release_jobs import (
    ReleaseEvaluationRequested,
    ReleasePostTestRequested,
)
from platform_core.config import Settings, get_settings
from platform_core.execution_budget import (
    AttemptBudgetExhausted,
    ExecutionBudget,
    run_with_execution_budget,
)
from platform_core.identity.tenant_context import TenantContext, tenant_session
from platform_core.knowledge.release_evaluator import (
    RELEASE_EVALUATION_REQUEST_EVENT,
    RELEASE_POST_TEST_REQUEST_EVENT,
)
from platform_core.knowledge.release_models import KnowledgeReleaseEvaluation
from platform_core.knowledge.release_signatures import (
    ReleaseSignatureError,
    configured_approved_release_datasets,
    configured_evaluator_public_keys,
    require_signing_key_matches_trust_root,
)
from platform_core.outbox import OutboxEvent, OutboxStatus
from worker import release_evaluator
from worker.wiring import WorkerConfigurationError, queue_bookkeeping_session

logger = JsonLogger("platform.worker")

RELEASE_EVALUATION_BATCH = 1
RELEASE_EVALUATION_MAX_ATTEMPTS = 3
RELEASE_EVALUATION_STALE_SECONDS = 180
RELEASE_EVALUATION_HEARTBEAT_SECONDS = 30
RELEASE_EVALUATION_IN_FLIGHT = "processing"
RELEASE_EVALUATION_RESULTS = (
    "pre_eligible",
    "pre_blocked",
    "post_passed",
    "post_blocked",
    "retried",
    "failed",
    "claim_lost",
    "stale_reclaimed",
)
_KEY_ID = re.compile(r"^[A-Za-z0-9._-]{1,63}$")
_COMMIT_SHA = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_ERROR_CODE = re.compile(r"^[A-Z][A-Z0-9_]{0,62}$")
_SERVICE_ACTOR_ID = uuid.uuid5(
    uuid.NAMESPACE_URL, "urn:platform:service:knowledge-release-evaluator"
)


@dataclass(frozen=True)
class ClaimedReleaseJob:
    """Metadata-only claim returned by the owner-role queue query."""

    event_id: uuid.UUID
    tenant_id: uuid.UUID
    processing_token: uuid.UUID
    attempt: int
    first_attempt_at: int
    deadline_at: int
    external_attempt_limit: int


@dataclass(frozen=True)
class ReleaseEvaluatorRuntime:
    answer_fn: Any
    embedder: Any
    embedding_model_id: str
    embedding_dimensions: int
    embedding_provider_endpoint: str
    answerer_version: str
    answerer_config_sha256: str
    evaluator_version: str
    commit_sha: str
    key_id: str
    private_key: SecretStr
    max_cases_per_run: int


def build_release_evaluator_runtime() -> ReleaseEvaluatorRuntime:
    """Resolve dedicated worker-only signing credentials and model clients."""
    settings = get_settings()
    if (
        not settings.knowledge_evaluator_auto_run
        or settings.knowledge_evaluator_max_cases_per_run < 1
    ):
        raise WorkerConfigurationError(
            "set APP_KNOWLEDGE_EVALUATOR_AUTO_RUN=true and a positive "
            "APP_KNOWLEDGE_EVALUATOR_MAX_CASES_PER_RUN to enable release evaluation"
        )
    private_key_value = os.environ.get("APP_KNOWLEDGE_EVALUATOR_PRIVATE_KEY_B64URL", "")
    key_id = os.environ.get("APP_KNOWLEDGE_EVALUATOR_KEY_ID", "")
    commit_sha = os.environ.get("APP_BUILD_COMMIT_SHA", "")
    if not private_key_value or not _KEY_ID.fullmatch(key_id):
        raise WorkerConfigurationError(
            "the dedicated worker requires APP_KNOWLEDGE_EVALUATOR_PRIVATE_KEY_B64URL "
            "and APP_KNOWLEDGE_EVALUATOR_KEY_ID from its secret manager"
        )
    if not _COMMIT_SHA.fullmatch(commit_sha):
        raise WorkerConfigurationError(
            "the dedicated worker requires APP_BUILD_COMMIT_SHA as a full git commit SHA"
        )
    private_key = SecretStr(private_key_value)
    try:
        public_keys = configured_evaluator_public_keys()
        if not configured_approved_release_datasets():
            raise WorkerConfigurationError(
                "the dedicated worker requires an approved release dataset allowlist"
            )
        require_signing_key_matches_trust_root(
            key_id=key_id,
            private_key=private_key,
            trusted_public_keys=public_keys,
        )
    except ReleaseSignatureError as exc:
        raise WorkerConfigurationError(
            f"release evaluator trust configuration failed: {exc.code}"
        ) from exc

    from platform_core.llm.factory import get_model_bundle
    from platform_core.retrieval.hybrid import ProviderEmbedder

    bundle = get_model_bundle()
    if bundle is None or bundle.embedding is None:
        raise WorkerConfigurationError(
            "the release evaluator requires configured chat and embedding providers"
        )
    answer_fn, answerer_version, answerer_config_sha256 = release_evaluator.build_release_answerer(
        bundle.chat,
        primary_model=settings.llm_model,
        provider_base_url=settings.llm_base_url,
        timeout_seconds=settings.llm_timeout_seconds,
        max_retries=settings.llm_max_retries,
        fallback_model=(settings.model_fallback_name if settings.model_fallback_enabled else None),
    )
    return ReleaseEvaluatorRuntime(
        answer_fn=answer_fn,
        embedder=ProviderEmbedder(bundle.embedding),
        embedding_model_id=settings.llm_embedding_model,
        embedding_dimensions=settings.llm_embedding_dimensions,
        embedding_provider_endpoint=settings.llm_base_url,
        answerer_version=answerer_version,
        answerer_config_sha256=answerer_config_sha256,
        evaluator_version="knowledge-release-evaluator-v1",
        commit_sha=commit_sha,
        key_id=key_id,
        private_key=private_key,
        max_cases_per_run=settings.knowledge_evaluator_max_cases_per_run,
    )


def context_for(claim: ClaimedReleaseJob) -> TenantContext:
    return TenantContext(
        tenant_id=claim.tenant_id,
        actor_id=_SERVICE_ACTOR_ID,
        actor_kind="system",
        role="integration_service",
    )


async def claim_release_jobs(
    session: AsyncSession, *, batch: int = RELEASE_EVALUATION_BATCH
) -> list[ClaimedReleaseJob]:
    """Claim metadata only; request payloads stay behind tenant RLS."""
    now = int(time.time())
    stale_cutoff = now - RELEASE_EVALUATION_STALE_SECONDS
    settings = get_settings()
    expired_queue = await session.execute(
        update(OutboxEvent)
        .where(
            OutboxEvent.event_type.in_(
                (RELEASE_EVALUATION_REQUEST_EVENT, RELEASE_POST_TEST_REQUEST_EVENT)
            ),
            OutboxEvent.status == OutboxStatus.QUEUED.value,
            OutboxEvent.deadline_at <= now,
        )
        .values(
            status=OutboxStatus.FAILED.value,
            processing_started_at=None,
            processing_token=None,
            last_error="release_evaluator_deadline_exhausted",
        )
    )
    expired_count = int(getattr(expired_queue, "rowcount", 0) or 0)
    if expired_count:
        get_metrics().knowledge_release_jobs_total.labels(result="failed").inc(expired_count)
        logger.error("release_evaluator_deadline_exhausted", count=expired_count)
    eligible_status = or_(
        OutboxEvent.status == OutboxStatus.QUEUED.value,
        and_(
            OutboxEvent.status == RELEASE_EVALUATION_IN_FLIGHT,
            OutboxEvent.processing_started_at <= stale_cutoff,
        ),
    )
    rows = (
        await session.execute(
            select(
                OutboxEvent.id,
                OutboxEvent.event_id,
                OutboxEvent.tenant_id,
                OutboxEvent.attempts,
                OutboxEvent.first_attempt_at,
                OutboxEvent.deadline_at,
                OutboxEvent.external_attempt_limit,
            )
            .where(
                OutboxEvent.event_type.in_(
                    (RELEASE_EVALUATION_REQUEST_EVENT, RELEASE_POST_TEST_REQUEST_EVENT)
                ),
                eligible_status,
                OutboxEvent.attempts < RELEASE_EVALUATION_MAX_ATTEMPTS,
                or_(
                    OutboxEvent.deadline_at.is_(None),
                    OutboxEvent.deadline_at > now,
                ),
            )
            .order_by(OutboxEvent.created_at, OutboxEvent.id)
            .limit(batch)
            .with_for_update(skip_locked=True)
        )
    ).all()
    claims: list[ClaimedReleaseJob] = []
    for row in rows:
        token = uuid.uuid4()
        first_attempt_at = int(row.first_attempt_at or now)
        deadline_at = int(
            row.deadline_at or first_attempt_at + settings.knowledge_evaluator_job_deadline_seconds
        )
        external_attempt_limit = int(
            row.external_attempt_limit or settings.knowledge_evaluator_max_external_attempts
        )
        await session.execute(
            update(OutboxEvent)
            .where(OutboxEvent.id == row.id)
            .values(
                status=RELEASE_EVALUATION_IN_FLIGHT,
                processing_started_at=now,
                processing_token=token,
                first_attempt_at=func.coalesce(OutboxEvent.first_attempt_at, now),
                deadline_at=func.coalesce(OutboxEvent.deadline_at, deadline_at),
                external_attempt_limit=func.coalesce(
                    OutboxEvent.external_attempt_limit,
                    settings.knowledge_evaluator_max_external_attempts,
                ),
                attempts=OutboxEvent.attempts + 1,
            )
        )
        claims.append(
            ClaimedReleaseJob(
                event_id=row.event_id,
                tenant_id=row.tenant_id,
                processing_token=token,
                attempt=int(row.attempts) + 1,
                first_attempt_at=first_attempt_at,
                deadline_at=deadline_at,
                external_attempt_limit=external_attempt_limit,
            )
        )
    return claims


async def reclaim_stale_release_jobs(session: AsyncSession) -> tuple[int, int]:
    """Requeue stale claims or fail them once their bounded retry budget ends."""
    now = int(time.time())
    cutoff = now - RELEASE_EVALUATION_STALE_SECONDS
    base = and_(
        OutboxEvent.event_type.in_(
            (RELEASE_EVALUATION_REQUEST_EVENT, RELEASE_POST_TEST_REQUEST_EVENT)
        ),
        OutboxEvent.status == RELEASE_EVALUATION_IN_FLIGHT,
        OutboxEvent.processing_started_at <= cutoff,
    )
    deadline_failed = await session.execute(
        update(OutboxEvent)
        .where(base, OutboxEvent.deadline_at <= now)
        .values(
            status=OutboxStatus.FAILED.value,
            processing_started_at=None,
            processing_token=None,
            last_error="release_evaluator_deadline_exhausted",
        )
    )
    within_deadline = and_(
        base,
        or_(
            OutboxEvent.deadline_at.is_(None),
            OutboxEvent.deadline_at > now,
        ),
    )
    reclaimed = await session.execute(
        update(OutboxEvent)
        .where(within_deadline, OutboxEvent.attempts < RELEASE_EVALUATION_MAX_ATTEMPTS)
        .values(
            status=OutboxStatus.QUEUED.value,
            processing_started_at=None,
            processing_token=None,
            last_error="release_evaluator_stale_claim",
        )
    )
    failed = await session.execute(
        update(OutboxEvent)
        .where(within_deadline, OutboxEvent.attempts >= RELEASE_EVALUATION_MAX_ATTEMPTS)
        .values(
            status=OutboxStatus.FAILED.value,
            processing_started_at=None,
            processing_token=None,
            last_error="release_evaluator_attempts_exhausted",
        )
    )
    return (
        int(getattr(reclaimed, "rowcount", 0) or 0),
        int(getattr(failed, "rowcount", 0) or 0)
        + int(getattr(deadline_failed, "rowcount", 0) or 0),
    )


async def _load_job(claim: ClaimedReleaseJob) -> tuple[str, dict[str, Any]]:
    async with tenant_session(context_for(claim)) as session:
        row = (
            await session.execute(
                select(OutboxEvent).where(
                    OutboxEvent.tenant_id == claim.tenant_id,
                    OutboxEvent.event_id == claim.event_id,
                    OutboxEvent.status == RELEASE_EVALUATION_IN_FLIGHT,
                    OutboxEvent.processing_token == claim.processing_token,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            raise RuntimeError("RELEASE_CLAIM_LOST")
        event_type = row.event_type
        payload = dict(row.payload or {})
    return event_type, payload


def _validate_payload(event_type: str, payload: dict[str, Any]) -> Any:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    parsed: ReleaseEvaluationRequested | ReleasePostTestRequested
    try:
        if event_type == RELEASE_EVALUATION_REQUEST_EVENT:
            parsed = ReleaseEvaluationRequested.model_validate_json(raw)
        elif event_type == RELEASE_POST_TEST_REQUEST_EVENT:
            parsed = ReleasePostTestRequested.model_validate_json(raw)
        else:
            raise ValueError("RELEASE_EVENT_TYPE_INVALID")
    except ValidationError as exc:
        raise ValueError("RELEASE_JOB_PAYLOAD_INVALID") from exc
    base = parsed.model_dump(mode="json", exclude={"request_hash"})
    expected_hash = hashlib.sha256(
        json.dumps(base, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    if expected_hash != parsed.request_hash:
        raise ValueError("RELEASE_JOB_REQUEST_HASH_INVALID")
    return parsed


async def _execute_job(
    claim: ClaimedReleaseJob,
    event_type: str,
    payload: dict[str, Any],
    runtime: ReleaseEvaluatorRuntime,
) -> str:
    ctx = context_for(claim)
    parsed = _validate_payload(event_type, payload)
    approval_map = configured_approved_release_datasets()
    if event_type == RELEASE_EVALUATION_REQUEST_EVENT:
        assert isinstance(parsed, ReleaseEvaluationRequested)
        approval = approval_map.get((claim.tenant_id, parsed.knowledge_space_id))
        if (
            approval is None
            or approval.sha256 != parsed.dataset_sha256
            or approval.approval_ref != parsed.dataset_approval_ref
        ):
            raise RuntimeError("EVAL_DATASET_NOT_APPROVED")
        _require_case_budget(runtime)
        _evaluation_id, status = await release_evaluator.evaluate_and_sign_release_candidate(
            ctx=ctx,
            draft_id=parsed.draft_id,
            knowledge_space_id=parsed.knowledge_space_id,
            baseline_version_id=parsed.baseline_version_id,
            candidate_version_id=parsed.candidate_version_id,
            idempotency_key=f"release-eval:{claim.event_id}",
            answer_fn=runtime.answer_fn,
            embedder=runtime.embedder,
            embedding_model_id=runtime.embedding_model_id,
            embedding_dimensions=runtime.embedding_dimensions,
            embedding_provider_endpoint=runtime.embedding_provider_endpoint,
            answerer_version=runtime.answerer_version,
            answerer_config_sha256=runtime.answerer_config_sha256,
            evaluator_version=runtime.evaluator_version,
            commit_sha=runtime.commit_sha,
            key_id=runtime.key_id,
            private_key=runtime.private_key,
            max_cases_per_run=runtime.max_cases_per_run,
        )
        return "pre_eligible" if status == "eligible" else "pre_blocked"

    assert isinstance(parsed, ReleasePostTestRequested)
    async with tenant_session(ctx) as session:
        evaluation = (
            await session.execute(
                select(KnowledgeReleaseEvaluation).where(
                    KnowledgeReleaseEvaluation.tenant_id == claim.tenant_id,
                    KnowledgeReleaseEvaluation.id == parsed.evaluation_id,
                )
            )
        ).scalar_one_or_none()
    if evaluation is None:
        raise RuntimeError("EVALUATION_NOT_FOUND")
    approval = approval_map.get((claim.tenant_id, evaluation.knowledge_space_id))
    if (
        approval is None
        or approval.sha256 != parsed.dataset_sha256
        or approval.approval_ref != parsed.dataset_approval_ref
    ):
        raise RuntimeError("EVAL_DATASET_NOT_APPROVED")
    _require_case_budget(runtime)
    _post_test_id, status = await release_evaluator.evaluate_and_sign_published_release(
        ctx=ctx,
        evaluation_id=parsed.evaluation_id,
        idempotency_key=f"release-post-test:{claim.event_id}",
        answer_fn=runtime.answer_fn,
        embedder=runtime.embedder,
        embedding_model_id=runtime.embedding_model_id,
        embedding_dimensions=runtime.embedding_dimensions,
        embedding_provider_endpoint=runtime.embedding_provider_endpoint,
        answerer_version=runtime.answerer_version,
        answerer_config_sha256=runtime.answerer_config_sha256,
        evaluator_version=runtime.evaluator_version,
        commit_sha=runtime.commit_sha,
        key_id=runtime.key_id,
        private_key=runtime.private_key,
        max_cases_per_run=runtime.max_cases_per_run,
    )
    return "post_passed" if status == "passed" else "post_blocked"


def _require_case_budget(runtime: ReleaseEvaluatorRuntime) -> None:
    settings: Settings = get_settings()
    if (
        not settings.knowledge_evaluator_auto_run
        or settings.knowledge_evaluator_max_cases_per_run != runtime.max_cases_per_run
        or runtime.max_cases_per_run <= 0
    ):
        raise RuntimeError("EVALUATOR_AUTORUN_DISABLED")


async def _heartbeat(claim: ClaimedReleaseJob, stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=RELEASE_EVALUATION_HEARTBEAT_SECONDS)
        except TimeoutError:
            pass
        if stop.is_set():
            return
        async with queue_bookkeeping_session() as session:
            result = await session.execute(
                update(OutboxEvent)
                .where(
                    OutboxEvent.tenant_id == claim.tenant_id,
                    OutboxEvent.event_id == claim.event_id,
                    OutboxEvent.status == RELEASE_EVALUATION_IN_FLIGHT,
                    OutboxEvent.processing_token == claim.processing_token,
                )
                .values(processing_started_at=int(time.time()))
            )
            await session.commit()
        if int(getattr(result, "rowcount", 0) or 0) != 1:
            return


def _release_attempt_budget(external_attempt_limit: int, attempt: int) -> int:
    """Allocate the fixed external-call ceiling across the durable job retries."""
    if attempt < 1 or attempt > RELEASE_EVALUATION_MAX_ATTEMPTS:
        return 0
    base, remainder = divmod(external_attempt_limit, RELEASE_EVALUATION_MAX_ATTEMPTS)
    return base + (1 if attempt <= remainder else 0)


def _budget_for_release_claim(
    claim: ClaimedReleaseJob,
    *,
    now: int | None = None,
) -> tuple[ExecutionBudget, float]:
    """Rebuild a bounded per-delivery budget from the persisted job deadline."""
    current = int(time.time()) if now is None else now
    remaining = claim.deadline_at - current
    if remaining <= 0:
        raise TimeoutError("RELEASE_EVALUATION_DEADLINE_EXCEEDED")
    attempts = _release_attempt_budget(claim.external_attempt_limit, claim.attempt)
    if attempts <= 0:
        raise AttemptBudgetExhausted("release_evaluator_attempt_budget_exhausted")
    return (
        ExecutionBudget.for_seconds(
            deadline_seconds=float(remaining),
            max_attempts=attempts,
            operation_limits={"model": attempts, "tool": 0, "outbound": 0},
        ),
        float(remaining),
    )


async def _execute_with_heartbeat(
    claim: ClaimedReleaseJob,
    event_type: str,
    payload: dict[str, Any],
    runtime: ReleaseEvaluatorRuntime,
) -> str:
    budget, remaining = _budget_for_release_claim(claim)
    stop = asyncio.Event()

    async def run_bounded_job() -> str:
        return await run_with_execution_budget(
            budget,
            asyncio.wait_for(_execute_job(claim, event_type, payload, runtime), timeout=remaining),
        )

    work = asyncio.create_task(run_bounded_job())
    heartbeat = asyncio.create_task(_heartbeat(claim, stop))
    done, _pending = await asyncio.wait({work, heartbeat}, return_when=asyncio.FIRST_COMPLETED)
    if heartbeat in done and not stop.is_set():
        work.cancel()
        await asyncio.gather(work, return_exceptions=True)
        raise RuntimeError("RELEASE_CLAIM_LOST")
    stop.set()
    heartbeat.cancel()
    await asyncio.gather(heartbeat, return_exceptions=True)
    return await work


async def _finish_claim(
    claim: ClaimedReleaseJob,
    *,
    result: str | None = None,
    error_code: str | None = None,
) -> bool:
    async with queue_bookkeeping_session() as session:
        values: dict[str, Any]
        if result is not None:
            values = {
                "status": OutboxStatus.SENT.value,
                "published_at": int(time.time()),
                "processing_started_at": None,
                "processing_token": None,
                "last_error": None,
            }
        else:
            terminal = claim.attempt >= RELEASE_EVALUATION_MAX_ATTEMPTS
            values = {
                "status": OutboxStatus.FAILED.value if terminal else OutboxStatus.QUEUED.value,
                "processing_started_at": None,
                "processing_token": None,
                "last_error": error_code or "RELEASE_EVALUATION_FAILED",
            }
        updated = await session.execute(
            update(OutboxEvent)
            .where(
                OutboxEvent.tenant_id == claim.tenant_id,
                OutboxEvent.event_id == claim.event_id,
                OutboxEvent.status == RELEASE_EVALUATION_IN_FLIGHT,
                OutboxEvent.processing_token == claim.processing_token,
            )
            .values(**values)
        )
        await session.commit()
    return int(getattr(updated, "rowcount", 0) or 0) == 1


class ReleaseEvaluationWorker:
    """Dedicated single-job-at-a-time queue with stale-claim recovery."""

    def __init__(self, runtime: ReleaseEvaluatorRuntime) -> None:
        self._runtime = runtime
        self._stopping = False

    def request_stop(self) -> None:
        self._stopping = True

    @property
    def stopping(self) -> bool:
        return self._stopping

    async def run_once(self) -> int:
        async with queue_bookkeeping_session() as session:
            reclaimed, exhausted = await reclaim_stale_release_jobs(session)
            claims = await claim_release_jobs(session)
            await session.commit()
        metrics = get_metrics()
        if reclaimed:
            metrics.knowledge_release_jobs_total.labels(result="stale_reclaimed").inc(reclaimed)
            logger.warning("release_evaluator_stale_claims_reclaimed", count=reclaimed)
        if exhausted:
            metrics.knowledge_release_jobs_total.labels(result="failed").inc(exhausted)
            logger.error("release_evaluator_stale_attempts_exhausted", count=exhausted)
        for claim in claims:
            try:
                event_type, payload = await _load_job(claim)
                result = await _execute_with_heartbeat(claim, event_type, payload, self._runtime)
            except Exception as exc:  # noqa: BLE001 - recorded as bounded queue state
                error_code = _safe_error_code(exc)
                owned = await _finish_claim(claim, error_code=error_code)
                metrics.knowledge_release_jobs_total.labels(
                    result="retried"
                    if owned and claim.attempt < RELEASE_EVALUATION_MAX_ATTEMPTS
                    else ("failed" if owned else "claim_lost")
                ).inc()
                logger.warning(
                    "release_evaluator_job_failed",
                    event_id=str(claim.event_id),
                    attempt=claim.attempt,
                    error_code=error_code,
                    claim_owned=owned,
                )
                continue
            owned = await _finish_claim(claim, result=result)
            final_result = result if owned else "claim_lost"
            metrics.knowledge_release_jobs_total.labels(result=final_result).inc()
            logger.info(
                "release_evaluator_job_completed",
                event_id=str(claim.event_id),
                result=final_result,
            )
        return len(claims)


def _safe_error_code(exc: Exception) -> str:
    if isinstance(exc, TimeoutError):
        return "RELEASE_EVALUATION_DEADLINE_EXCEEDED"
    if isinstance(exc, AttemptBudgetExhausted):
        return "RELEASE_EVALUATION_ATTEMPT_BUDGET_EXHAUSTED"
    code = getattr(exc, "code", None)
    if isinstance(code, str) and _ERROR_CODE.fullmatch(code):
        return code
    message = str(exc)
    if _ERROR_CODE.fullmatch(message):
        return message
    return "RELEASE_EVALUATION_INTERNAL_ERROR"


__all__ = [
    "ClaimedReleaseJob",
    "ReleaseEvaluationWorker",
    "ReleaseEvaluatorRuntime",
    "build_release_evaluator_runtime",
    "claim_release_jobs",
    "context_for",
    "reclaim_stale_release_jobs",
]
