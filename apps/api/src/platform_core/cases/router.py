"""Case API (docs/api-contracts.md case command API).

- POST /v1/cases                      create
- GET  /v1/cases/{case_id}            read one
- GET  /v1/cases                      list (tenant-scoped, paginated)
- POST /v1/cases/{case_id}/commands   the only way a case changes state

Every command goes through CaseService so transition rules, optimistic
versioning, SLA accrual and the outbox event all happen in one transaction
(see cases/service.py). This router owns HTTP concerns only: policy gates,
payload validation, error mapping and audit records.

Error mapping (stable codes per docs/api-contracts.md):
  LookupError            -> 404 CASE_NOT_FOUND
  TransitionNotAllowed   -> 409 CASE_TRANSITION_NOT_ALLOWED
  VersionConflict        -> 409 CASE_VERSION_CONFLICT
  unknown command        -> 400 VALIDATION_FAILED
"""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Body, File, Form, Query, Request, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from platform_core.api import (
    CASE_NOT_FOUND,
    CASE_TRANSITION_NOT_ALLOWED,
    CASE_VERSION_CONFLICT,
    IDEMPOTENCY_KEY_REQUIRED,
    VALIDATION_FAILED,
    error_response,
    get_context,
    new_trace_id,
    ok_response,
    parse_uuid,
    require_idempotency_key,
    require_policy,
    require_write_idempotency,
    tenant_session,
)
from platform_core.audit import service as audit_service
from platform_core.cases import attachments as attachment_rules
from platform_core.cases.models import (
    Case,
    CaseAttachment,
    CaseConversation,
    CaseStatus,
    TransitionNotAllowed,
    VersionConflict,
)
from platform_core.cases.service import CaseError, CaseService
from platform_core.config import get_settings
from platform_core.outbox_service import enqueue
from platform_policy import Action

router = APIRouter(prefix="/v1/cases", tags=["cases"])

VALID_COMMANDS = frozenset({"transition", "change_priority", "assign", "record_first_response"})
VALID_PRIORITIES = frozenset({"p0", "p1", "p2", "p3"})


class CaseCreateIn(BaseModel):
    subject: str = Field(min_length=1, max_length=512)
    description: str = Field(default="", max_length=20_000)
    priority: str = Field(default="p2")
    category: str = Field(default="general", max_length=63)
    # The account this Case is about. Determines the SLA policy via the
    # account's contract tier (see `cases.models.sla_policy_for_tier`).
    enterprise_account_id: uuid.UUID | None = None
    # The conversation this Case came from, when it came from one. Recorded on
    # `CaseConversation`, which is what lets an escalated case be traced back
    # to the customer waiting on it - and what priority claiming filters on.
    # Optional because a Case can be raised from a phone call or an email.
    conversation_ref_id: uuid.UUID | None = None


class CaseCommandIn(BaseModel):
    command: str
    expected_version: int | None = None
    parameters: dict[str, Any] = Field(default_factory=dict)
    reason: str = Field(default="", max_length=500)


def _serialize(case: Case) -> dict[str, Any]:
    return {
        "case_id": str(case.id),
        "subject": case.subject,
        "status": case.status,
        "priority": case.priority,
        "category": case.category,
        # Both were previously absent from the API surface entirely, which is
        # how `enterprise_account_id` came to be a column nothing could write.
        "enterprise_account_id": (
            str(case.enterprise_account_id) if case.enterprise_account_id else None
        ),
        "sla_tier": case.sla_tier,
        "assignee_ref": case.assignee_ref,
        "team_ref": case.team_ref,
        "version": int(case.version),
        "opened_at": case.opened_at,
        "first_response_due_at": case.first_response_due_at,
        "resolution_due_at": case.resolution_due_at,
        "first_responded_at": case.first_responded_at,
        "resolved_at": case.resolved_at,
        "closed_at": case.closed_at,
    }


