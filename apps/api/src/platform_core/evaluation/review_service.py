"""Persistence and immutable evidence for stratified online AgentRun reviews."""

from __future__ import annotations

import hashlib
import json
import math
import secrets
import time
import uuid
from collections import Counter
from typing import Any, cast

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from platform_core.agent_runtime.models import AgentRun, PromptTemplate, RunStatus, run_executed
from platform_core.audit import service as audit_service
from platform_core.evaluation.human_review_metrics import (
    ReviewDecision,
    ReviewSelection,
    ReviewVerdict,
    summarize_stratified_review,
)
from platform_core.evaluation.review_models import (
    QualityReviewBatch,
    QualityReviewDecision,
    QualityReviewEvidence,
    QualityReviewItem,
)
from platform_core.evaluation.review_sampling import (
    review_population_counts,
    select_review_sample,
)
from platform_core.identity.tenant_context import TenantContext

MAX_REVIEW_WINDOW_SECONDS = 30 * 24 * 3600
MAX_REVIEW_POPULATION = 20_000
MAX_REVIEW_BATCH_SIZE = 500
MAX_RELEASE_REVIEW_EVIDENCE_AGE_SECONDS = 30 * 24 * 3600
SAMPLER_VERSION = "risk-stratified-v1"
REVIEW_REASON_CODES = frozenset(
    {
        "unsupported_claim",
        "wrong_route",
        "citation_gap",
        "unsafe_action",
        "task_outcome_mismatch",
        "other",
    }
)
_REVIEWABLE_STATUSES = (
    RunStatus.COMPLETED.value,
    RunStatus.ABSTAINED.value,
    RunStatus.HANDED_OFF.value,
    RunStatus.FAILED.value,
)


class QualityReviewError(Exception):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(detail or code)
        self.code = code
        self.detail = detail or code


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _canonical_hash(value: dict[str, Any]) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return _hash(encoded)


async def _lock_review_batch(
    session: AsyncSession, *, tenant_id: uuid.UUID, batch_id: uuid.UUID
) -> None:
    """Serialize review decisions and evidence finalization without row UPDATE grants."""
    digest = _hash(f"quality-review:{tenant_id}:{batch_id}")[:16]
    lock_key = int.from_bytes(bytes.fromhex(digest), byteorder="big", signed=True)
    await session.execute(text("SELECT pg_advisory_xact_lock(:lock_key)"), {"lock_key": lock_key})


def _batch_request_hash(
    *, window_seconds: int, size: int, target_prompt_version_id: uuid.UUID | None
) -> str:
    return _canonical_hash(
        {
            "window_seconds": window_seconds,
            "size": size,
            "target_prompt_version_id": (
                str(target_prompt_version_id) if target_prompt_version_id else None
            ),
        }
    )


def _decision_request_hash(
    *,
    batch_id: uuid.UUID,
    agent_run_id: uuid.UUID,
    reviewer_id: uuid.UUID,
    verdict: str,
    reason_code: str | None,
) -> str:
    return _canonical_hash(
        {
            "agent_run_id": str(agent_run_id),
            "batch_id": str(batch_id),
            "reason_code": reason_code,
            "reviewer_id": str(reviewer_id),
            "verdict": verdict,
        }
    )


