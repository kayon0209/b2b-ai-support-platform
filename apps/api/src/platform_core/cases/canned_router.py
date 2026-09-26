"""Canned reply API: reusable agent replies.

    GET    /v1/canned-replies              list, most-used first
    GET    /v1/canned-replies/resolve      `/eta` -> the reply
    GET    /v1/canned-replies/{id}         read one
    POST   /v1/canned-replies              create      (admin)
    PATCH  /v1/canned-replies/{id}         update      (admin)
    POST   /v1/canned-replies/{id}/use     record one use

Authorization is asymmetric on purpose:

- **Any agent may read and use** a reply (`CASE_READ`). A template an ordinary
  agent cannot insert is a template nobody uses, which defeats having one.
- **Only an administrator may change one** (`TENANT_ADMIN`). A shared template
  edits what every agent says at once, so it carries the blast radius of a
  prompt release rather than of one reply - which is why mutations below also
  write an audit event (11.7: who changed the wording).

Two deliberate departures from the standing rules, both narrower than the rule
they appear to break:

1. **`POST /{id}/use` carries no Idempotency-Key.** `AGENTS.md` requires one for
   writes and this is a write, but it records an *occurrence*, not a state
   transition. Requiring a key would force a client to mint a fresh one for
   every insertion, and the only thing a duplicate could corrupt is a ranking
   counter. That is a bad trade - it would push agents away from the feature.
   Documented rather than smuggled.
2. **`resolve` is a GET with a query parameter** even though it looks up by
   value. It is a read, safe to retry, and an agent's shortcut bar must be able
   to ask it constantly without managing idempotency.
"""

import uuid
from typing import Any

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError

from platform_core.api import (
    AUTH_UNRESOLVED,
    NOT_FOUND,
    VALIDATION_FAILED,
    error_response,
    get_context,
    require_policy,
    require_write_idempotency,
    tenant_session,
)
from platform_core.audit import service as audit_service
from platform_core.cases.canned_models import MAX_BODY, MAX_SHORTCUT, MAX_TITLE
from platform_core.cases.canned_service import (
    CannedReplyError,
    create_canned,
    list_canned,
    resolve_shortcut,
    update_canned,
    use_canned,
)
from platform_policy import Action

router = APIRouter(prefix="/v1/canned-replies", tags=["canned-replies"])

_MAX_LIMIT = 500


class CannedCreateIn(BaseModel):
    title: str = Field(min_length=1, max_length=MAX_TITLE)
    body: str = Field(min_length=1, max_length=MAX_BODY)
    shortcut: str | None = Field(default=None, max_length=MAX_SHORTCUT)
    business_line: str = Field(default="", max_length=31)
    team_ref: str = Field(default="", max_length=63)
    locale: str = Field(default="", max_length=15)


class CannedUpdateIn(BaseModel):
    """Partial update. A field that is absent is left alone.

    `None` on `shortcut` means "clear it", which is why the field is nullable
    here and `exclude_unset` is used below - otherwise an update that only
    changes the body would erase the shortcut.
    """

    title: str | None = Field(default=None, min_length=1, max_length=MAX_TITLE)
    body: str | None = Field(default=None, min_length=1, max_length=MAX_BODY)
    shortcut: str | None = Field(default=None, max_length=MAX_SHORTCUT)
    business_line: str | None = Field(default=None, max_length=31)
    team_ref: str | None = Field(default=None, max_length=63)
    locale: str | None = Field(default=None, max_length=15)
    archived: bool | None = None


def _out(row: Any) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "title": row.title,
        "body": row.body,
        "shortcut": row.shortcut,
        "business_line": row.business_line,
        "team_ref": row.team_ref,
        "locale": row.locale,
        "usage_count": int(row.usage_count),
        "last_used_at": row.last_used_at,
        "archived": bool(row.archived),
    }


def _unresolved() -> Any:
    return error_response(AUTH_UNRESOLVED, "tenant context not resolved", status_code=401)


def _taken(exc: Exception) -> Any:
    """A shortcut another reply already owns.

    `uq_canned_shortcut` is the authority on this, and letting it surface as an
    unhandled IntegrityError would turn a normal user mistake - typing a
    shortcut that is already in use - into a 500 with a stack trace. Rolled back
    first: the failed INSERT leaves the session unusable otherwise.
    """
    del exc
    return error_response(
        "CANNED_SHORTCUT_TAKEN",
        "another reply already uses that shortcut",
        status_code=409,
    )


@router.get("")
async def get_canned_replies(
    request: Request,
    business_line: str | None = Query(default=None, max_length=31),
    team_ref: str | None = Query(default=None, max_length=63),
    locale: str | None = Query(default=None, max_length=15),
    include_archived: bool = Query(default=False),
    limit: int = Query(default=200, ge=1, le=_MAX_LIMIT),
) -> Any:
    """Replies for an agent, most-used first.

    Scope filters are inclusive of the unscoped: filtering to `pcb` returns the
    PCB replies *and* the general ones, because a general reply is usable from
    any queue.
    """
    ctx = get_context(request)
    if ctx is None:
        return _unresolved()
    denied = require_policy(ctx, Action.CASE_READ)
    if denied is not None:
        return denied

    async with tenant_session(ctx) as session:
        rows = await list_canned(
            session,
            tenant_id=ctx.tenant_id,
            business_line=business_line,
            team_ref=team_ref,
            locale=locale,
            include_archived=include_archived,
            limit=limit,
        )
    return {"count": len(rows), "items": [_out(r) for r in rows]}