@router.post("")
async def create_case(request: Request, body: CaseCreateIn) -> Any:
    ctx = get_context(request)
    if ctx is None:
        return error_response("AUTH_UNRESOLVED", "tenant context not resolved", status_code=401)
    denied = require_policy(ctx, Action.CASE_CREATE)
    if denied is not None:
        return denied

    # Creating a case is a write: without an idempotency key a retry after a
    # timeout creates a duplicate ticket. The commands endpoint below already
    # requires one; create must be consistent with it.
    missing_idem = require_write_idempotency(request, Action.CASE_CREATE)
    if missing_idem is not None:
        return missing_idem

    if body.priority not in VALID_PRIORITIES:
        return error_response(
            VALIDATION_FAILED,
            f"priority must be one of {sorted(VALID_PRIORITIES)}",
            status_code=400,
        )

    trace_id = new_trace_id()
    async with tenant_session(ctx) as session:
        service = CaseService(session)
        try:
            case = await service.create_case(
                tenant_id=ctx.tenant_id,
                subject=body.subject,
                description=body.description,
                priority=body.priority,
                category=body.category,
                enterprise_account_id=body.enterprise_account_id,
                conversation_ref_id=body.conversation_ref_id,
            )
        except CaseError as exc:
            # One code for "no such account" and "another tenant's account":
            # RLS cannot see the latter, and distinguishing them would make
            # this endpoint a way to enumerate account ids.
            return error_response(
                exc.code,
                "the account does not exist in this tenant",
                status_code=404 if exc.code == "ACCOUNT_NOT_FOUND" else 400,
            )
        await audit_service.record(
            session,
            ctx=ctx,
            action="case.created",
            resource_type="case",
            resource_id=case.id,
            decision="completed",
            reason_code="OK",
            after={
                "priority": case.priority,
                "category": case.category,
                "enterprise_account_id": (
                    str(case.enterprise_account_id) if case.enterprise_account_id else None
                ),
                "sla_tier": case.sla_tier,
            },
            trace_id=trace_id,
        )
        await enqueue(
            session,
            tenant_id=ctx.tenant_id,
            event_type="case.created",
            aggregate_type="case",
            aggregate_id=str(case.id),
            payload={"case_id": str(case.id), "status": case.status},
            trace_id=trace_id,
        )
        payload = _serialize(case)

    return ok_response({"case": payload}, trace_id=trace_id)


@router.get("")
async def list_cases(
    request: Request,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    status: str | None = Query(default=None, max_length=31),
    priority: str | None = Query(default=None, max_length=7),
) -> Any:
    ctx = get_context(request)
    if ctx is None:
        return error_response("AUTH_UNRESOLVED", "tenant context not resolved", status_code=401)
    denied = require_policy(ctx, Action.CASE_READ)
    if denied is not None:
        return denied

    stmt = select(Case).order_by(Case.opened_at.desc(), Case.id)
    if status:
        stmt = stmt.where(Case.status == status)
    if priority:
        stmt = stmt.where(Case.priority == priority)

    async with tenant_session(ctx) as session:
        total = (
            await session.execute(select(func.count()).select_from(stmt.subquery()))
        ).scalar_one()
        rows = (await session.execute(stmt.limit(limit).offset(offset))).scalars().all()
        items = [_serialize(c) for c in rows]

    return ok_response({"items": items, "total": int(total), "limit": limit, "offset": offset})


@router.get("/{case_id}")
async def get_case(request: Request, case_id: str) -> Any:
    ctx = get_context(request)
    if ctx is None:
        return error_response("AUTH_UNRESOLVED", "tenant context not resolved", status_code=401)
    denied = require_policy(ctx, Action.CASE_READ)
    if denied is not None:
        return denied

    try:
        cid = parse_uuid(case_id, field="case_id")
    except ValueError as exc:
        return error_response(VALIDATION_FAILED, str(exc), status_code=400)

    async with tenant_session(ctx) as session:
        # RLS bounds this to the caller's tenant even without the explicit
        # tenant filter; the filter is kept so the intent is readable.
        case = (await session.execute(select(Case).where(Case.id == cid))).scalar_one_or_none()
        if case is None:
            return error_response(CASE_NOT_FOUND, "case not found", status_code=404)
        payload = _serialize(case)

    return ok_response({"case": payload})


