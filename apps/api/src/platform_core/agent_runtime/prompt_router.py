"""Prompt version release API (ticket 38, docs/development-plan.md Phase 4).

    GET  /v1/prompts                      list versions of a template
    GET  /v1/prompts/active               what is serving traffic now
    POST /v1/prompts                      author a new draft version
    POST /v1/prompts/{id}/candidate       submit a draft for evaluation
    POST /v1/prompts/{id}/promote         activate, gated on evidence
    POST /v1/prompts/{id}/reject          withdraw a candidate
    POST /v1/prompts/rollback             restore a previously active version

Authorization is split deliberately:

- reading prompt versions requires `PROMPT_READ` (auditor, security_admin,
  tenant_owner) - templates are reviewed during incident analysis;
- releasing requires `PROMPT_RELEASE` (tenant_owner only). Promoting a
  prompt rewrites what every customer-visible answer says, so it is a
  production change, not a configuration read.

The release gate itself lives in `agent_runtime.prompt_release`; this router
only translates HTTP to that service. Keeping the gate out of the router is
what lets CI and the release path share one implementation.
"""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Body, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from platform_core.agent_runtime.prompt_release import (
    CategoryScore,
    EvaluationEvidence,
    Regression,
    ReleaseError,
    create_draft,
    get_active,
    list_versions,
    promote,
    reject,
    rollback,
    submit_candidate,
)
from platform_core.api import (
    domain_error_response,
    error_response,
    require_write_idempotency,
    tenant_session,
)
from platform_core.identity import tenant_context
from platform_core.identity.tenant_context import TenantContext
from platform_policy import Action, Decision, PolicyEngine, Principal

router = APIRouter(prefix="/v1/prompts", tags=["prompts"])


class CategoryScoreIn(BaseModel):
    category: str = Field(min_length=1, max_length=63)
    passed: int = Field(ge=0)
    total: int = Field(ge=0)


class RegressionIn(BaseModel):
    category: str = Field(min_length=1, max_length=63)
    baseline_rate: float = Field(ge=0.0, le=1.0)
    candidate_rate: float = Field(ge=0.0, le=1.0)


class EvidenceIn(BaseModel):
    """Evaluation evidence supplied by the caller (typically CI)."""

    eval_run_id: str = Field(min_length=1, max_length=127)
    scores: list[CategoryScoreIn] = Field(default_factory=list)
    regressions: list[RegressionIn] = Field(default_factory=list)


class VersionOut(BaseModel):
    id: str
    template_name: str
    version: int
    published: bool
    body: str


class DraftIn(BaseModel):
    template_name: str = Field(min_length=1, max_length=127)
    body: str = Field(min_length=1)
    notes: str = Field(default="", max_length=500)


class RollbackIn(BaseModel):
    template_name: str = Field(min_length=1, max_length=127)
    to_version_id: str
    reason: str = Field(min_length=1, max_length=500)


class RejectIn(BaseModel):
    reason: str = Field(min_length=1, max_length=500)


def _principal_from_ctx(ctx: TenantContext) -> Principal:
    return Principal(
        tenant_id=str(ctx.tenant_id),
        actor_id=str(ctx.actor_id) if ctx.actor_id else "",
        role=ctx.role or "unknown",
    )


def _denied(action: str, reason: str) -> JSONResponse:
    """403, not a 200 with an error body.

    A denial that arrives with a success status is worse than useless: any
    client that branches on the status code - a retry wrapper, a dashboard,
    an integration test - reads "denied" as "here is your data". The body
    code alone is not a contract when the transport disagrees with it.
    """
    return error_response(
        "PROMPT_ACCESS_DENIED",
        reason or "prompt access denied",
        status_code=403,
        details={"action": action},
    )


def _release_error(exc: ReleaseError) -> JSONResponse:
    """A refused release action, as a real 4xx.

    A promotion refused by the evaluation gate used to come back as a 200
    carrying an error body, so the admin UI showed the operator a green
    "Promoted" banner for a promotion that never happened (see
    platform_core.api.domain_error_response).
    """
    return domain_error_response(exc.code, exc.detail)


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


def _to_evidence(payload: EvidenceIn | None) -> EvaluationEvidence | None:
    if payload is None:
        return None
    return EvaluationEvidence(
        eval_run_id=payload.eval_run_id,
        scores=[
            CategoryScore(category=s.category, passed=s.passed, total=s.total)
            for s in payload.scores
        ],
        regressions=[
            Regression(
                category=r.category,
                baseline_rate=r.baseline_rate,
                candidate_rate=r.candidate_rate,
                p0=False,  # set below; P0 membership is owned by the service
            )
            for r in payload.regressions
        ],
    )


def _mark_p0(evidence: EvaluationEvidence) -> EvaluationEvidence:
    """Tag regressions with P0 membership.

    The service owns `P0_CATEGORIES`; the wire format does not carry the
    flag, so a client cannot declare its own regression non-blocking by
    sending `p0: false`.
    """
    from platform_core.agent_runtime.prompt_release import P0_CATEGORIES

    evidence.regressions = [
        Regression(
            category=r.category,
            baseline_rate=r.baseline_rate,
            candidate_rate=r.candidate_rate,
            p0=r.category in P0_CATEGORIES,
        )
        for r in evidence.regressions
    ]
    return evidence