async def create_review_batch(
    session: AsyncSession,
    *,
    ctx: TenantContext,
    window_seconds: int,
    size: int,
    target_prompt_version_id: uuid.UUID | None,
    idempotency_key: str,
    trace_id: str | None = None,
) -> tuple[QualityReviewBatch, list[QualityReviewItem], bool]:
    """Select and persist a reproducible tenant-local sample without run text."""
    if ctx.actor_id is None:
        raise QualityReviewError("ACTOR_REQUIRED")
    if not 1 <= window_seconds <= MAX_REVIEW_WINDOW_SECONDS:
        raise QualityReviewError("REVIEW_WINDOW_INVALID")
    if not 1 <= size <= MAX_REVIEW_BATCH_SIZE:
        raise QualityReviewError("REVIEW_BATCH_SIZE_INVALID")
    if not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key) <= 255:
        raise QualityReviewError("IDEMPOTENCY_KEY_INVALID")

    tenant_id = ctx.tenant_id
    key_hash = _hash(idempotency_key)
    request_hash = _batch_request_hash(
        window_seconds=window_seconds,
        size=size,
        target_prompt_version_id=target_prompt_version_id,
    )
    existing = (
        await session.execute(
            select(QualityReviewBatch).where(
                QualityReviewBatch.tenant_id == tenant_id,
                QualityReviewBatch.idempotency_key_hash == key_hash,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        if existing.request_hash != request_hash:
            raise QualityReviewError("IDEMPOTENCY_CONFLICT")
        items = await _items_for_batch(session, tenant_id=tenant_id, batch_id=existing.id)
        return existing, items, True

    now = int(time.time())
    if target_prompt_version_id is not None:
        prompt_exists = (
            await session.execute(
                select(PromptTemplate.id).where(
                    PromptTemplate.tenant_id == tenant_id,
                    PromptTemplate.id == target_prompt_version_id,
                )
            )
        ).scalar_one_or_none()
        if prompt_exists is None:
            raise QualityReviewError("PROMPT_VERSION_NOT_FOUND")

    run_statement = select(AgentRun).where(
        AgentRun.tenant_id == tenant_id,
        AgentRun.started_at.is_not(None),
        AgentRun.started_at >= now - window_seconds,
        AgentRun.status.in_(_REVIEWABLE_STATUSES),
        run_executed(),
    )
    if target_prompt_version_id is not None:
        run_statement = run_statement.where(AgentRun.prompt_version_id == target_prompt_version_id)
    run_statement = run_statement.order_by(AgentRun.started_at, AgentRun.id).limit(
        MAX_REVIEW_POPULATION + 1
    )
    runs_result = await session.execute(run_statement)
    runs = runs_result.scalars().all()
    if len(runs) > MAX_REVIEW_POPULATION:
        raise QualityReviewError("REVIEW_POPULATION_TOO_LARGE")

    run_by_id = {str(run.id): run for run in runs}
    candidates = [
        {"id": key, "status": run.status, "route": run.route, "confidence": None}
        for key, run in run_by_id.items()
    ]
    population = review_population_counts(candidates)
    seed = secrets.token_urlsafe(24)
    samples = select_review_sample(candidates, size=size, seed=seed)
    values = {
        "id": uuid.uuid4(),
        "tenant_id": tenant_id,
        "created_by": ctx.actor_id,
        "idempotency_key_hash": key_hash,
        "request_hash": request_hash,
        "seed": seed,
        "window_seconds": window_seconds,
        "requested_size": size,
        "target_prompt_version_id": target_prompt_version_id,
        "population_by_stratum": population,
        "sampler_version": SAMPLER_VERSION,
        "created_at": now,
    }
    created_id = (
        await session.execute(
            pg_insert(QualityReviewBatch)
            .values(**values)
            .on_conflict_do_nothing()
            .returning(QualityReviewBatch.id)
        )
    ).scalar_one_or_none()
    if created_id is None:
        concurrent = (
            await session.execute(
                select(QualityReviewBatch).where(
                    QualityReviewBatch.tenant_id == tenant_id,
                    QualityReviewBatch.idempotency_key_hash == key_hash,
                )
            )
        ).scalar_one_or_none()
        if concurrent is None or concurrent.request_hash != request_hash:
            raise QualityReviewError("IDEMPOTENCY_CONFLICT")
        return (
            concurrent,
            await _items_for_batch(session, tenant_id=tenant_id, batch_id=concurrent.id),
            True,
        )

    batch = QualityReviewBatch(**values)
    for sample in samples:
        run = run_by_id[sample.run_id]
        session.add(
            QualityReviewItem(
                tenant_id=tenant_id,
                batch_id=created_id,
                agent_run_id=run.id,
                conversation_ref_id=run.conversation_ref_id,
                stratum=sample.stratum,
                route=run.route,
                run_status=run.status,
                prompt_version_id=run.prompt_version_id,
                code_version=run.code_version,
                policy_version=run.policy_version,
                selected_at=now,
            )
        )
    await session.flush()
    await audit_service.record(
        session,
        ctx=ctx,
        action="quality.review.batch.created",
        resource_type="quality_review_batch",
        resource_id=created_id,
        metadata={
            "window_seconds": window_seconds,
            "requested_size": size,
            "selected_count": len(samples),
            "population_by_stratum": population,
            "sampler_version": SAMPLER_VERSION,
        },
        trace_id=trace_id,
    )
    items = await _items_for_batch(session, tenant_id=tenant_id, batch_id=created_id)
    return batch, items, False


async def record_review_decision(
    session: AsyncSession,
    *,
    ctx: TenantContext,
    batch_id: uuid.UUID,
    agent_run_id: uuid.UUID,
    verdict: str,
    reason_code: str | None,
    idempotency_key: str,
    trace_id: str | None = None,
) -> tuple[QualityReviewDecision, bool]:
    if ctx.actor_id is None:
        raise QualityReviewError("ACTOR_REQUIRED")
    if verdict not in {"agree", "override"}:
        raise QualityReviewError("REVIEW_VERDICT_INVALID")
    if verdict == "override" and reason_code not in REVIEW_REASON_CODES:
        raise QualityReviewError("REVIEW_REASON_REQUIRED")
    if verdict == "agree" and reason_code is not None:
        raise QualityReviewError("REVIEW_REASON_INVALID")
    if not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key) <= 255:
        raise QualityReviewError("IDEMPOTENCY_KEY_INVALID")

    tenant_id = ctx.tenant_id
    key_hash = _hash(idempotency_key)
    request_hash = _decision_request_hash(
        batch_id=batch_id,
        agent_run_id=agent_run_id,
        reviewer_id=ctx.actor_id,
        verdict=verdict,
        reason_code=reason_code,
    )
    prior = (
        await session.execute(
            select(QualityReviewDecision).where(
                QualityReviewDecision.tenant_id == tenant_id,
                QualityReviewDecision.idempotency_key_hash == key_hash,
            )
        )
    ).scalar_one_or_none()
    if prior is not None:
        if prior.request_hash != request_hash:
            raise QualityReviewError("IDEMPOTENCY_CONFLICT")
        return prior, True
    await _lock_review_batch(session, tenant_id=tenant_id, batch_id=batch_id)
    batch = await _batch_by_id(session, tenant_id=tenant_id, batch_id=batch_id)
    if batch is None:
        raise QualityReviewError("REVIEW_BATCH_NOT_FOUND")
    finalized = (
        await session.execute(
            select(QualityReviewEvidence.id).where(
                QualityReviewEvidence.tenant_id == tenant_id,
                QualityReviewEvidence.batch_id == batch_id,
            )
        )
    ).scalar_one_or_none()
    if finalized is not None:
        raise QualityReviewError("REVIEW_BATCH_FINALIZED")
    item = (
        await session.execute(
            select(QualityReviewItem.id).where(
                QualityReviewItem.tenant_id == tenant_id,
                QualityReviewItem.batch_id == batch_id,
                QualityReviewItem.agent_run_id == agent_run_id,
            )
        )
    ).scalar_one_or_none()
    if item is None:
        raise QualityReviewError("RUN_NOT_SELECTED")

    values = {
        "id": uuid.uuid4(),
        "tenant_id": tenant_id,
        "batch_id": batch_id,
        "agent_run_id": agent_run_id,
        "reviewer_actor_id": ctx.actor_id,
        "verdict": verdict,
        "reason_code": reason_code,
        "idempotency_key_hash": key_hash,
        "request_hash": request_hash,
        "reviewed_at": int(time.time()),
    }
    created_id = (
        await session.execute(
            pg_insert(QualityReviewDecision)
            .values(**values)
            .on_conflict_do_nothing()
            .returning(QualityReviewDecision.id)
        )
    ).scalar_one_or_none()
    if created_id is None:
        prior = (
            await session.execute(
                select(QualityReviewDecision).where(
                    QualityReviewDecision.tenant_id == tenant_id,
                    QualityReviewDecision.idempotency_key_hash == key_hash,
                )
            )
        ).scalar_one_or_none()
        if prior is not None and prior.request_hash == request_hash:
            return prior, True
        raise QualityReviewError("RUN_ALREADY_REVIEWED")

    decision = QualityReviewDecision(**values)
    await session.flush()
    await audit_service.record(
        session,
        ctx=ctx,
        action="quality.review.decision.recorded",
        resource_type="agent_run",
        resource_id=agent_run_id,
        metadata={"batch_id": str(batch_id), "verdict": verdict, "reason_code": reason_code},
        trace_id=trace_id,
    )
    return decision, False


async def review_batch_summary(
    session: AsyncSession, *, tenant_id: uuid.UUID, batch_id: uuid.UUID
) -> tuple[QualityReviewBatch, list[dict[str, Any]], dict[str, object]]:
    batch = await _batch_by_id(session, tenant_id=tenant_id, batch_id=batch_id)
    if batch is None:
        raise QualityReviewError("REVIEW_BATCH_NOT_FOUND")
    rows = (
        await session.execute(
            select(QualityReviewItem, QualityReviewDecision)
            .outerjoin(
                QualityReviewDecision,
                (QualityReviewDecision.tenant_id == QualityReviewItem.tenant_id)
                & (QualityReviewDecision.batch_id == QualityReviewItem.batch_id)
                & (QualityReviewDecision.agent_run_id == QualityReviewItem.agent_run_id),
            )
            .where(
                QualityReviewItem.tenant_id == tenant_id,
                QualityReviewItem.batch_id == batch_id,
            )
            .order_by(QualityReviewItem.stratum, QualityReviewItem.agent_run_id)
        )
    ).all()
    selections = [
        ReviewSelection(run_id=str(item.agent_run_id), stratum=item.stratum)
        for item, _decision in rows
    ]
    decisions = [
        ReviewDecision(run_id=str(item.agent_run_id), verdict=cast(ReviewVerdict, decision.verdict))
        for item, decision in rows
        if decision is not None
    ]
    summary = summarize_stratified_review(batch.population_by_stratum, selections, decisions)
    items = [
        {
            "agent_run_id": str(item.agent_run_id),
            "conversation_ref_id": str(item.conversation_ref_id),
            "stratum": item.stratum,
            "route": item.route,
            "run_status": item.run_status,
            "prompt_version_id": str(item.prompt_version_id) if item.prompt_version_id else None,
            "code_version": item.code_version,
            "policy_version": item.policy_version,
            "reviewed": decision is not None,
            "verdict": decision.verdict if decision is not None else None,
            "reason_code": decision.reason_code if decision is not None else None,
        }
        for item, decision in rows
    ]
    return batch, items, summary


async def finalize_review_evidence(
    session: AsyncSession,
    *,
    ctx: TenantContext,
    batch_id: uuid.UUID,
    idempotency_key: str,
    trace_id: str | None = None,
) -> tuple[QualityReviewEvidence, bool]:
    if ctx.actor_id is None:
        raise QualityReviewError("ACTOR_REQUIRED")
    if not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key) <= 255:
        raise QualityReviewError("IDEMPOTENCY_KEY_INVALID")
    tenant_id = ctx.tenant_id
    key_hash = _hash(idempotency_key)
    request_hash = _canonical_hash({"batch_id": str(batch_id), "operation": "finalize"})
    existing = (
        await session.execute(
            select(QualityReviewEvidence).where(
                QualityReviewEvidence.tenant_id == tenant_id,
                QualityReviewEvidence.idempotency_key_hash == key_hash,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        if existing.request_hash != request_hash:
            raise QualityReviewError("IDEMPOTENCY_CONFLICT")
        return existing, True
    await _lock_review_batch(session, tenant_id=tenant_id, batch_id=batch_id)
    batch = await _batch_by_id(session, tenant_id=tenant_id, batch_id=batch_id)
    if batch is None:
        raise QualityReviewError("REVIEW_BATCH_NOT_FOUND")
    prior = (
        await session.execute(
            select(QualityReviewEvidence).where(
                QualityReviewEvidence.tenant_id == tenant_id,
                QualityReviewEvidence.batch_id == batch_id,
            )
        )
    ).scalar_one_or_none()
    if prior is not None:
        return prior, True

    _batch, items, summary = await review_batch_summary(
        session, tenant_id=tenant_id, batch_id=batch_id
    )
    if summary["status"] != "measured" or not items:
        raise QualityReviewError("REVIEW_BATCH_INCOMPLETE")
    version_scope = {
        "prompt_version_ids": sorted(
            {item["prompt_version_id"] for item in items if item["prompt_version_id"]}
        ),
        "code_versions": sorted({str(item["code_version"]) for item in items}),
        "policy_versions": sorted({str(item["policy_version"]) for item in items}),
    }
    override_reason_counts = dict(
        sorted(
            Counter(
                cast(str, item["reason_code"])
                for item in items
                if item["verdict"] == "override" and item["reason_code"] is not None
            ).items()
        )
    )
    snapshot: dict[str, Any] = {
        "schema_version": 2,
        "artifact_type": "stratified_agent_run_human_review",
        "batch_id": str(batch.id),
        "target_prompt_version_id": (
            str(batch.target_prompt_version_id) if batch.target_prompt_version_id else None
        ),
        "created_by": str(batch.created_by),
        "sampler_version": batch.sampler_version,
        "window_seconds": batch.window_seconds,
        "requested_size": batch.requested_size,
        "population_by_stratum": batch.population_by_stratum,
        "versions": version_scope,
        "summary": summary,
        "override_reason_counts": override_reason_counts,
        "selected_item_count": len(items),
        "provenance": "tenant-scoped sampled AgentRun decisions; no prompts or answers stored",
    }
    evidence_hash = _canonical_hash(snapshot)
    values = {
        "id": uuid.uuid4(),
        "tenant_id": tenant_id,
        "batch_id": batch.id,
        "evidence_hash": evidence_hash,
        "idempotency_key_hash": key_hash,
        "request_hash": request_hash,
        "snapshot": snapshot,
        "created_by": ctx.actor_id,
        "created_at": int(time.time()),
    }
    created_id = (
        await session.execute(
            pg_insert(QualityReviewEvidence)
            .values(**values)
            .on_conflict_do_nothing()
            .returning(QualityReviewEvidence.id)
        )
    ).scalar_one_or_none()
    if created_id is None:
        prior = (
            await session.execute(
                select(QualityReviewEvidence).where(
                    QualityReviewEvidence.tenant_id == tenant_id,
                    QualityReviewEvidence.batch_id == batch.id,
                )
            )
        ).scalar_one_or_none()
        if prior is not None:
            return prior, True
        raise QualityReviewError("IDEMPOTENCY_CONFLICT")
    evidence = QualityReviewEvidence(**values)
    await session.flush()
    await audit_service.record(
        session,
        ctx=ctx,
        action="quality.review.evidence.finalized",
        resource_type="quality_review_evidence",
        resource_id=evidence.id,
        after={"evidence_hash": evidence_hash, "batch_id": str(batch.id)},
        metadata={
            "weighted_override_rate": summary["weighted_override_rate"],
            "reviewed_count": summary["reviewed_count"],
            "population_count": summary["population_count"],
        },
        trace_id=trace_id,
    )
    return evidence, False


async def verified_review_evidence_for_prompt(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    prompt_version_id: uuid.UUID,
) -> dict[str, Any] | None:
    """Return recent, complete, hash-verified evidence for one prompt candidate."""
    now = int(time.time())
    rows = (
        (
            await session.execute(
                select(QualityReviewEvidence)
                .join(
                    QualityReviewBatch,
                    (QualityReviewBatch.tenant_id == QualityReviewEvidence.tenant_id)
                    & (QualityReviewBatch.id == QualityReviewEvidence.batch_id),
                )
                .where(
                    QualityReviewEvidence.tenant_id == tenant_id,
                    QualityReviewEvidence.created_at
                    >= now - MAX_RELEASE_REVIEW_EVIDENCE_AGE_SECONDS,
                    QualityReviewBatch.target_prompt_version_id == prompt_version_id,
                )
                .order_by(QualityReviewEvidence.created_at.desc(), QualityReviewEvidence.id.desc())
            )
        )
        .scalars()
        .all()
    )
    for row in rows:
        if (
            type(row.created_at) is not int
            or row.created_at < now - MAX_RELEASE_REVIEW_EVIDENCE_AGE_SECONDS
            or row.created_at > now + 60
        ):
            continue
        if _canonical_hash(row.snapshot) != row.evidence_hash:
            continue
        if row.snapshot.get("schema_version") != 2:
            continue
        if row.snapshot.get("artifact_type") != "stratified_agent_run_human_review":
            continue
        if row.snapshot.get("target_prompt_version_id") != str(prompt_version_id):
            continue
        versions = row.snapshot.get("versions")
        if not isinstance(versions, dict):
            continue
        if versions.get("prompt_version_ids") != [str(prompt_version_id)]:
            continue
        summary = row.snapshot.get("summary")
        if not isinstance(summary, dict) or summary.get("status") != "measured":
            continue
        population_count = summary.get("population_count")
        selected_count = summary.get("selected_count")
        reviewed_count = summary.get("reviewed_count")
        weighted_override_rate = summary.get("weighted_override_rate")
        if (
            type(population_count) is not int
            or population_count <= 0
            or type(selected_count) is not int
            or selected_count <= 0
            or type(reviewed_count) is not int
            or reviewed_count != selected_count
            or row.snapshot.get("selected_item_count") != selected_count
            or not isinstance(weighted_override_rate, (int, float))
            or isinstance(weighted_override_rate, bool)
            or not math.isfinite(float(weighted_override_rate))
            or not 0.0 <= float(weighted_override_rate) <= 1.0
        ):
            continue
        window_seconds = row.snapshot.get("window_seconds")
        if (
            type(window_seconds) is not int
            or not 1 <= window_seconds <= MAX_REVIEW_WINDOW_SECONDS
            or row.snapshot.get("sampler_version") != SAMPLER_VERSION
        ):
            continue
        by_stratum = summary.get("by_stratum")
        if not isinstance(by_stratum, dict) or not by_stratum:
            continue
        if any(
            not isinstance(values, dict)
            or type(values.get("population")) is not int
            or type(values.get("selected")) is not int
            or type(values.get("reviewed")) is not int
            or type(values.get("override_count")) is not int
            or values["population"] < 0
            or values["selected"] < 0
            or values["reviewed"] != values["selected"]
            or values["override_count"] < 0
            or values["override_count"] > values["reviewed"]
            for values in by_stratum.values()
        ):
            continue
        if sum(values["population"] for values in by_stratum.values()) != population_count:
            continue
        if sum(values["selected"] for values in by_stratum.values()) != selected_count:
            continue
        override_count = sum(values["override_count"] for values in by_stratum.values())
        reason_counts = row.snapshot.get("override_reason_counts")
        if (
            not isinstance(reason_counts, dict)
            or any(
                reason not in REVIEW_REASON_CODES or type(count) is not int or count < 0
                for reason, count in reason_counts.items()
            )
            or sum(reason_counts.values()) != override_count
        ):
            continue
        return {
            "evidence_id": str(row.id),
            "evidence_hash": row.evidence_hash,
            "target_prompt_version_id": str(prompt_version_id),
            "summary": summary,
            "override_reason_counts": reason_counts,
            "versions": versions,
            "window_seconds": window_seconds,
            "created_at": int(row.created_at),
        }
    return None


async def _items_for_batch(
    session: AsyncSession, *, tenant_id: uuid.UUID, batch_id: uuid.UUID
) -> list[QualityReviewItem]:
    return list(
        (
            await session.execute(
                select(QualityReviewItem)
                .where(
                    QualityReviewItem.tenant_id == tenant_id,
                    QualityReviewItem.batch_id == batch_id,
                )
                .order_by(QualityReviewItem.stratum, QualityReviewItem.agent_run_id)
            )
        )
        .scalars()
        .all()
    )


async def _batch_by_id(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    batch_id: uuid.UUID,
) -> QualityReviewBatch | None:
    statement = select(QualityReviewBatch).where(
        QualityReviewBatch.tenant_id == tenant_id,
        QualityReviewBatch.id == batch_id,
    )
    return (await session.execute(statement)).scalar_one_or_none()


__all__ = [
    "MAX_REVIEW_BATCH_SIZE",
    "MAX_REVIEW_POPULATION",
    "MAX_REVIEW_WINDOW_SECONDS",
    "MAX_RELEASE_REVIEW_EVIDENCE_AGE_SECONDS",
    "REVIEW_REASON_CODES",
    "QualityReviewError",
    "create_review_batch",
    "finalize_review_evidence",
    "record_review_decision",
    "review_batch_summary",
    "verified_review_evidence_for_prompt",
]