@router.get("/{case_id}/workbench")
async def case_workbench(request: Request, case_id: str) -> Any:
    """Everything an agent needs to take over without re-asking.

    Feature list 7.4/7.6: the complaint about handoffs is never the handoff,
    it is that the customer then has to repeat themselves. This bundles the
    case, the conversation so far, the AI's last proposal with the sources it
    cited, and the account's tier - so the first human message can start from
    "I can see you asked about…" rather than "could you give me your order
    number".

    Read-only and derived: it assembles what already exists and writes
    nothing, so it cannot drift from the underlying rows.

    Related cases are same-category and recent, and the payload says so
    (`basis`). Calling that "similar" would promise a relevance the query does
    not compute.
    """
    ctx = get_context(request)
    if ctx is None:
        return error_response("AUTH_UNRESOLVED", "tenant context not resolved", status_code=401)
    denied = require_policy(ctx, Action.CASE_READ)
    if denied is not None:
        return denied

    try:
        cid = parse_uuid(case_id, field="case_id")
    except ValueError as exc:
        return error_response(VALIDATION_FAILED, str(exc), status_code=400)

    async with tenant_session(ctx) as session:
        case = (await session.execute(select(Case).where(Case.id == cid))).scalar_one_or_none()
        if case is None:
            return error_response(CASE_NOT_FOUND, "case not found", status_code=404)

        link = (
            await session.execute(
                select(CaseConversation.conversation_ref_id)
                .where(CaseConversation.case_id == cid)
                .limit(1)
            )
        ).scalar_one_or_none()

        conversation: list[dict[str, str]] = []
        suggestion: dict[str, object] | None = None
        if link is not None:
            from platform_core.agent_runtime import conversation_store

            turns = await conversation_store.load_turns(
                session, tenant_id=ctx.tenant_id, conversation_ref_id=link, limit=50
            )
            conversation = [{"role": turn.role.value, "text": turn.text} for turn in turns]
            latest = await conversation_store.latest_suggestion(
                session, tenant_id=ctx.tenant_id, conversation_ref_id=link
            )
            if latest is not None:
                suggestion = {"text": latest[0], "sources": latest[1]}

        related = (
            await session.execute(
                select(Case.id, Case.subject, Case.status)
                .where(
                    Case.tenant_id == ctx.tenant_id,
                    Case.category == case.category,
                    Case.id != cid,
                )
                .order_by(Case.opened_at.desc())
                .limit(5)
            )
        ).all()

        tier: str | None = None
        contacts: list[str] = []
        if case.enterprise_account_id is not None:
            # Through the identity seam, not by importing its models.
            from platform_core.identity import org

            facts = await org.account_sla_facts(
                session, tenant_id=ctx.tenant_id, account_id=case.enterprise_account_id
            )
            if facts is not None:
                tier = facts[0]
            # Feature list 2.1: one company reaches us through several
            # channels, and each channel is a different Chatwoot contact. The
            # binding already says they are the same account; showing them is
            # what stops an agent treating the email from last week and the
            # WeChat message from this morning as two different customers.
            contacts = await org.list_contacts(
                session, tenant_id=ctx.tenant_id, account_id=case.enterprise_account_id
            )

    return ok_response(
        {
            "case": _serialize(case),
            "account_tier": tier,
            "account_contacts": contacts,
            "conversation": conversation,
            "ai_suggestion": suggestion,
            "related_cases": {
                "basis": "same_category",
                "items": [
                    {
                        "case_id": str(row[0]),
                        "subject": row[1],
                        "status": row[2],
                    }
                    for row in related
                ],
            },
        }
    )


