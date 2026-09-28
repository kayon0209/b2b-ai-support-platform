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

import hashlib
import json
import time
import uuid
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Body, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import select

from platform_contracts.knowledge_release import KnowledgeEvalRun
from platform_contracts.release_attestation import (
    SignedReleaseEvaluationArtifact,
    SignedReleasePostTestArtifact,
)
from platform_core.api import (
    domain_error_response,
    error_response,
    require_idempotency_key,
    require_write_idempotency,
    tenant_session,
)
from platform_core.audit import service as audit_service
from platform_core.config import get_settings
from platform_core.identity import tenant_context
from platform_core.identity.tenant_context import TenantContext, tenant_repeatable_read_session
from platform_core.knowledge import (
    flag_service,
    gap_service,
    release_artifacts,
    release_evaluator,
    release_service,
)
from platform_core.knowledge.gap_models import GapStatus
from platform_core.knowledge.models import KnowledgeSpace
from platform_core.knowledge.release_attestation_service import release_evidence_available
from platform_core.knowledge.release_signatures import (
    ReleaseSignatureError,
    configured_approved_release_datasets,
    configured_evaluator_public_keys,
)
from platform_core.outbox import OutboxEvent, OutboxStatus
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
    # Which conversation prompted this draft, when the operator is writing it
    # with one open. Optional and never inferred: a draft written from the gap
    # queue has no conversation, and a review that cannot see the customer is
    # the honest state rather than a wrong link.
    conversation_ref_id: str | None = None


class ReviewIn(BaseModel):
    approve: bool
    notes: str = Field(default="", max_length=2000)


class PublishIn(BaseModel):
    space_id: str
    version_label: str = Field(default="v1", min_length=1, max_length=63)
    release_evaluation_id: uuid.UUID | None = None


class InternalReleaseEvaluationIn(BaseModel):
    knowledge_space_id: uuid.UUID
    baseline_run: KnowledgeEvalRun
    candidate_run: KnowledgeEvalRun


class InternalReleaseCandidateIn(BaseModel):
    knowledge_space_id: uuid.UUID


class InternalReleaseEvaluationRequestIn(BaseModel):
    knowledge_space_id: uuid.UUID
    baseline_version_id: uuid.UUID
    candidate_version_id: uuid.UUID


class InternalReleasePostTestIn(BaseModel):
    run: KnowledgeEvalRun


class ReleaseRollbackIn(BaseModel):
    reason_code: Literal["post_test_failed", "manual_quality_issue"]


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


def _gap_error(exc: gap_service.GapError) -> JSONResponse:
    """A refused gap/draft action, as a real 4xx.

    Returned as a JSONResponse rather than a dict: a plain dict is rendered
    with status 200, which the admin UI reads as success (see
    platform_core.api.domain_error_response).
    """
    return domain_error_response(exc.code, exc.detail)


def _release_error(exc: release_service.KnowledgeReleaseError) -> JSONResponse:
    status = 409
    if exc.code.endswith("NOT_FOUND"):
        status = 404
    elif exc.code.endswith("DENIED") or exc.code.endswith("REQUIRED"):
        status = 403
    return error_response(exc.code, exc.detail, status_code=status)


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
        "author_id": str(row.author_id) if row.author_id else None,
        "reviewed_by": str(row.reviewed_by) if row.reviewed_by else None,
        "reviewed_at": row.reviewed_at,
        "review_notes": row.review_notes,
        "published_document_id": (
            str(row.published_document_id) if row.published_document_id else None
        ),
        # Echoed so the console can offer "open the conversation" beside the
        # draft, and so a reviewer can tell "written without one" from "the link
        # is missing" - in the payload those two look identical otherwise.
        "conversation_ref_id": (str(row.conversation_ref_id) if row.conversation_ref_id else None),
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

    async with tenant_session(ctx) as session:
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

    async with tenant_session(ctx) as session:
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

    async with tenant_session(ctx) as session:
        rows = await gap_service.list_drafts(
            session, tenant_id=ctx.tenant_id, status=status, limit=limit
        )
        release_gate = await flag_service.evaluate(
            session,
            flag_key=release_service.FLAG_KNOWLEDGE_RELEASE_GATE,
            tenant_id=ctx.tenant_id,
            default=False,
        )
        return {
            "items": [_draft_out(r) for r in rows],
            "total": len(rows),
            "release_gate_enabled": release_gate.enabled,
            "release_evidence_available": release_evidence_available(ctx.tenant_id),
        }


