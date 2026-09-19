"""Knowledge document API: upload, inspect, download (docs/development-plan.md).

    POST /v1/knowledge/documents                  register a document + start ingest
    GET  /v1/knowledge/documents/{id}/versions    list a document's versions
    GET  /v1/knowledge/versions/{id}              fetch one version's status
    POST /v1/knowledge/versions/{id}/ready        mark ingestion complete
    POST /v1/knowledge/versions/{id}/download-url mint a short-lived download URL
    GET  /v1/knowledge/spaces                     list spaces (draft publish targets)

Why this router exists
----------------------
Before it, `documents` / `document_versions` had models, an ingestion state
machine, an ACL table, and a SigV4 storage client - and no way for a document
to enter the system. Retrieval was fully implemented and permanently empty
because nothing could populate it. `presign_get` likewise had no caller, so
the documented access rule ("pre-signed short-lived URLs for all client-facing
access") was aspirational.

Authorization, split the way the rest of the API splits it:

- uploading and driving ingestion require `KNOWLEDGE_UPLOAD`
  (knowledge_manager, tenant_owner);
- minting a download URL requires `KNOWLEDGE_READ` *and* the per-document ACL.
  The policy engine answers "may this role read knowledge at all"; the ACL
  answers "this specific document". Both must pass.

The download route deliberately returns a URL rather than the bytes. Proxying
would put document content through the API's memory, its logs, and its request
timeouts, and would make revocation meaningless.
"""

import time
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, File, Form, Query, Request, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import select

from platform_core.api import error_response, require_write_idempotency, tenant_session
from platform_core.config import get_settings
from platform_core.identity import tenant_context
from platform_core.identity.tenant_context import TenantContext
from platform_core.knowledge import service
from platform_core.knowledge.models import KnowledgeSpace
from platform_policy import Action, Decision, PolicyEngine, Principal

router = APIRouter(prefix="/v1/knowledge", tags=["knowledge"])


class ReadyIn(BaseModel):
    version_label: str | None = Field(default=None, max_length=63)


class DownloadUrlIn(BaseModel):
    expires_seconds: int = Field(default=300, ge=60, le=3600)


def _principal_from_ctx(ctx: TenantContext) -> Principal:
    return Principal(
        tenant_id=str(ctx.tenant_id),
        actor_id=str(ctx.actor_id) if ctx.actor_id else "",
        role=ctx.role or "unknown",
    )


def _denied(action: str, reason: str) -> JSONResponse:
    """403, not a 200 carrying an error body."""
    return error_response(
        "KNOWLEDGE_ACCESS_DENIED",
        reason or "knowledge access denied",
        status_code=403,
        details={"action": action},
    )


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


# Status per error code. A malformed id, a foreign id, and a nonexistent id all
# map to 404 with the same body, so the endpoint cannot be used to probe for
# other tenants' document ids.
_STATUS = {
    "NOT_FOUND": 404,
    "UPLOAD_TOO_LARGE": 413,
    "UNSUPPORTED_CONTENT_TYPE": 415,
    "EMPTY_UPLOAD": 400,
    "INVALID_TITLE": 400,
    "INVALID_URI": 400,
    "INVALID_CLASSIFICATION": 400,
    "OBJECT_MISSING": 409,
}


def _error(exc: service.KnowledgeError) -> JSONResponse:
    return error_response(
        exc.code,
        exc.detail or exc.code,
        status_code=_STATUS.get(exc.code, 400),
    )


def _uuid(raw: str, what: str) -> uuid.UUID:
    """Map a malformed path id to NOT_FOUND rather than letting the cast fail.

    A database cast error surfaces as a 500 and reveals that the id was the
    problem, which is a different answer from "not yours" - enough to probe.
    """
    try:
        return uuid.UUID(raw)
    except (ValueError, AttributeError, TypeError) as exc:
        raise service.KnowledgeError("NOT_FOUND", f"no such {what} for this tenant") from exc


def _version_out(row: Any) -> dict[str, Any]:
    return {
        "version_id": str(row.id),
        "document_id": str(row.document_id),
        "version_label": row.version_label,
        "status": row.status,
        "ingestion_status": row.ingestion_status,
        "content_hash": row.content_hash,
        "object_uri": row.object_uri,
        "effective_at": row.effective_at,
        "expires_at": row.expires_at,
        "metadata": row.metadata_json,
    }


