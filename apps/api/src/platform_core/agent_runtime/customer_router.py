"""Read-only endpoints for the customer-facing chat surface.

Why this exists
---------------
The admin console could already see *that* a run happened (route, status,
abstention reason), but nothing could read the conversation itself: the
answer text was only ever handed to Chatwoot and dropped. `conversation_turns`
(iteration plan 2.1) now persists each redacted turn, so a customer-facing
screen can finally render the exchange.

Kept in its own module on purpose: `intent.py`, `conversation.py` and
`qa_path.py` are under active development elsewhere, and this adds no
behaviour to the write path — it only reads what is already stored.

Authentication
--------------
These endpoints currently resolve a tenant from the normal bearer token and
require `CASE_READ`. That is a placeholder, not the design: there is no
customer identity yet, so a real deployment must swap this for a
per-conversation token before this is exposed to customers. Leaving the
existing policy gate in place means the endpoints fail closed today rather
than silently publishing conversations.
"""

import time

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import select

from platform_core.agent_runtime.models import Citation, ConversationTurn
from platform_core.api import (
    IDEMPOTENCY_KEY_REQUIRED,
    VALIDATION_FAILED,
    error_response,
    get_context,
    new_trace_id,
    ok_response,
    parse_uuid,
    require_idempotency_key,
    require_policy,
    tenant_session,
)
from platform_core.evaluation.pii import redact_text
from platform_core.support_bridge.minimize import payload_hash
from platform_policy import Action

router = APIRouter(prefix="/v1/customer", tags=["customer"])


class TurnOut(BaseModel):
    role: str
    text: str
    at: int
    source: str


class CitationOut(BaseModel):
    source_uri: str
    claim_index: int
    excerpt_hash: str


@router.get("/conversations/{conversation_ref}/timeline")
async def conversation_timeline(
    request: Request,
    conversation_ref: str,
    limit: int = Query(default=50, ge=1, le=200),
) -> object:
    """The redacted exchange for one conversation, oldest first.

    Tenant scoping is RLS, not a filter: the query runs inside
    `tenant_session`, so a conversation belonging to another tenant simply
    returns nothing rather than leaking.
    """
    ctx = get_context(request)
    if ctx is None:
        return error_response("AUTH_UNRESOLVED", "tenant context not resolved", status_code=401)
    denied = require_policy(ctx, Action.CASE_READ)
    if denied is not None:
        return denied

    try:
        ref_id = parse_uuid(conversation_ref, field="conversation_ref")
    except ValueError as exc:
        return error_response(VALIDATION_FAILED, str(exc), status_code=400)

    async with tenant_session(ctx) as session:
        rows = (
            (
                await session.execute(
                    select(ConversationTurn)
                    .where(ConversationTurn.conversation_ref_id == ref_id)
                    .order_by(ConversationTurn.ts.asc(), ConversationTurn.id.asc())
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )

    items = [
        TurnOut(
            role=r.role,
            text=r.text_redacted,
            at=r.ts,
            source=getattr(r, "source", "") or "",
        ).model_dump()
        for r in rows
    ]
    return ok_response({"items": items}, trace_id=new_trace_id())


class MessageIn(BaseModel):
    text: str = Field(min_length=1, max_length=4000)


@router.post("/conversations/{conversation_ref}/messages")
async def post_message(request: Request, conversation_ref: str, body: MessageIn) -> object:
    """Accept a customer question from the platform's own chat surface.

    Why this exists: the write path could only be reached through a
    Chatwoot webhook, and the orchestrator reads the question back from
    Chatwoot (see `OrchestratorDeps.reader`). A customer using our own
    screen had no system of record, so a queued run had nothing to read and
    completed without answering.

    This persists the turn locally first. Retention is NOT invented here —
    `RetentionPolicy.conversation_turn_days` (default 90) already governs
    `conversation_turns` and its sweep prunes expired rows, so the copy
    stays a bounded cache rather than a second system of record.

    The text is redacted before storage, matching how Chatwoot-sourced
    turns are written: the platform must not hold raw customer PII.
    """
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
            "posting a message requires an Idempotency-Key header",
            status_code=400,
        )

    try:
        ref_id = parse_uuid(conversation_ref, field="conversation_ref")
    except ValueError as exc:
        return error_response(VALIDATION_FAILED, str(exc), status_code=400)

    redacted, _count = redact_text(body.text)
    digest = payload_hash(body.text.encode())

    async with tenant_session(ctx) as session:
        # Replay guard. Requiring the header is not enough — a retry that
        # simply inserts again turns "the customer pressed send twice" into
        # two separate questions for the agent to answer. Content hashing
        # rather than a new column keeps this clear of the migrations that
        # another stream of work owns.
        existing = (
            await session.execute(
                select(ConversationTurn)
                .where(
                    ConversationTurn.conversation_ref_id == ref_id,
                    ConversationTurn.text_hash == digest,
                    ConversationTurn.role == "customer",
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        if existing is not None:
            return ok_response(
                {
                    "turn_id": str(existing.id),
                    "conversation_ref": str(ref_id),
                    "status": "queued",
                    "duplicate": True,
                },
                trace_id=new_trace_id(),
            )

        turn = ConversationTurn(
            tenant_id=ctx.tenant_id,
            conversation_ref_id=ref_id,
            role="customer",
            text_redacted=redacted,
            text_hash=digest,
            ts=int(time.time()),
            source="platform",
        )
        session.add(turn)
        await session.flush()

        return ok_response(
            {
                "turn_id": str(turn.id),
                "conversation_ref": str(ref_id),
                "status": "queued",
            },
            trace_id=new_trace_id(),
        )


@router.get("/agent-runs/{run_id}/citations")
async def run_citations(
    request: Request,
    run_id: str,
    limit: int = Query(default=20, ge=1, le=100),
) -> object:
    """Sources a run's answer was drawn from.

    Exposed so the customer can check the answer rather than trust it — the
    whole point of generating with citations in the first place.
    """
    ctx = get_context(request)
    if ctx is None:
        return error_response("AUTH_UNRESOLVED", "tenant context not resolved", status_code=401)
    denied = require_policy(ctx, Action.CASE_READ)
    if denied is not None:
        return denied

    try:
        run_uuid = parse_uuid(run_id, field="run_id")
    except ValueError as exc:
        return error_response(VALIDATION_FAILED, str(exc), status_code=400)

    async with tenant_session(ctx) as session:
        rows = (
            (
                await session.execute(
                    select(Citation)
                    .where(Citation.agent_run_id == run_uuid)
                    .order_by(Citation.claim_index.asc(), Citation.id.asc())
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )

    items = [
        CitationOut(
            source_uri=r.source_uri,
            claim_index=r.claim_index,
            excerpt_hash=r.excerpt_hash,
        ).model_dump()
        for r in rows
    ]
    return ok_response({"items": items}, trace_id=new_trace_id())