def _version_out(row: Any) -> dict[str, Any]:
    return VersionOut(
        id=str(row.id),
        template_name=row.template_name,
        version=row.version,
        published=bool(row.published),
        body=row.body,
    ).model_dump()


@router.get("")
async def list_prompt_versions(
    request: Request,
    template_name: str = Query(..., min_length=1, max_length=127),
) -> Any:
    ctx = _ctx_of(request)
    denial = _gate(request, ctx, Action.PROMPT_READ, "prompt.read")
    if denial is not None:
        return denial

    async with tenant_session(ctx) as session:
        rows = await list_versions(session, tenant_id=ctx.tenant_id, template_name=template_name)
        return {"items": [_version_out(r) for r in rows], "total": len(rows)}


@router.get("/active")
async def get_active_prompt(
    request: Request,
    template_name: str = Query(..., min_length=1, max_length=127),
) -> Any:
    ctx = _ctx_of(request)
    denial = _gate(request, ctx, Action.PROMPT_READ, "prompt.read")
    if denial is not None:
        return denial

    async with tenant_session(ctx) as session:
        row = await get_active(session, tenant_id=ctx.tenant_id, template_name=template_name)
        if row is None:
            return {"active": None, "template_name": template_name}
        return {"active": _version_out(row), "template_name": template_name}


@router.post("")
async def create_prompt_draft(
    request: Request,
    payload: Annotated[DraftIn, Body()],
) -> Any:
    ctx = _ctx_of(request)
    denial = _gate(request, ctx, Action.PROMPT_RELEASE, "prompt.release")
    if denial is not None:
        return denial

    async with tenant_session(ctx) as session:
        try:
            row = await create_draft(
                session,
                ctx=ctx,
                template_name=payload.template_name,
                body=payload.body,
                notes=payload.notes,
            )
        except ReleaseError as exc:
            return _release_error(exc)
        await session.commit()
        return _version_out(row)


@router.post("/{version_id}/candidate")
async def submit_prompt_candidate(request: Request, version_id: str) -> Any:
    ctx = _ctx_of(request)
    denial = _gate(request, ctx, Action.PROMPT_RELEASE, "prompt.release")
    if denial is not None:
        return denial

    async with tenant_session(ctx) as session:
        try:
            row = await submit_candidate(session, ctx=ctx, version_id=_uuid(version_id))
        except ReleaseError as exc:
            return _release_error(exc)
        await session.commit()
        return _version_out(row)


@router.post("/{version_id}/promote")
async def promote_prompt_version(
    request: Request,
    version_id: str,
    payload: Annotated[EvidenceIn | None, Body()] = None,
) -> Any:
    ctx = _ctx_of(request)
    denial = _gate(request, ctx, Action.PROMPT_RELEASE, "prompt.release")
    if denial is not None:
        return denial

    evidence = _to_evidence(payload)
    if evidence is not None:
        evidence = _mark_p0(evidence)

    async with tenant_session(ctx) as session:
        try:
            row = await promote(session, ctx=ctx, version_id=_uuid(version_id), evidence=evidence)
        except ReleaseError as exc:
            return _release_error(exc)
        await session.commit()
        return _version_out(row)


@router.post("/{version_id}/reject")
async def reject_prompt_version(
    request: Request,
    version_id: str,
    payload: Annotated[RejectIn, Body()],
) -> Any:
    ctx = _ctx_of(request)
    denial = _gate(request, ctx, Action.PROMPT_RELEASE, "prompt.release")
    if denial is not None:
        return denial

    async with tenant_session(ctx) as session:
        try:
            row = await reject(
                session, ctx=ctx, version_id=_uuid(version_id), reason=payload.reason
            )
        except ReleaseError as exc:
            return _release_error(exc)
        await session.commit()
        return _version_out(row)


@router.post("/rollback")
async def rollback_prompt_version(
    request: Request,
    payload: Annotated[RollbackIn, Body()],
) -> Any:
    ctx = _ctx_of(request)
    denial = _gate(request, ctx, Action.PROMPT_RELEASE, "prompt.release")
    if denial is not None:
        return denial

    async with tenant_session(ctx) as session:
        try:
            row = await rollback(
                session,
                ctx=ctx,
                template_name=payload.template_name,
                to_version_id=_uuid(payload.to_version_id),
                reason=payload.reason,
            )
        except ReleaseError as exc:
            return _release_error(exc)
        await session.commit()
        return _version_out(row)


def _uuid(raw: str) -> uuid.UUID:
    """Parse a path id, mapping malformed input to a release error.

    A bad id must not reach the database as a cast error: it would surface
    as a 500 and, worse, leak that the id was the problem rather than the
    permission. Reporting NOT_FOUND matches the not-your-tenant case.
    """
    try:
        return uuid.UUID(raw)
    except (ValueError, AttributeError, TypeError) as exc:
        raise ReleaseError("NOT_FOUND", "no such prompt version for this tenant") from exc


__all__ = ["router"]
