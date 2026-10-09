"""Authenticated online human-review sampling and immutable evidence API."""

from __future__ import annotations

import uuid
from typing import Any, Literal

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from platform_core.api import (
    IDEMPOTENCY_KEY_REQUIRED,
    error_response,
    new_trace_id,
    require_idempotency_key,
    require_policy,
    tenant_session,
)
from platform_core.evaluation.review_service import (
    MAX_REVIEW_BATCH_SIZE,
    MAX_REVIEW_WINDOW_SECONDS,
    QualityReviewError,
    create_review_batch,
    finalize_review_evidence,
    record_review_decision,
    review_batch_summary,
)
from platform_core.identity import tenant_context
from platform_core.identity.tenant_context import TenantContext
from platform_policy import Action

router = APIRouter(prefix="/v1/quality/reviews", tags=["quality-reviews"])


class ReviewBatchIn(BaseModel):
    window_seconds: int = Field(ge=1, le=MAX_REVIEW_WINDOW_SECONDS)
    size: int = Field(ge=1, le=MAX_REVIEW_BATCH_SIZE)
    target_prompt_version_id: uuid.UUID | None = None


class ReviewDecisionIn(BaseModel):
    agent_run_id: uuid.UUID
    verdict: Literal["agree", "override"]
    reason_code: (
        Literal[
            "unsupported_claim",
            "wrong_route",
            "citation_gap",
            "unsafe_action",
            "task_outcome_mismatch",
            "other",
        ]
        | None
    ) = None


def _authorize(request: Request) -> TenantContext | JSONResponse:
    ctx = tenant_context.get_tenant_context()
    if ctx is None:
        return error_response("AUTH_UNRESOLVED", "tenant context not resolved", status_code=401)

    denied = require_policy(ctx, Action.CASE_REVIEW)
    if denied is not None:
        return denied
    if ctx.actor_id is None:
        return error_response(
            "ACTOR_REQUIRED", "an identified reviewer is required", status_code=403
        )
    return ctx


def _error(exc: QualityReviewError) -> Any:
    status = {
        "REVIEW_BATCH_NOT_FOUND": 404,
        "REVIEW_POPULATION_TOO_LARGE": 413,
        "IDEMPOTENCY_CONFLICT": 409,
        "RUN_ALREADY_REVIEWED": 409,
        "REVIEW_BATCH_FINALIZED": 409,
        "REVIEW_BATCH_INCOMPLETE": 409,
        "RUN_NOT_SELECTED": 409,
    }.get(exc.code, 400)
    return error_response(exc.code, exc.detail, status_code=status, trace_id=new_trace_id())


@router.post("/batches")
async def create_batch(request: Request, body: ReviewBatchIn) -> Any:
    ctx = _authorize(request)
    if isinstance(ctx, JSONResponse):
        return ctx
    idempotency_key = require_idempotency_key(request)
    if not idempotency_key:
        return error_response(
            IDEMPOTENCY_KEY_REQUIRED,
            "creating a human-review batch requires an Idempotency-Key",
            status_code=400,
        )
    try:
        async with tenant_session(ctx) as session:
            batch, items, replayed = await create_review_batch(
                session,
                ctx=ctx,
                window_seconds=body.window_seconds,
                size=body.size,
                target_prompt_version_id=body.target_prompt_version_id,
                idempotency_key=idempotency_key,
                trace_id=getattr(request.state, "trace_id", None),
            )
    except QualityReviewError as exc:
        return _error(exc)
    return {
        "batch_id": str(batch.id),
        "replayed": replayed,
        "window_seconds": batch.window_seconds,
        "requested_size": batch.requested_size,
        "target_prompt_version_id": (
            str(batch.target_prompt_version_id) if batch.target_prompt_version_id else None
        ),
        "selected_count": len(items),
        "population_by_stratum": batch.population_by_stratum,
        "sampler_version": batch.sampler_version,
        "items": [
            {
                "agent_run_id": str(item.agent_run_id),
                "conversation_ref_id": str(item.conversation_ref_id),
                "stratum": item.stratum,
                "route": item.route,
                "run_status": item.run_status,
                "prompt_version_id": (
                    str(item.prompt_version_id) if item.prompt_version_id else None
                ),
                "code_version": item.code_version,
                "policy_version": item.policy_version,
            }
            for item in items
        ],
    }


@router.get("/batches/{batch_id}")
async def get_batch(request: Request, batch_id: uuid.UUID) -> Any:
    ctx = _authorize(request)
    if isinstance(ctx, JSONResponse):
        return ctx
    try:
        async with tenant_session(ctx) as session:
            batch, items, summary = await review_batch_summary(
                session, tenant_id=ctx.tenant_id, batch_id=batch_id
            )
    except QualityReviewError as exc:
        return _error(exc)
    return {
        "batch_id": str(batch.id),
        "window_seconds": batch.window_seconds,
        "requested_size": batch.requested_size,
        "target_prompt_version_id": (
            str(batch.target_prompt_version_id) if batch.target_prompt_version_id else None
        ),
        "population_by_stratum": batch.population_by_stratum,
        "sampler_version": batch.sampler_version,
        "summary": summary,
        "items": items,
    }


@router.post("/batches/{batch_id}/decisions")
async def decide_review(request: Request, batch_id: uuid.UUID, body: ReviewDecisionIn) -> Any:
    ctx = _authorize(request)
    if isinstance(ctx, JSONResponse):
        return ctx
    idempotency_key = require_idempotency_key(request)
    if not idempotency_key:
        return error_response(
            IDEMPOTENCY_KEY_REQUIRED,
            "recording a review decision requires an Idempotency-Key",
            status_code=400,
        )
    try:
        async with tenant_session(ctx) as session:
            decision, replayed = await record_review_decision(
                session,
                ctx=ctx,
                batch_id=batch_id,
                agent_run_id=body.agent_run_id,
                verdict=body.verdict,
                reason_code=body.reason_code,
                idempotency_key=idempotency_key,
                trace_id=getattr(request.state, "trace_id", None),
            )
    except QualityReviewError as exc:
        return _error(exc)
    return {
        "batch_id": str(decision.batch_id),
        "agent_run_id": str(decision.agent_run_id),
        "verdict": decision.verdict,
        "reason_code": decision.reason_code,
        "reviewed_at": decision.reviewed_at,
        "replayed": replayed,
    }


@router.post("/batches/{batch_id}/finalize")
async def finalize_batch(request: Request, batch_id: uuid.UUID) -> Any:
    ctx = _authorize(request)
    if isinstance(ctx, JSONResponse):
        return ctx
    idempotency_key = require_idempotency_key(request)
    if not idempotency_key:
        return error_response(
            IDEMPOTENCY_KEY_REQUIRED,
            "finalizing review evidence requires an Idempotency-Key",
            status_code=400,
        )
    try:
        async with tenant_session(ctx) as session:
            evidence, replayed = await finalize_review_evidence(
                session,
                ctx=ctx,
                batch_id=batch_id,
                idempotency_key=idempotency_key,
                trace_id=getattr(request.state, "trace_id", None),
            )
    except QualityReviewError as exc:
        return _error(exc)
    return {
        "evidence_id": str(evidence.id),
        "batch_id": str(evidence.batch_id),
        "evidence_hash": evidence.evidence_hash,
        "snapshot": evidence.snapshot,
        "replayed": replayed,
    }


__all__ = ["router"]
