"""Knowledge gap queue API (ticket 39, docs/development-plan.md Phase 4).

    GET  /v1/knowledge/gaps                    the queue, most-demanded first
    GET  /v1/knowledge/gaps/stats               counts for the queue header
    GET  /v1/knowledge/gaps/drafts              drafts awaiting review
    POST /v1/knowledge/gaps/{id}/acknowledge    claim a gap
    POST /v1/knowledge/gaps/{id}/dismiss        decide not to document it
    POST /v1/knowledge/gaps/{id}/drafts         propose an answer
    POST /v1/knowledge/drafts/{id}/review       approve or reject a draft
    POST /v1/knowledge/drafts/{id}/publish      turn an approved draft into knowledge

Authorization is split the same way as the prompt release API:

- reading the queue requires `KNOWLEDGE_READ` (every support role holds it),
  because reviewers triage the queue and agents should be able to see what is
  already known to be missing;
- everything that changes the queue or touches knowledge requires
  `KNOWLEDGE_PUBLISH` (knowledge_manager and tenant_owner). Acknowledging is a
  write even though it publishes nothing: it is a claim on a shared work item,
  and letting any reader move a gap out from under a reviewer would defeat the
  queue.

`publish` is the only route that creates knowledge. It rejects a draft that is
not approved and a publisher who is also the reviewer (four-eyes), both
enforced in `gap_service` so the HTTP path cannot bypass them.
"""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Body, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from platform_core.api import error_response, require_write_idempotency
from platform_core.config import get_settings
from platform_core.db import session_scope_with_url
from platform_core.identity import tenant_context
from platform_core.identity.tenant_context import TenantContext, apply_rls_tenant
from platform_core.knowledge import gap_service
from platform_core.knowledge.gap_models import GapStatus
from platform_policy import Action, Decision, PolicyEngine, Principal

router = APIRouter(prefix="/v1/knowledge", tags=["knowledge-gaps"])

# Every status is named explicitly so a client cannot ask for an arbitrary
# string and, more importantly, so the wire vocabulary is discoverable.
_STATUS_VALUES = tuple(s.value for s in GapStatus)


class DismissIn(BaseModel):
    reason: str = Field(min_length=1, max_length=500)


class DraftIn(BaseModel):
    title: str = Field(min_length=1, max_length=512)
    body: str = Field(min_length=1)
    target_space_id: str | None = None


class ReviewIn(BaseModel):
    approve: bool
    notes: str = Field(default="", max_length=2000)


class PublishIn(BaseModel):
    space_id: str
    version_label: str = Field(default="v1", min_length=1, max_length=63)


def _principal_from_ctx(ctx: TenantContext) -> Principal:
    return Principal(
        tenant_id=str(ctx.tenant_id),
        actor_id=str(ctx.actor_id) if ctx.actor_id else "",
        role=ctx.role or "unknown",
    )


def _denied(action: str, reason: str) -> JSONResponse:
    """403, not a 200 with an error body (see prompt_router for the why)."""
    return error_response(
        "KNOWLEDGE_ACCESS_DENIED",
        reason or "knowledge access denied",
        status_code=403,
        details={"action": action},
    )


def _gap_error(exc: gap_service.GapError) -> dict[str, Any]:
    return {
        "error": {
            "code": exc.code,
            "reason": exc.detail or exc.code,
            "retryable": False,
        },
        "trace_id": "",
    }


def _ctx_of(request: Request) -> TenantContext:
    ctx = getattr(request.state, "tenant_context", None)
    if ctx is None:
        ctx = tenant_context.get_tenant_context()
    return ctx


def _gate(request: Request, ctx: TenantContext, action: Action, name: str) -> JSONResponse | None:
    """Return a denial envelope, or None when the caller may proceed.

    A write action additionally requires an Idempotency-Key, so every write
    endpoint in this router enforces it through this one gate.
    """
    decision = PolicyEngine().check(_principal_from_ctx(ctx), action)
    if decision.decision != Decision.ALLOW.value:
        return _denied(name, decision.reason_code)
    return require_write_idempotency(request, action)


