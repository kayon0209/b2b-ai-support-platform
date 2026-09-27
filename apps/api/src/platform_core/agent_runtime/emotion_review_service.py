"""Append-only persistence for supervisor corrections to emotion advice."""

from __future__ import annotations

import time
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from platform_core.agent_runtime.emotion import Emotion
from platform_core.agent_runtime.emotion_advice_models import EmotionAdviceReview
from platform_core.audit import service as audit_service
from platform_core.identity.tenant_context import TenantContext


class EmotionReviewError(Exception):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(detail or code)
        self.code = code
        self.detail = detail or code


async def replay_correction(
    session: AsyncSession,
    *,
    ctx: TenantContext,
    conversation_ref_id: uuid.UUID,
    advice_id: str,
    corrected_level: Emotion,
    reason_code: str,
    idempotency_key: str,
) -> EmotionAdviceReview | None:
    """Return an exact prior command replay before checking current freshness.

    A retry may arrive after a new customer turn has made the advice stale.
    Returning the row for the same original command is safe because it makes
    no second write; reusing the key for different input is always rejected.
    """
    if ctx.actor_id is None:
        raise EmotionReviewError("ACTOR_REQUIRED", "an identified reviewer is required")
    if not idempotency_key or len(idempotency_key) > 255:
        raise EmotionReviewError("IDEMPOTENCY_KEY_INVALID", "invalid Idempotency-Key")
    existing = (
        await session.execute(
            select(EmotionAdviceReview).where(
                EmotionAdviceReview.tenant_id == ctx.tenant_id,
                EmotionAdviceReview.idempotency_key == idempotency_key,
            )
        )
    ).scalar_one_or_none()
    if existing is None:
        return None
    matches = (
        existing.conversation_ref_id == conversation_ref_id
        and existing.advice_id == advice_id
        and existing.corrected_level == corrected_level.value
        and existing.reason_code == reason_code
        and existing.reviewer_id == ctx.actor_id
    )
    if not matches:
        raise EmotionReviewError(
            "IDEMPOTENCY_CONFLICT", "Idempotency-Key was already used for a different correction"
        )
    return existing


def _same_payload(
    row: EmotionAdviceReview,
    *,
    conversation_ref_id: uuid.UUID,
    advice_id: str,
    suggested_level: Emotion,
    corrected_level: Emotion,
    reason_code: str,
    reviewer_id: uuid.UUID,
) -> bool:
    return (
        row.conversation_ref_id == conversation_ref_id
        and row.advice_id == advice_id
        and row.suggested_level == suggested_level.value
        and row.corrected_level == corrected_level.value
        and row.reason_code == reason_code
        and row.reviewer_id == reviewer_id
    )


async def record_correction(
    session: AsyncSession,
    *,
    ctx: TenantContext,
    conversation_ref_id: uuid.UUID,
    advice_id: str,
    suggested_level: Emotion,
    corrected_level: Emotion,
    reason_code: str,
    idempotency_key: str,
    trace_id: str | None = None,
) -> tuple[EmotionAdviceReview, bool]:
    """Persist a correction once; return `(row, replayed)`.

    The transaction stores only enum values, a reason code, and opaque turn
    revision hash. No customer phrase or evidence text is duplicated.
    """
    if ctx.actor_id is None:
        raise EmotionReviewError("ACTOR_REQUIRED", "an identified reviewer is required")
    if not idempotency_key or len(idempotency_key) > 255:
        raise EmotionReviewError("IDEMPOTENCY_KEY_INVALID", "invalid Idempotency-Key")

    existing = (
        await session.execute(
            select(EmotionAdviceReview).where(
                EmotionAdviceReview.tenant_id == ctx.tenant_id,
                EmotionAdviceReview.idempotency_key == idempotency_key,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        if _same_payload(
            existing,
            conversation_ref_id=conversation_ref_id,
            advice_id=advice_id,
            suggested_level=suggested_level,
            corrected_level=corrected_level,
            reason_code=reason_code,
            reviewer_id=ctx.actor_id,
        ):
            return existing, True
        raise EmotionReviewError(
            "IDEMPOTENCY_CONFLICT", "Idempotency-Key was already used for a different correction"
        )

    values: dict[str, Any] = {
        "id": uuid.uuid4(),
        "tenant_id": ctx.tenant_id,
        "conversation_ref_id": conversation_ref_id,
        "advice_id": advice_id,
        "suggested_level": suggested_level.value,
        "corrected_level": corrected_level.value,
        "reason_code": reason_code,
        "reviewer_id": ctx.actor_id,
        "idempotency_key": idempotency_key,
        "created_at": int(time.time()),
    }
    statement = (
        pg_insert(EmotionAdviceReview)
        .values(**values)
        .on_conflict_do_nothing()
        .returning(EmotionAdviceReview.id)
    )
    created_id = (await session.execute(statement)).scalar_one_or_none()
    if created_id is None:
        # A concurrent correction may have won one of the two uniqueness
        # constraints after the first read. Re-read under RLS to distinguish a
        # replay from a second reviewer action without rewriting the row.
        existing = (
            await session.execute(
                select(EmotionAdviceReview).where(
                    EmotionAdviceReview.tenant_id == ctx.tenant_id,
                    EmotionAdviceReview.idempotency_key == idempotency_key,
                )
            )
        ).scalar_one_or_none()
        if existing is not None and _same_payload(
            existing,
            conversation_ref_id=conversation_ref_id,
            advice_id=advice_id,
            suggested_level=suggested_level,
            corrected_level=corrected_level,
            reason_code=reason_code,
            reviewer_id=ctx.actor_id,
        ):
            return existing, True
        raise EmotionReviewError(
            "EMOTION_ADVICE_ALREADY_REVIEWED",
            "this reviewer already corrected this advice revision",
        )

    row = (
        await session.execute(
            select(EmotionAdviceReview).where(
                EmotionAdviceReview.tenant_id == ctx.tenant_id,
                EmotionAdviceReview.id == created_id,
            )
        )
    ).scalar_one()
    await audit_service.record(
        session,
        ctx=ctx,
        action="emotion_advice.corrected",
        resource_type="conversation",
        resource_id=conversation_ref_id,
        reason_code="EMOTION_ADVICE_CORRECTED",
        metadata={
            "advice_id": advice_id,
            "suggested_level": suggested_level.value,
            "corrected_level": corrected_level.value,
            "correction_reason": reason_code,
        },
        trace_id=trace_id,
    )
    return row, False


__all__ = ["EmotionReviewError", "record_correction", "replay_correction"]