@router.post("/documents")
async def upload_document(
    request: Request,
    space_id: Annotated[str, Form()],
    title: Annotated[str, Form()],
    canonical_uri: Annotated[str, Form()],
    file: Annotated[UploadFile, File()],
    classification: Annotated[str, Form()] = "internal",
    version_label: Annotated[str, Form()] = "v1",
) -> Any:
    """Register a document, store its bytes, and leave it ready for indexing.

    Multipart rather than a pre-signed upload URL: the pilot's documents are
    small, and routing the bytes through the API is what lets content-type
    validation and the size cap be enforced *before* anything is stored. A
    pre-signed PUT would let a client write arbitrary bytes and only then have
    the API discover they are not allowed - after the object exists.
    """
    ctx = _ctx_of(request)
    denial = _gate(request, ctx, Action.KNOWLEDGE_UPLOAD, "knowledge.upload")
    if denial is not None:
        return denial

    try:
        space_uuid = _uuid(space_id, "knowledge space")
    except service.KnowledgeError as exc:
        return _error(exc)

    data = await file.read()
    try:
        async with tenant_session(ctx) as session:
            created = await service.create_document(
                session,
                tenant_id=ctx.tenant_id,
                space_id=space_uuid,
                title=title,
                canonical_uri=canonical_uri,
                data=data,
                content_type=file.content_type,
                filename=file.filename or "upload",
                owner_ref=str(ctx.actor_id) if ctx.actor_id else None,
                classification=classification,
                version_label=version_label,
            )

        # The row is committed before the bytes go to storage. A row without an
        # object is recoverable; an object without a row is an orphan nothing
        # would ever list or clean up.
        object_uri = service.upload_object(
            created.object_key, data, file.content_type or "application/octet-stream"
        )
    except service.KnowledgeError as exc:
        return _error(exc)
    except Exception as exc:  # storage unreachable, bucket missing, ...
        # The document row exists and is reported as failed rather than
        # silently vanishing; the client can retry the upload for the same
        # canonical_uri once storage recovers.
        return error_response(
            "STORAGE_UNAVAILABLE",
            f"document registered but object storage rejected the upload: {type(exc).__name__}",
            status_code=503,
        )

    return {
        "document_id": str(created.document_id),
        "version_id": str(created.version_id),
        "object_uri": object_uri,
        "content_hash": created.content_hash,
        "ingestion_status": created.ingestion_status,
    }


@router.get("/spaces")
async def list_spaces(request: Request) -> Any:
    """Knowledge spaces, for draft publish targeting and similar pickers.

    Exists because the gap-queue publish flow needs to name a space and an
    operator has no other way to look up a space id than this list.
    """
    ctx = _ctx_of(request)
    denial = _gate(request, ctx, Action.KNOWLEDGE_READ, "knowledge.read")
    if denial is not None:
        return denial

    async with tenant_session(ctx) as session:
        rows = (
            (await session.execute(select(KnowledgeSpace).order_by(KnowledgeSpace.name)))
            .scalars()
            .all()
        )
    return {"items": [{"id": str(row.id), "name": row.name} for row in rows], "total": len(rows)}


@router.get("/documents/{document_id}/versions")
async def list_document_versions(request: Request, document_id: str) -> Any:
    ctx = _ctx_of(request)
    denial = _gate(request, ctx, Action.KNOWLEDGE_READ, "knowledge.read")
    if denial is not None:
        return denial

    try:
        doc_uuid = _uuid(document_id, "document")
    except service.KnowledgeError as exc:
        return _error(exc)

    async with tenant_session(ctx) as session:
        rows = await service.list_versions(session, tenant_id=ctx.tenant_id, document_id=doc_uuid)
        if not rows:
            # No versions means either no such document or one owned by another
            # tenant; both are 404 so the response does not distinguish them.
            return _error(service.KnowledgeError("NOT_FOUND", "no such document for this tenant"))
        return {"items": [_version_out(r) for r in rows], "total": len(rows)}


@router.get("/versions/{version_id}")
async def get_version(request: Request, version_id: str) -> Any:
    ctx = _ctx_of(request)
    denial = _gate(request, ctx, Action.KNOWLEDGE_READ, "knowledge.read")
    if denial is not None:
        return denial

    try:
        vid = _uuid(version_id, "document version")
    except service.KnowledgeError as exc:
        return _error(exc)

    async with tenant_session(ctx) as session:
        try:
            row = await service.get_version(session, tenant_id=ctx.tenant_id, version_id=vid)
        except service.KnowledgeError as exc:
            return _error(exc)
        return _version_out(row)


@router.post("/versions/{version_id}/ready")
async def mark_version_ready(request: Request, version_id: str, body: ReadyIn | None = None) -> Any:
    """Advance a version to READY once the pipeline has indexed its chunks.

    This is the hand-off point from ingest to retrieval. It is a separate
    route because parsing, chunking and embedding happen in the worker, which
    owns those steps; the API's role is to record that they finished. The
    transition goes through `ingest.transition`, so an illegal jump (say,
    UPLOADED straight to READY without the intermediate states) fails here
    rather than producing a version that claims to be indexed while nothing
    was.
    """
    ctx = _ctx_of(request)
    denial = _gate(request, ctx, Action.KNOWLEDGE_UPLOAD, "knowledge.upload")
    if denial is not None:
        return denial

    try:
        vid = _uuid(version_id, "document version")
    except service.KnowledgeError as exc:
        return _error(exc)

    async with tenant_session(ctx) as session:
        try:
            row = await service.mark_ready(session, tenant_id=ctx.tenant_id, version_id=vid)
        except service.KnowledgeError as exc:
            return _error(exc)
        except Exception as exc:
            # An invalid transition is a client mistake (the version is in the
            # wrong state), not a server failure.
            from platform_core.knowledge.ingest import InvalidTransition

            if isinstance(exc, InvalidTransition):
                return error_response("INVALID_TRANSITION", str(exc), status_code=409)
            raise
        return _version_out(row)