def _app_url() -> str:
    settings = get_settings()
    return settings.database_url.replace("platform:platform@", "platform_app:platform_app@")


def _uuid(raw: str, what: str) -> uuid.UUID:
    """Parse a path id, mapping malformed input to a gap error.

    A bad id must not reach the database as a cast error: it would surface as
    a 500 and leak that the id was the problem rather than the permission.
    NOT_FOUND matches the not-your-tenant case, so a caller cannot probe for
    the difference.
    """
    try:
        return uuid.UUID(raw)
    except (ValueError, AttributeError, TypeError) as exc:
        raise gap_service.GapError("NOT_FOUND", f"no such {what} for this tenant") from exc


def _gap_out(row: Any) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "sample_question": row.sample_question,
        "reason_code": row.reason_code,
        "status": row.status,
        "frequency": row.frequency,
        "first_seen_at": row.first_seen_at,
        "last_seen_at": row.last_seen_at,
        "acknowledged_at": row.acknowledged_at,
        "target_space_id": str(row.target_space_id) if row.target_space_id else None,
    }


def _draft_out(row: Any) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "gap_id": str(row.gap_id),
        "title": row.title,
        "body": row.body,
        "status": row.status,
        "author_kind": row.author_kind,
        "reviewed_by": str(row.reviewed_by) if row.reviewed_by else None,
        "reviewed_at": row.reviewed_at,
        "review_notes": row.review_notes,
        "published_document_id": (
            str(row.published_document_id) if row.published_document_id else None
        ),
    }


