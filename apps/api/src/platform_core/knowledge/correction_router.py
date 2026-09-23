"""Answer corrections (feature list 7.8).

    POST   /v1/corrections                 an agent records a wrong answer
    GET    /v1/corrections                 the review queue
    POST   /v1/corrections/{id}/review     approve or dismiss one

Why recording needs only `CASE_UPDATE`: flagging "this answer was wrong" is a
working agent's judgement, and gating it behind an admin action would mean the
people who actually see the bad answers cannot record them - the correction
would live in a chat message instead, and be lost.

Why reviewing needs `KNOWLEDGE_PUBLISH`: approving is saying "this may become
what the platform tells customers". That is the same blast radius as publishing
a document, and one person's correction is not two people's agreement.

Nothing here applies itself. An approved correction is an instruction to write
knowledge, not knowledge - publishing remains its own reviewed act.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from platform_core.api import (
    error_response,
    get_context,
    new_trace_id,
    ok_response,
    require_policy,
    tenant_session,
)
from platform_core.knowledge import corrections as service
from platform_core.knowledge.correction_models import CorrectionStatus
from platform_core.knowledge.corrections import CorrectionError
from platform_policy import Action

router = APIRouter(prefix="/v1/corrections", tags=["corrections"])


class CorrectionIn(BaseModel):
    agent_run_id: uuid.UUID
    question: str = Field(min_length=1, max_length=2000)
    correct_answer: str = Field(min_length=1, max_length=8000)
    note: str | None = Field(default=None, max_length=2000)


class ReviewIn(BaseModel):
    approve: bool


def _serialize(row: object) -> dict[str, object]:
    return {
        "id": str(row.id),  # type: ignore[attr-defined]
        "agent_run_id": str(row.agent_run_id),  # type: ignore[attr-defined]
        "question": row.question,  # type: ignore[attr-defined]
        "correct_answer": row.correct_answer,  # type: ignore[attr-defined]
        "note": row.note,  # type: ignore[attr-defined]
        "status": row.status,  # type: ignore[attr-defined]
        "created_by": row.created_by,  # type: ignore[attr-defined]
        "created_at": row.created_at,  # type: ignore[attr-defined]
        "reviewed_by": row.reviewed_by,  # type: ignore[attr-defined]
        "reviewed_at": row.reviewed_at,  # type: ignore[attr-defined]
    }


def _unresolved() -> object:
    return error_response("AUTH_UNRESOLVED", "tenant context not resolved", status_code=401)


@router.post("")
async def create_correction(request: Request, body: CorrectionIn) -> object:
    ctx = get_context(request)
    if ctx is None:
        return _unresolved()
    denied = require_policy(ctx, Action.CASE_UPDATE)
    if denied is not None:
        return denied

    async with tenant_session(ctx) as session:
        try:
            row = await service.record_correction(
                session,
                ctx=ctx,
                agent_run_id=body.agent_run_id,
                question=body.question,
                correct_answer=body.correct_answer,
                note=body.note,
            )
        except CorrectionError as exc:
            return error_response(exc.code, exc.detail, status_code=400)
    return ok_response(_serialize(row), trace_id=new_trace_id())


@router.get("")
async def list_corrections(request: Request, status: str | None = None, limit: int = 50) -> object:
    ctx = get_context(request)
    if ctx is None:
        return _unresolved()
    denied = require_policy(ctx, Action.CASE_READ)
    if denied is not None:
        return denied

    async with tenant_session(ctx) as session:
        rows = await service.list_corrections(
            session, ctx=ctx, status=status, limit=max(1, min(limit, 200))
        )
    return ok_response(
        {"items": [_serialize(r) for r in rows], "status_filter": status},
        trace_id=new_trace_id(),
    )


@router.post("/{correction_id}/review")
async def review_correction(request: Request, correction_id: uuid.UUID, body: ReviewIn) -> object:
    ctx = get_context(request)
    if ctx is None:
        return _unresolved()
    denied = require_policy(ctx, Action.KNOWLEDGE_PUBLISH)
    if denied is not None:
        return denied

    async with tenant_session(ctx) as session:
        try:
            row = await service.review_correction(
                session, ctx=ctx, correction_id=correction_id, approve=body.approve
            )
        except CorrectionError as exc:
            code = 404 if exc.code == "NOT_FOUND" else 409
            return error_response(exc.code, exc.detail, status_code=code)
    return ok_response(
        {
            **_serialize(row),
            "next": (
                "write this as a document and publish it"
                if row.status == CorrectionStatus.APPROVED.value
                else "no further action"
            ),
        },
        trace_id=new_trace_id(),
    )