@router.post("/{case_id}/commands")
async def apply_case_command(
    request: Request,
    case_id: str,
    body: Annotated[CaseCommandIn, Body()],
) -> Any:
    ctx = get_context(request)
    if ctx is None:
        return error_response("AUTH_UNRESOLVED", "tenant context not resolved", status_code=401)
    denied = require_policy(ctx, Action.CASE_UPDATE)
    if denied is not None:
        return denied

    idem = require_idempotency_key(request)
    if not idem:
        return error_response(
            IDEMPOTENCY_KEY_REQUIRED,
            "every case command must carry an Idempotency-Key header",
            status_code=400,
        )

    try:
        cid = parse_uuid(case_id, field="case_id")
    except ValueError as exc:
        return error_response(VALIDATION_FAILED, str(exc), status_code=400)

    if body.command not in VALID_COMMANDS:
        return error_response(
            VALIDATION_FAILED,
            f"command must be one of {sorted(VALID_COMMANDS)}",
            status_code=400,
        )

    # Validate parameters before touching the database so a malformed
    # command cannot consume a version bump.
    problem = _validate_parameters(body)
    if problem:
        return error_response(VALIDATION_FAILED, problem, status_code=400)

    trace_id = new_trace_id()
    async with tenant_session(ctx) as session:
        service = CaseService(session)
        before_status: str | None = None
        try:
            # Read the prior state for the audit trail's before snapshot.
            prior = (
                await session.execute(select(Case.status, Case.version).where(Case.id == cid))
            ).one_or_none()
            if prior is not None:
                before_status = prior[0]

            case = await service.apply_command(
                tenant_id=ctx.tenant_id,
                case_id=cid,
                command=body.command,
                expected_version=body.expected_version,
                parameters=body.parameters,
            )
        except LookupError:
            return error_response(CASE_NOT_FOUND, "case not found", status_code=404)
        except TransitionNotAllowed as exc:
            await audit_service.record(
                session,
                ctx=ctx,
                action="case.command.rejected",
                resource_type="case",
                resource_id=cid,
                decision="denied",
                reason_code=CASE_TRANSITION_NOT_ALLOWED,
                trace_id=trace_id,
            )
            return error_response(CASE_TRANSITION_NOT_ALLOWED, str(exc), status_code=409)
        except VersionConflict as exc:
            await audit_service.record(
                session,
                ctx=ctx,
                action="case.command.rejected",
                resource_type="case",
                resource_id=cid,
                decision="denied",
                reason_code=CASE_VERSION_CONFLICT,
                trace_id=trace_id,
            )
            return error_response(CASE_VERSION_CONFLICT, str(exc), status_code=409)

        await audit_service.record(
            session,
            ctx=ctx,
            action="case.command.applied",
            resource_type="case",
            resource_id=case.id,
            decision="completed",
            reason_code="OK",
            before={"status": before_status},
            after={"status": case.status, "version": int(case.version), "reason": body.reason},
            trace_id=trace_id,
        )
        await enqueue(
            session,
            tenant_id=ctx.tenant_id,
            event_type="case.updated",
            aggregate_type="case",
            aggregate_id=str(case.id),
            payload={
                "case_id": str(case.id),
                "command": body.command,
                "status": case.status,
                "version": int(case.version),
            },
            trace_id=trace_id,
        )
        payload = _serialize(case)

    return ok_response({"case": payload, "idempotency_key": idem}, trace_id=trace_id)


def _validate_parameters(body: CaseCommandIn) -> str:
    """Return a human-readable problem string, or "" when valid.

    Checked here rather than in the service because it is a request-shape
    concern; the service owns transition and version rules.
    """
    if body.command == "transition":
        target = body.parameters.get("target")
        if not target:
            return "transition requires parameters.target"
        if target not in {s.value for s in CaseStatus}:
            return f"unknown case status: {target}"
    elif body.command == "change_priority":
        priority = body.parameters.get("priority")
        if priority not in VALID_PRIORITIES:
            return f"priority must be one of {sorted(VALID_PRIORITIES)}"
    elif body.command == "assign":
        if not body.parameters.get("assignee_ref") and not body.parameters.get("team_ref"):
            return "assign requires parameters.assignee_ref or parameters.team_ref"
        # Both columns are String(255); without this bound a longer value
        # reaches the database and surfaces as a bare 500.
        for field in ("assignee_ref", "team_ref"):
            value = body.parameters.get(field)
            if value is not None and (not isinstance(value, str) or len(value) > 255):
                return f"parameters.{field} must be a string of at most 255 characters"
    return ""


# --- Evidence attachments (research report stage 3) ------------------------


def _serialize_attachment(row: CaseAttachment, *, url: str | None) -> dict[str, Any]:
    return {
        "attachment_id": str(row.id),
        "filename": row.filename,
        "content_type": row.content_type,
        "size_bytes": row.size_bytes,
        "uploaded_by": row.uploaded_by,
        "created_at": row.created_at,
        # Short-lived and only issued on read. Null on the upload response, and
        # that is deliberate rather than an omission: the upload's job is to
        # store, and a URL minted at upload time would outlive the request that
        # asked for it while sitting in a log.
        "url": url,
    }