@router.get("/gaps")
async def list_knowledge_gaps(
    request: Request,
    status: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> Any:
    ctx = _ctx_of(request)
    denial = _gate(request, ctx, Action.KNOWLEDGE_READ, "knowledge.read")
    if denial is not None:
        return denial

    # An unrecognized status is refused rather than silently matching
    # nothing: a reviewer filtering on a typo should be told, not shown an
    # empty queue that looks like an all-clear.
    if status is not None and status not in _STATUS_VALUES:
        return _gap_error(
            gap_service.GapError(
                "INVALID_STATUS",
                f"status must be one of: {', '.join(_STATUS_VALUES)}",
            )
        )

    async with session_scope_with_url(_app_url()) as session:
        await apply_rls_tenant(session, ctx)
        rows, total = await gap_service.list_gaps(
            session,
            tenant_id=ctx.tenant_id,
            status=status,
            limit=limit,
            offset=offset,
        )
        return {"items": [_gap_out(r) for r in rows], "total": total}


@router.get("/gaps/stats")
async def get_gap_stats(request: Request) -> Any:
    ctx = _ctx_of(request)
    denial = _gate(request, ctx, Action.KNOWLEDGE_READ, "knowledge.read")
    if denial is not None:
        return denial

    async with session_scope_with_url(_app_url()) as session:
        await apply_rls_tenant(session, ctx)
        return await gap_service.gap_stats(session, tenant_id=ctx.tenant_id)


@router.get("/gaps/drafts")
async def list_knowledge_drafts(
    request: Request,
    status: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
) -> Any:
    ctx = _ctx_of(request)
    denial = _gate(request, ctx, Action.KNOWLEDGE_READ, "knowledge.read")
    if denial is not None:
        return denial

    async with session_scope_with_url(_app_url()) as session:
        await apply_rls_tenant(session, ctx)
        rows = await gap_service.list_drafts(
            session, tenant_id=ctx.tenant_id, status=status, limit=limit
        )
        return {"items": [_draft_out(r) for r in rows], "total": len(rows)}


@router.post("/gaps/{gap_id}/acknowledge")
async def acknowledge_gap(request: Request, gap_id: str) -> Any:
    ctx = _ctx_of(request)
    denial = _gate(request, ctx, Action.KNOWLEDGE_PUBLISH, "knowledge.publish")
    if denial is not None:
        return denial

    async with session_scope_with_url(_app_url()) as session:
        await apply_rls_tenant(session, ctx)
        try:
            row = await gap_service.acknowledge(
                session, ctx=ctx, gap_id=_uuid(gap_id, "knowledge gap")
            )
        except gap_service.GapError as exc:
            return _gap_error(exc)
        await session.commit()
        return _gap_out(row)


@router.post("/gaps/{gap_id}/dismiss")
async def dismiss_gap(
    request: Request,
    gap_id: str,
    payload: Annotated[DismissIn, Body()],
) -> Any:
    ctx = _ctx_of(request)
    denial = _gate(request, ctx, Action.KNOWLEDGE_PUBLISH, "knowledge.publish")
    if denial is not None:
        return denial

    async with session_scope_with_url(_app_url()) as session:
        await apply_rls_tenant(session, ctx)
        try:
            row = await gap_service.dismiss(
                session,
                ctx=ctx,
                gap_id=_uuid(gap_id, "knowledge gap"),
                reason=payload.reason,
            )
        except gap_service.GapError as exc:
            return _gap_error(exc)
        await session.commit()
        return _gap_out(row)


@router.post("/gaps/{gap_id}/drafts")
async def create_gap_draft(
    request: Request,
    gap_id: str,
    payload: Annotated[DraftIn, Body()],
) -> Any:
    ctx = _ctx_of(request)
    denial = _gate(request, ctx, Action.KNOWLEDGE_PUBLISH, "knowledge.publish")
    if denial is not None:
        return denial

    async with session_scope_with_url(_app_url()) as session:
        await apply_rls_tenant(session, ctx)
        try:
            row = await gap_service.create_draft(
                session,
                ctx=ctx,
                gap_id=_uuid(gap_id, "knowledge gap"),
                title=payload.title,
                body=payload.body,
                target_space_id=(
                    _uuid(payload.target_space_id, "knowledge space")
                    if payload.target_space_id
                    else None
                ),
            )
        except gap_service.GapError as exc:
            return _gap_error(exc)
        await session.commit()
        return _draft_out(row)


@router.post("/drafts/{draft_id}/review")
async def review_gap_draft(
    request: Request,
    draft_id: str,
    payload: Annotated[ReviewIn, Body()],
) -> Any:
    ctx = _ctx_of(request)
    denial = _gate(request, ctx, Action.KNOWLEDGE_PUBLISH, "knowledge.publish")
    if denial is not None:
        return denial

    async with session_scope_with_url(_app_url()) as session:
        await apply_rls_tenant(session, ctx)
        try:
            row = await gap_service.review_draft(
                session,
                ctx=ctx,
                draft_id=_uuid(draft_id, "knowledge draft"),
                approve=payload.approve,
                notes=payload.notes,
            )
        except gap_service.GapError as exc:
            return _gap_error(exc)
        await session.commit()
        return _draft_out(row)


@router.post("/drafts/{draft_id}/publish")
async def publish_gap_draft(
    request: Request,
    draft_id: str,
    payload: Annotated[PublishIn, Body()],
) -> Any:
    ctx = _ctx_of(request)
    denial = _gate(request, ctx, Action.KNOWLEDGE_PUBLISH, "knowledge.publish")
    if denial is not None:
        return denial

    async with session_scope_with_url(_app_url()) as session:
        await apply_rls_tenant(session, ctx)
        try:
            version = await gap_service.publish_draft(
                session,
                ctx=ctx,
                draft_id=_uuid(draft_id, "knowledge draft"),
                space_id=_uuid(payload.space_id, "knowledge space"),
                version_label=payload.version_label,
            )
        except gap_service.GapError as exc:
            return _gap_error(exc)
        await session.commit()
        return {
            "document_version_id": str(version.id),
            "document_id": str(version.document_id),
            "status": version.status,
            "version_label": version.version_label,
        }


__all__ = ["router"]