@router.post("/gaps/{gap_id}/acknowledge")
async def acknowledge_gap(request: Request, gap_id: str) -> Any:
    ctx = _ctx_of(request)
    denial = _gate(request, ctx, Action.KNOWLEDGE_PUBLISH, "knowledge.publish")
    if denial is not None:
        return denial

    async with tenant_session(ctx) as session:
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

    async with tenant_session(ctx) as session:
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

    async with tenant_session(ctx) as session:
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
                conversation_ref_id=(
                    _uuid(payload.conversation_ref_id, "conversation")
                    if payload.conversation_ref_id
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

    async with tenant_session(ctx) as session:
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

    idempotency_key = require_idempotency_key(request)
    if idempotency_key is None:
        return error_response("IDEMPOTENCY_KEY_REQUIRED", status_code=400)

    try:
        async with tenant_repeatable_read_session(ctx) as session:
            decision = await flag_service.evaluate(
                session,
                flag_key=release_service.FLAG_KNOWLEDGE_RELEASE_GATE,
                tenant_id=ctx.tenant_id,
                default=False,
            )
            post_approval = None
            settings = get_settings()
            if (
                decision.enabled
                and payload.release_evaluation_id is not None
                and settings.knowledge_evaluator_auto_run
                and settings.knowledge_evaluator_max_cases_per_run > 0
            ):
                try:
                    post_approval = configured_approved_release_datasets().get(
                        (ctx.tenant_id, _uuid(payload.space_id, "knowledge space"))
                    )
                    public_keys = configured_evaluator_public_keys()
                except ReleaseSignatureError as exc:
                    return error_response(exc.code, status_code=409)
                if post_approval is None or not public_keys:
                    return error_response("EVALUATOR_PROVENANCE_UNAVAILABLE", status_code=409)
                try:
                    post_dataset = release_evaluator.load_approved_release_dataset(
                        tenant_id=ctx.tenant_id,
                        knowledge_space_id=_uuid(payload.space_id, "knowledge space"),
                    )
                except release_evaluator.ReleaseEvaluationError as exc:
                    return error_response(exc.code, exc.detail, status_code=409)
                if len(post_dataset.cases) > settings.knowledge_evaluator_max_cases_per_run:
                    return error_response("EVAL_CASE_BUDGET_EXCEEDED", status_code=409)
            version = await gap_service.publish_draft(
                session,
                ctx=ctx,
                draft_id=_uuid(draft_id, "knowledge draft"),
                space_id=_uuid(payload.space_id, "knowledge space"),
                version_label=payload.version_label,
                release_gate_enabled=decision.enabled,
                release_evaluation_id=payload.release_evaluation_id,
                idempotency_key=idempotency_key,
            )
            if post_approval is not None and payload.release_evaluation_id is not None:
                post_payload = {
                    "schema_version": 1,
                    "evaluation_id": str(payload.release_evaluation_id),
                    "dataset_sha256": post_approval.sha256,
                    "dataset_approval_ref": post_approval.approval_ref,
                }
                post_hash = hashlib.sha256(
                    json.dumps(post_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
                ).hexdigest()
                event_id = uuid.uuid5(
                    ctx.tenant_id,
                    f"knowledge-release-post-test:{payload.release_evaluation_id}",
                )
                existing = (
                    await session.execute(
                        select(OutboxEvent).where(
                            OutboxEvent.tenant_id == ctx.tenant_id,
                            OutboxEvent.event_id == event_id,
                        )
                    )
                ).scalar_one_or_none()
                if existing is not None:
                    if (existing.payload or {}).get("request_hash") != post_hash:
                        return error_response("IDEMPOTENCY_CONFLICT", status_code=409)
                else:
                    session.add(
                        OutboxEvent(
                            id=uuid.uuid4(),
                            tenant_id=ctx.tenant_id,
                            event_id=event_id,
                            event_type=release_evaluator.RELEASE_POST_TEST_REQUEST_EVENT,
                            event_version=1,
                            aggregate_type="knowledge_release_evaluation",
                            aggregate_id=str(payload.release_evaluation_id),
                            payload={**post_payload, "request_hash": post_hash},
                            status=OutboxStatus.QUEUED.value,
                            created_at=int(time.time()),
                            trace_id=getattr(request.state, "trace_id", "") or "",
                        )
                    )
                    await audit_service.record(
                        session,
                        ctx=ctx,
                        action="knowledge.release_post_test_requested",
                        resource_type="knowledge_release_evaluation",
                        resource_id=payload.release_evaluation_id,
                        metadata={
                            "event_id": str(event_id),
                            "dataset_sha256": post_approval.sha256,
                        },
                    )
    except gap_service.GapError as exc:
        return _gap_error(exc)
    except release_service.KnowledgeReleaseError as exc:
        return _release_error(exc)
    return {
        "document_version_id": str(version.id),
        "document_id": str(version.document_id),
        "status": version.status,
        "version_label": version.version_label,
    }


@router.get("/drafts/{draft_id}/release-evaluations")
async def list_draft_release_evaluations(request: Request, draft_id: str) -> Any:
    ctx = _ctx_of(request)
    denial = _gate(request, ctx, Action.KNOWLEDGE_READ, "knowledge.read")
    if denial is not None:
        return denial
    assert ctx is not None
    async with tenant_session(ctx) as session:
        try:
            draft_uuid = _uuid(draft_id, "knowledge draft")
            items = await release_service.list_release_evaluations(
                session,
                tenant_id=ctx.tenant_id,
                draft_id=draft_uuid,
                reviewer_id=ctx.actor_id,
            )
            decision = await flag_service.evaluate(
                session,
                flag_key=release_service.FLAG_KNOWLEDGE_RELEASE_GATE,
                tenant_id=ctx.tenant_id,
                default=False,
            )
        except gap_service.GapError as exc:
            return _gap_error(exc)
        return {
            "items": items,
            "release_gate_enabled": decision.enabled,
            "release_evidence_available": release_evidence_available(ctx.tenant_id),
            "can_approve": ctx.role in {"knowledge_manager", "tenant_owner"}
            and ctx.actor_id is not None,
        }


@router.post("/drafts/{draft_id}/release-evaluations/{evaluation_id}/approve")
async def approve_knowledge_release(request: Request, draft_id: str, evaluation_id: str) -> Any:
    ctx = _ctx_of(request)
    denial = _gate(request, ctx, Action.KNOWLEDGE_PUBLISH, "knowledge.publish")
    if denial is not None:
        return denial
    assert ctx is not None
    if ctx.role not in {"knowledge_manager", "tenant_owner"}:
        return _denied("knowledge.release.approve", "reviewer role is not permitted")
    idempotency_key = require_idempotency_key(request)
    if idempotency_key is None:
        return error_response("IDEMPOTENCY_KEY_REQUIRED", status_code=400)
    try:
        draft_uuid = _uuid(draft_id, "knowledge draft")
        evaluation_uuid = _uuid(evaluation_id, "knowledge release evaluation")
    except gap_service.GapError as exc:
        return _gap_error(exc)

    async with tenant_session(ctx) as session:
        enabled = await flag_service.evaluate(
            session,
            flag_key=release_service.FLAG_KNOWLEDGE_RELEASE_GATE,
            tenant_id=ctx.tenant_id,
            default=False,
        )
        if not enabled.enabled:
            return error_response(
                "FEATURE_DISABLED", "knowledge release gate is not enabled", status_code=409
            )
        try:
            approval, replayed = await release_service.approve_evaluation(
                session,
                ctx=ctx,
                evaluation_id=evaluation_uuid,
                draft_id=draft_uuid,
                idempotency_key=idempotency_key,
            )
        except release_service.KnowledgeReleaseError as exc:
            return _release_error(exc)
        await session.commit()
    return {
        "approval_id": str(approval.id),
        "evaluation_id": str(approval.evaluation_id),
        "candidate_fingerprint": approval.candidate_fingerprint,
        "replayed": replayed,
    }


@router.post("/internal/drafts/{draft_id}/release-candidates")
async def prepare_internal_knowledge_release_candidate(
    request: Request,
    draft_id: str,
    payload: Annotated[InternalReleaseCandidateIn, Body()],
) -> Any:
    """Stage approved content as non-retrievable input for the release evaluator."""
    ctx = _ctx_of(request)
    if (
        ctx.actor_kind not in {"system", "service"}
        or ctx.actor_id is None
        or ctx.role != "integration_service"
    ):
        return error_response("EVALUATOR_SERVICE_REQUIRED", status_code=403)
    idempotency_key = require_idempotency_key(request)
    if idempotency_key is None:
        return error_response("IDEMPOTENCY_KEY_REQUIRED", status_code=400)
    try:
        draft_uuid = _uuid(draft_id, "knowledge draft")
    except gap_service.GapError as exc:
        return _gap_error(exc)

    async with tenant_session(ctx) as session:
        enabled = await flag_service.evaluate(
            session,
            flag_key=release_service.FLAG_KNOWLEDGE_RELEASE_GATE,
            tenant_id=ctx.tenant_id,
            default=False,
        )
        if not enabled.enabled:
            return error_response(
                "FEATURE_DISABLED", "knowledge release gate is not enabled", status_code=409
            )
        try:
            version, replayed = await gap_service.prepare_release_candidate(
                session,
                ctx=ctx,
                draft_id=draft_uuid,
                space_id=payload.knowledge_space_id,
                idempotency_key=idempotency_key,
            )
        except gap_service.GapError as exc:
            return _gap_error(exc)
        await session.commit()
    return {
        "candidate_version_id": str(version.id),
        "knowledge_space_id": str(payload.knowledge_space_id),
        "status": version.status,
        "ingestion_status": version.ingestion_status,
        "replayed": replayed,
    }


@router.post("/internal/drafts/{draft_id}/release-evaluation-requests")
async def request_internal_knowledge_release_evaluation(
    request: Request,
    draft_id: str,
    payload: Annotated[InternalReleaseEvaluationRequestIn, Body()],
) -> Any:
    """Durably request a signed evaluation; the request contains identifiers only."""
    ctx = _ctx_of(request)
    if (
        ctx.actor_kind not in {"system", "service"}
        or ctx.actor_id is None
        or ctx.role != "integration_service"
    ):
        return error_response("EVALUATOR_SERVICE_REQUIRED", status_code=403)
    idempotency_key = require_idempotency_key(request)
    if idempotency_key is None:
        return error_response("IDEMPOTENCY_KEY_REQUIRED", status_code=400)
    try:
        draft_uuid = _uuid(draft_id, "knowledge draft")
    except gap_service.GapError as exc:
        return _gap_error(exc)

    key_hash = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()
    async with tenant_session(ctx) as session:
        enabled = await flag_service.evaluate(
            session,
            flag_key=release_service.FLAG_KNOWLEDGE_RELEASE_GATE,
            tenant_id=ctx.tenant_id,
            default=False,
        )
        if not enabled.enabled:
            return error_response(
                "FEATURE_DISABLED", "knowledge release gate is not enabled", status_code=409
            )
        settings = get_settings()
        if (
            not settings.knowledge_evaluator_auto_run
            or settings.knowledge_evaluator_max_cases_per_run <= 0
        ):
            return error_response("EVALUATOR_AUTORUN_DISABLED", status_code=409)
        try:
            approval = configured_approved_release_datasets().get(
                (ctx.tenant_id, payload.knowledge_space_id)
            )
            public_keys = configured_evaluator_public_keys()
        except ReleaseSignatureError as exc:
            return error_response(exc.code, status_code=409)
        if approval is None or not public_keys:
            return error_response("EVALUATOR_PROVENANCE_UNAVAILABLE", status_code=409)
        try:
            approved_dataset = release_evaluator.load_approved_release_dataset(
                tenant_id=ctx.tenant_id,
                knowledge_space_id=payload.knowledge_space_id,
            )
        except release_evaluator.ReleaseEvaluationError as exc:
            return error_response(exc.code, exc.detail, status_code=409)
        if len(approved_dataset.cases) > settings.knowledge_evaluator_max_cases_per_run:
            return error_response("EVAL_CASE_BUDGET_EXCEEDED", status_code=409)
        try:
            draft = await gap_service._load_draft(
                session, ctx=ctx, draft_id=draft_uuid, for_update=True
            )
            if draft.status != "approved":
                return _gap_error(
                    gap_service.GapError(
                        "DRAFT_NOT_APPROVED", "only an approved draft can be evaluated"
                    )
                )
            space = (
                await session.execute(
                    select(KnowledgeSpace).where(
                        KnowledgeSpace.tenant_id == ctx.tenant_id,
                        KnowledgeSpace.id == payload.knowledge_space_id,
                        KnowledgeSpace.status == "active",
                    )
                )
            ).scalar_one_or_none()
            if space is None:
                return _gap_error(
                    gap_service.GapError("KNOWLEDGE_SPACE_NOT_FOUND", "no active tenant space")
                )
            candidate = await release_evaluator._load_candidate_version(
                session,
                tenant_id=ctx.tenant_id,
                knowledge_space_id=payload.knowledge_space_id,
                version_id=payload.candidate_version_id,
            )
            baseline = await release_evaluator._load_baseline_version(
                session,
                tenant_id=ctx.tenant_id,
                knowledge_space_id=payload.knowledge_space_id,
                version_id=payload.baseline_version_id,
            )
        except (gap_service.GapError, release_evaluator.ReleaseEvaluationError) as exc:
            if isinstance(exc, gap_service.GapError):
                return _gap_error(exc)
            return error_response(exc.code, exc.detail, status_code=409)
        expected_candidate_id = uuid.uuid5(
            ctx.tenant_id,
            f"knowledge-release-candidate:{draft.id}:{payload.knowledge_space_id}:"
            f"{release_service.draft_content_sha256(draft)}",
        )
        if (
            candidate.id != expected_candidate_id
            or baseline.id == candidate.id
            or payload.candidate_version_id != candidate.id
        ):
            return error_response("CANDIDATE_VERSION_MISMATCH", status_code=409)

        request_payload = {
            "schema_version": 1,
            "draft_id": str(draft.id),
            "knowledge_space_id": str(payload.knowledge_space_id),
            "baseline_version_id": str(baseline.id),
            "candidate_version_id": str(candidate.id),
            "dataset_sha256": approval.sha256,
            "dataset_approval_ref": approval.approval_ref,
            "idempotency_key_sha256": key_hash,
        }
        request_hash = hashlib.sha256(
            json.dumps(request_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        event_id = uuid.uuid5(ctx.tenant_id, f"knowledge-release-evaluation:{key_hash}")
        existing = (
            await session.execute(
                select(OutboxEvent).where(
                    OutboxEvent.tenant_id == ctx.tenant_id,
                    OutboxEvent.event_id == event_id,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            if (existing.payload or {}).get("request_hash") != request_hash:
                return error_response("IDEMPOTENCY_CONFLICT", status_code=409)
            return {
                "event_id": str(existing.event_id),
                "status": str(existing.status),
                "replayed": True,
            }
        event = OutboxEvent(
            id=uuid.uuid4(),
            tenant_id=ctx.tenant_id,
            event_id=event_id,
            event_type=release_evaluator.RELEASE_EVALUATION_REQUEST_EVENT,
            event_version=1,
            aggregate_type="knowledge_draft",
            aggregate_id=str(draft.id),
            payload={**request_payload, "request_hash": request_hash},
            status=OutboxStatus.QUEUED.value,
            created_at=int(time.time()),
            trace_id=getattr(request.state, "trace_id", "") or "",
        )
        session.add(event)
        await audit_service.record(
            session,
            ctx=ctx,
            action="knowledge.release_evaluation_requested",
            resource_type="knowledge_draft",
            resource_id=draft.id,
            metadata={
                "event_id": str(event_id),
                "dataset_sha256": approval.sha256,
                "candidate_version_id": str(candidate.id),
            },
        )
        await session.commit()
    return {"event_id": str(event_id), "status": OutboxStatus.QUEUED.value, "replayed": False}


@router.post("/internal/drafts/{draft_id}/release-evaluations")
async def record_internal_knowledge_evaluation(
    request: Request,
    draft_id: str,
    payload: Annotated[InternalReleaseEvaluationIn, Body()],
) -> Any:
    ctx = _ctx_of(request)
    if ctx.actor_kind not in {"system", "service"}:
        return error_response("EVALUATOR_SERVICE_REQUIRED", status_code=403)
    idempotency_key = require_idempotency_key(request)
    if idempotency_key is None:
        return error_response("IDEMPOTENCY_KEY_REQUIRED", status_code=400)
    try:
        draft_uuid = _uuid(draft_id, "knowledge draft")
    except gap_service.GapError as exc:
        return _gap_error(exc)
    async with tenant_session(ctx) as session:
        enabled = await flag_service.evaluate(
            session,
            flag_key=release_service.FLAG_KNOWLEDGE_RELEASE_GATE,
            tenant_id=ctx.tenant_id,
            default=False,
        )
        if not enabled.enabled:
            return error_response(
                "FEATURE_DISABLED", "knowledge release gate is not enabled", status_code=409
            )
        try:
            row, replayed = await release_service.record_evaluation(
                session,
                ctx=ctx,
                draft_id=draft_uuid,
                knowledge_space_id=payload.knowledge_space_id,
                baseline_run=payload.baseline_run,
                candidate_run=payload.candidate_run,
                idempotency_key=idempotency_key,
            )
        except release_service.KnowledgeReleaseError as exc:
            return _release_error(exc)
        await session.commit()
    return {
        "evaluation_id": str(row.id),
        "candidate_version_id": str(row.candidate_version_id),
        "candidate_fingerprint": row.candidate_fingerprint,
        "status": row.status,
        "reason_code": row.reason_code,
        "replayed": replayed,
    }


@router.post("/internal/drafts/{draft_id}/signed-release-evaluations")
async def record_signed_knowledge_evaluation(
    request: Request,
    draft_id: str,
    payload: Annotated[SignedReleaseEvaluationArtifact, Body()],
) -> Any:
    """Record only a trusted worker signature over an approved fixed-set run."""
    ctx = _ctx_of(request)
    if (
        ctx.actor_kind not in {"system", "service"}
        or ctx.actor_id is None
        or ctx.role != "integration_service"
    ):
        return error_response("EVALUATOR_SERVICE_REQUIRED", status_code=403)
    idempotency_key = require_idempotency_key(request)
    if idempotency_key is None:
        return error_response("IDEMPOTENCY_KEY_REQUIRED", status_code=400)
    try:
        draft_uuid = _uuid(draft_id, "knowledge draft")
    except gap_service.GapError as exc:
        return _gap_error(exc)
    if payload.artifact.draft_id != draft_uuid:
        return error_response("EVAL_DRAFT_MISMATCH", status_code=409)

    try:
        async with tenant_repeatable_read_session(ctx) as session:
            row, replayed = await release_artifacts.persist_signed_release_evaluation(
                session,
                ctx=ctx,
                signed=payload,
                idempotency_key=idempotency_key,
            )
    except release_service.KnowledgeReleaseError as exc:
        return _release_error(exc)
    return {
        "evaluation_id": str(row.id),
        "candidate_version_id": str(row.candidate_version_id),
        "candidate_fingerprint": row.candidate_fingerprint,
        "status": row.status,
        "reason_code": row.reason_code,
        "replayed": replayed,
    }


@router.post("/internal/releases/{evaluation_id}/post-test")
async def record_internal_knowledge_post_test(
    request: Request,
    evaluation_id: str,
    payload: Annotated[InternalReleasePostTestIn, Body()],
) -> Any:
    ctx = _ctx_of(request)
    if ctx.actor_kind not in {"system", "service"}:
        return error_response("EVALUATOR_SERVICE_REQUIRED", status_code=403)
    idempotency_key = require_idempotency_key(request)
    if idempotency_key is None:
        return error_response("IDEMPOTENCY_KEY_REQUIRED", status_code=400)
    try:
        evaluation_uuid = _uuid(evaluation_id, "knowledge release evaluation")
    except gap_service.GapError as exc:
        return _gap_error(exc)
    async with tenant_session(ctx) as session:
        try:
            row, replayed = await release_service.record_post_test(
                session,
                ctx=ctx,
                evaluation_id=evaluation_uuid,
                post_run=payload.run,
                idempotency_key=idempotency_key,
            )
        except release_service.KnowledgeReleaseError as exc:
            return _release_error(exc)
        await session.commit()
    return {
        "post_test_id": str(row.id),
        "evaluation_id": str(row.evaluation_id),
        "status": row.status,
        "reason_code": row.reason_code,
        "replayed": replayed,
    }


@router.post("/internal/releases/{evaluation_id}/signed-post-test")
async def record_signed_knowledge_post_test(
    request: Request,
    evaluation_id: str,
    payload: Annotated[SignedReleasePostTestArtifact, Body()],
) -> Any:
    ctx = _ctx_of(request)
    if (
        ctx.actor_kind not in {"system", "service"}
        or ctx.actor_id is None
        or ctx.role != "integration_service"
    ):
        return error_response("EVALUATOR_SERVICE_REQUIRED", status_code=403)
    idempotency_key = require_idempotency_key(request)
    if idempotency_key is None:
        return error_response("IDEMPOTENCY_KEY_REQUIRED", status_code=400)
    try:
        evaluation_uuid = _uuid(evaluation_id, "knowledge release evaluation")
    except gap_service.GapError as exc:
        return _gap_error(exc)
    if payload.artifact.evaluation_id != evaluation_uuid:
        return error_response("POST_TEST_INPUT_MISMATCH", status_code=409)

    try:
        async with tenant_repeatable_read_session(ctx) as session:
            row, replayed = await release_artifacts.persist_signed_release_post_test(
                session,
                ctx=ctx,
                evaluation_id=evaluation_uuid,
                signed=payload,
                idempotency_key=idempotency_key,
            )
    except release_service.KnowledgeReleaseError as exc:
        return _release_error(exc)
    return {
        "post_test_id": str(row.id),
        "evaluation_id": str(row.evaluation_id),
        "status": row.status,
        "reason_code": row.reason_code,
        "replayed": replayed,
    }


@router.post("/releases/{evaluation_id}/rollback")
async def rollback_knowledge_release(
    request: Request,
    evaluation_id: str,
    payload: Annotated[ReleaseRollbackIn, Body()],
) -> Any:
    ctx = _ctx_of(request)
    denial = _gate(request, ctx, Action.KNOWLEDGE_PUBLISH, "knowledge.publish")
    if denial is not None:
        return denial
    assert ctx is not None
    if ctx.role not in {"knowledge_manager", "tenant_owner"}:
        return _denied("knowledge.release.rollback", "publisher role is not permitted")
    idempotency_key = require_idempotency_key(request)
    if idempotency_key is None:
        return error_response("IDEMPOTENCY_KEY_REQUIRED", status_code=400)
    try:
        evaluation_uuid = _uuid(evaluation_id, "knowledge release evaluation")
    except gap_service.GapError as exc:
        return _gap_error(exc)
    async with tenant_session(ctx) as session:
        try:
            candidate_id, baseline_id, replayed = await release_service.rollback_release(
                session,
                ctx=ctx,
                evaluation_id=evaluation_uuid,
                idempotency_key=idempotency_key,
                reason_code=payload.reason_code,
            )
        except release_service.KnowledgeReleaseError as exc:
            return _release_error(exc)
        await session.commit()
    return {
        "evaluation_id": str(evaluation_uuid),
        "superseded_version_id": str(candidate_id),
        "restored_version_id": str(baseline_id),
        "replayed": replayed,
    }


__all__ = ["router"]