@router.post("/{case_id}/attachments")
async def upload_case_attachment(
    request: Request,
    case_id: str,
    file: Annotated[UploadFile, File()],
    uploaded_by: Annotated[str | None, Form()] = None,
) -> Any:
    """Attach evidence to a case.

    Multipart rather than a pre-signed PUT, for the reason the knowledge upload
    records: routing the bytes through the API is what lets the content type
    and the size cap be enforced *before* anything is stored. A pre-signed PUT
    lets a client write arbitrary bytes and only then have the API discover
    they are not allowed, after the object exists.
    """
    ctx = get_context(request)
    if ctx is None:
        return error_response("AUTH_UNRESOLVED", "tenant context not resolved", status_code=401)
    denied = require_policy(ctx, Action.CASE_UPDATE)
    if denied is not None:
        return denied
    # A write, and a retried upload is a second copy of the evidence.
    missing_idem = require_write_idempotency(request, Action.CASE_UPDATE)
    if missing_idem is not None:
        return missing_idem

    try:
        cid = parse_uuid(case_id, field="case_id")
    except ValueError as exc:
        return error_response(VALIDATION_FAILED, str(exc), status_code=400)

    from platform_core.knowledge.service import object_storage

    data = await file.read()
    try:
        content_type = attachment_rules.validate_attachment(
            content_type=file.content_type, data=data
        )
    except attachment_rules.AttachmentError as exc:
        return error_response(exc.code, exc.detail or str(exc), status_code=400)

    trace_id = new_trace_id()
    async with tenant_session(ctx) as session:
        try:
            row = await attachment_rules.create_attachment(
                session,
                tenant_id=ctx.tenant_id,
                case_id=cid,
                filename=file.filename or "attachment",
                content_type=content_type,
                data=data,
                uploaded_by=uploaded_by,
                storage=object_storage(get_settings()),
            )
        except attachment_rules.AttachmentError as exc:
            return error_response(
                exc.code,
                # One message for "no such case" and "another tenant's case":
                # RLS cannot see the latter, and distinguishing them would make
                # this endpoint a way to enumerate case ids.
                "the case does not exist in this tenant",
                status_code=404,
            )
        await audit_service.record(
            session,
            ctx=ctx,
            action="case.attachment_added",
            resource_type="case",
            resource_id=cid,
            decision="completed",
            reason_code="OK",
            after={
                "filename": row.filename,
                "content_type": row.content_type,
                "size_bytes": row.size_bytes,
            },
            trace_id=trace_id,
        )
        payload = _serialize_attachment(row, url=None)

    return ok_response({"attachment": payload}, trace_id=trace_id)


@router.get("/{case_id}/attachments")
async def list_case_attachments(request: Request, case_id: str) -> Any:
    """The case's evidence, each with a short-lived download URL.

    The URL is signed here rather than stored: a pre-signed URL in a row would
    be expired by the time anyone read it, and one long enough to outlive a
    review is one long enough to leak.
    """
    ctx = get_context(request)
    if ctx is None:
        return error_response("AUTH_UNRESOLVED", "tenant context not resolved", status_code=401)
    denied = require_policy(ctx, Action.CASE_READ)
    if denied is not None:
        return denied

    try:
        cid = parse_uuid(case_id, field="case_id")
    except ValueError as exc:
        return error_response(VALIDATION_FAILED, str(exc), status_code=400)

    from platform_core.knowledge.service import presign_for

    async with tenant_session(ctx) as session:
        case = (await session.execute(select(Case.id).where(Case.id == cid))).scalar_one_or_none()
        if case is None:
            return error_response(CASE_NOT_FOUND, "case not found", status_code=404)
        rows = await attachment_rules.list_attachments(session, case_id=cid)
        expires = get_settings().presign_expiry_seconds
        items = [
            _serialize_attachment(row, url=presign_for(row.object_key, expires_seconds=expires))
            for row in rows
        ]

    return ok_response({"items": items, "total": len(items)})