@router.post("/versions/{version_id}/download-url")
async def create_download_url(
    request: Request,
    version_id: str,
    body: DownloadUrlIn | None = None,
    expires_seconds: int | None = Query(default=None, ge=60, le=3600),
) -> Any:
    """Mint a short-lived pre-signed GET URL for a version's stored object.

    Two independent gates, both required:

    1. `KNOWLEDGE_READ` - may this role read knowledge at all.
    2. the per-document ACL - may this principal read *this* document.

    Skipping step 2 is the tempting shortcut and the dangerous one: the URL is
    a bearer credential, so issuing it to an unauthorized principal would hand
    over the document without ever going through retrieval, making the ACL
    table advisory.
    """
    ctx = _ctx_of(request)
    denial = _gate(request, ctx, Action.KNOWLEDGE_READ, "knowledge.read")
    if denial is not None:
        return denial

    try:
        vid = _uuid(version_id, "document version")
    except service.KnowledgeError as exc:
        return _error(exc)

    requested = (
        expires_seconds if expires_seconds is not None else (body.expires_seconds if body else 300)
    )

    async with tenant_session(ctx) as session:
        try:
            key, url = await service.authorize_download(
                session,
                tenant_id=ctx.tenant_id,
                version_id=vid,
                principal_id=str(ctx.actor_id) if ctx.actor_id else "",
                role=ctx.role or "",
                expires_seconds=requested,
            )
        except service.KnowledgeError as exc:
            return _error(exc)

    return {
        "version_id": str(vid),
        "object_uri": key,
        "url": url,
        "expires_seconds": min(requested, get_settings().presign_expiry_seconds),
    }


# --- Alias management (plan 1.3) --------------------------------------------


class AliasIn(BaseModel):
    term: str = Field(min_length=1, max_length=127)
    alias: str = Field(min_length=1, max_length=127)
    # Multiplier scale, matching the DB CHECK `weight > 0 AND weight <= 2`:
    # 1.0 is neutral, 2.0 doubles a term's pull. (An earlier draft spoke
    # percent and crashed on its own default against that CHECK.)
    weight: float = Field(default=1.0, gt=0, le=2)


@router.get("/aliases")
async def list_aliases(request: Request) -> Any:
    """The tenant's alias table, as (term, alias, weight) rows."""
    ctx = _ctx_of(request)
    denial = _gate(request, ctx, Action.KNOWLEDGE_READ, "knowledge.read")
    if denial is not None:
        return denial

    from platform_core.knowledge.models import KnowledgeAlias

    async with tenant_session(ctx) as session:
        rows = (
            (await session.execute(select(KnowledgeAlias).order_by(KnowledgeAlias.alias)))
            .scalars()
            .all()
        )
    return {
        "items": [{"term": r.term, "alias": r.alias, "weight": float(r.weight)} for r in rows],
        "total": len(rows),
    }


@router.post("/aliases")
async def upsert_alias(request: Request, body: AliasIn) -> Any:
    """Create or update one alias. Upsert, because a corrected mapping should
    replace the wrong one, not fight it on the unique constraint."""
    ctx = _ctx_of(request)
    denial = _gate(request, ctx, Action.KNOWLEDGE_UPLOAD, "knowledge.upload")
    if denial is not None:
        return denial

    from sqlalchemy.dialects.postgresql import insert as pg_insert

    from platform_core.knowledge.models import KnowledgeAlias

    async with tenant_session(ctx) as session:
        stmt = (
            pg_insert(KnowledgeAlias)
            .values(
                tenant_id=ctx.tenant_id,
                term=body.term.strip().lower(),
                alias=body.alias.strip().lower(),
                weight=body.weight,
                created_at=int(time.time()),
            )
            .on_conflict_do_update(
                # Migration 0033 created `uq_aliases_tenant_alias` as a UNIQUE
                # INDEX, and `ON CONFLICT ON CONSTRAINT` only accepts a table
                # constraint — inferring on the indexed columns is the form
                # that works against both.
                index_elements=["tenant_id", "alias"],
                set_={"term": body.term.strip().lower(), "weight": body.weight},
            )
        )
        await session.execute(stmt)
    return {"status": "ok"}


@router.delete("/aliases/{alias}")
async def delete_alias(request: Request, alias: str) -> Any:
    ctx = _ctx_of(request)
    denial = _gate(request, ctx, Action.KNOWLEDGE_UPLOAD, "knowledge.upload")
    if denial is not None:
        return denial

    from sqlalchemy import delete as sa_delete

    from platform_core.knowledge.models import KnowledgeAlias

    async with tenant_session(ctx) as session:
        await session.execute(
            sa_delete(KnowledgeAlias).where(
                KnowledgeAlias.tenant_id == ctx.tenant_id,
                KnowledgeAlias.alias == alias.strip().lower(),
            )
        )
    return {"status": "ok"}