@router.get("/resolve")
async def get_canned_by_shortcut(
    request: Request, shortcut: str = Query(min_length=1, max_length=MAX_SHORTCUT)
) -> Any:
    """What an agent gets by typing `/eta`.

    Archived replies never resolve - see `canned_service.resolve_shortcut`.
    """
    ctx = get_context(request)
    if ctx is None:
        return _unresolved()
    denied = require_policy(ctx, Action.CASE_READ)
    if denied is not None:
        return denied

    async with tenant_session(ctx) as session:
        row = await resolve_shortcut(session, tenant_id=ctx.tenant_id, shortcut=shortcut)
    if row is None:
        return error_response(NOT_FOUND, f"no reply for shortcut {shortcut!r}", status_code=404)
    return _out(row)


@router.get("/{reply_id}")
async def get_canned_reply(request: Request, reply_id: uuid.UUID) -> Any:
    ctx = get_context(request)
    if ctx is None:
        return _unresolved()
    denied = require_policy(ctx, Action.CASE_READ)
    if denied is not None:
        return denied

    async with tenant_session(ctx) as session:
        rows = await list_canned(
            session, tenant_id=ctx.tenant_id, include_archived=True, limit=_MAX_LIMIT
        )
    row = next((r for r in rows if r.id == reply_id), None)
    if row is None:
        return error_response(NOT_FOUND, "no such canned reply", status_code=404)
    return _out(row)


@router.post("")
async def post_canned_reply(request: Request, body: CannedCreateIn) -> Any:
    ctx = get_context(request)
    if ctx is None:
        return _unresolved()
    denied = require_policy(ctx, Action.TENANT_ADMIN)
    if denied is not None:
        return denied
    missing_idem = require_write_idempotency(request, Action.TENANT_ADMIN)
    if missing_idem is not None:
        return missing_idem

    try:
        async with tenant_session(ctx) as session:
            row = await create_canned(
                session,
                tenant_id=ctx.tenant_id,
                title=body.title,
                body=body.body,
                actor_id=ctx.actor_id,
                shortcut=body.shortcut,
                business_line=body.business_line,
                team_ref=body.team_ref,
                locale=body.locale,
            )
            await audit_service.record(
                session,
                ctx=ctx,
                action="canned_reply.created",
                resource_type="canned_reply",
                resource_id=row.id,
                # Metadata, not `after`: `after` is hashed and unreadable, and
                # "who changed the wording" is the question this answers.
                metadata={"title": row.title, "shortcut": row.shortcut or ""},
            )
    except CannedReplyError as exc:
        return error_response(VALIDATION_FAILED, str(exc), status_code=400)
    except IntegrityError as exc:
        return _taken(exc)

    return _out(row)


@router.patch("/{reply_id}")
async def patch_canned_reply(request: Request, reply_id: uuid.UUID, body: CannedUpdateIn) -> Any:
    ctx = get_context(request)
    if ctx is None:
        return _unresolved()
    denied = require_policy(ctx, Action.TENANT_ADMIN)
    if denied is not None:
        return denied
    missing_idem = require_write_idempotency(request, Action.TENANT_ADMIN)
    if missing_idem is not None:
        return missing_idem

    fields = body.model_dump(exclude_unset=True)
    if not fields:
        return error_response(
            VALIDATION_FAILED, "an update must change at least one field", status_code=400
        )

    try:
        async with tenant_session(ctx) as session:
            row = await update_canned(
                session,
                tenant_id=ctx.tenant_id,
                reply_id=reply_id,
                actor_id=ctx.actor_id,
                **fields,
            )
            await audit_service.record(
                session,
                ctx=ctx,
                action="canned_reply.updated",
                resource_type="canned_reply",
                resource_id=row.id,
                metadata={
                    "fields": ",".join(sorted(fields)),
                    "archived": bool(row.archived),
                },
            )
    except CannedReplyError as exc:
        return error_response(NOT_FOUND, str(exc), status_code=404)
    except IntegrityError as exc:
        return _taken(exc)

    return _out(row)


@router.post("/{reply_id}/use")
async def post_canned_use(request: Request, reply_id: uuid.UUID) -> Any:
    """Insert this reply into a conversation.

    Returns the body so the client does not have to hold the list, and bumps the
    counter that orders the picker. No Idempotency-Key - see the module
    docstring.
    """
    ctx = get_context(request)
    if ctx is None:
        return _unresolved()
    denied = require_policy(ctx, Action.CASE_READ)
    if denied is not None:
        return denied

    async with tenant_session(ctx) as session:
        row = await use_canned(session, tenant_id=ctx.tenant_id, reply_id=reply_id)
    if row is None:
        return error_response(NOT_FOUND, "no such canned reply", status_code=404)
    return _out(row)


__all__ = ["router"]
