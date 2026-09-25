"""The visitor-facing chat surface: `/v1/support`.

Why this module exists
----------------------
`/v1/customer/*` reads and writes the same conversation, but it authenticates
with an *operator* bearer token and requires `CASE_READ` / `CASE_UPDATE`. A real
customer holds neither, so opening the platform's chat page returned 401 and the
UI rendered that as an empty conversation (audit F-1/F-2). ADR 0010 responded by
declaring the customer experience out of scope; ADR 0011 revisits that, because
"the customer cannot open the chat window" is not a scope boundary a product can
ship on.

How a visitor is authorized
---------------------------
`POST /sessions` is the only unauthenticated route here. It resolves a tenant
from a slug -- `tenants` is global reference data with no RLS, so this read is
legitimate before any binding exists -- mints a conversation, and returns a
signed token bound to that (tenant, conversation) pair (`visitor_token`).

Every other route requires that token and derives the tenant *and* the
conversation from it. A path or body value never selects either: the request
carries no conversation id at all, so a visitor cannot address someone else's
conversation even by guessing. That is the whole authorization model, and it is
deliberately narrower than a role.

What bounds the spend
---------------------
Issuing a session creates no work; the first run is only queued by `POST
/messages`, which goes through the same quota and queue-depth gates as an
operator-initiated run (`chat_service.queue_agent_run`). So "anyone may open the
window" is not "anyone may spend without limit": the tenant's monthly run quota
and the global queue cap are what actually bound it, and both already existed.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from platform_core.agent_runtime import chat_service
from platform_core.agent_runtime.hours import is_open, opening_hour
from platform_core.agent_runtime.language import conversation_is_chinese
from platform_core.api import (
    AUTH_UNRESOLVED,
    IDEMPOTENCY_KEY_REQUIRED,
    VALIDATION_FAILED,
    error_response,
    new_trace_id,
    ok_response,
    require_idempotency_key,
    tenant_session,
)
from platform_core.identity import lease_service
from platform_core.identity.branding import sanitize_display_name
from platform_core.identity.models import Tenant, TenantStatus
from platform_core.identity.tenant_context import TenantContext
from platform_core.support_bridge.conversation_ref import conversation_ref_for
from platform_core.support_bridge.visitor_token import (
    VisitorClaim,
    VisitorTokenError,
    issue,
    verify,
)

router = APIRouter(prefix="/v1/support", tags=["support"])

# Long enough to leave a tab open through a support conversation, short enough
# that a leaked token is not a permanent key. Independent of the 90-day
# `conversation_turn_days` retention sweep, which bounds the *rows*, not the
# credential.
SESSION_TTL_SECONDS = 12 * 60 * 60


class SessionIn(BaseModel):
    tenant_slug: str = Field(min_length=1, max_length=63)
    # The page keeps this in localStorage. Supplying it again resumes the same
    # conversation; omitting it starts a new one. It is an opaque handle, not an
    # identity -- the token is what authorizes, and it is issued per session.
    visitor_id: str | None = Field(default=None, max_length=64)


def _bearer(request: Request) -> str:
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise VisitorTokenError("missing bearer token")
    return auth.removeprefix("Bearer ").strip()


def _claim(request: Request) -> VisitorClaim | object:
    """Resolve the visitor token, or return the 401 to send back."""
    try:
        return verify(_bearer(request))
    except VisitorTokenError as exc:
        # One message for every failure mode: absent, malformed, forged and
        # expired are indistinguishable to the caller, so a probe learns
        # nothing from the difference.
        return error_response(
            AUTH_UNRESOLVED,
            f"visitor token rejected: {type(exc).__name__}",
            status_code=401,
            trace_id=new_trace_id(),
        )


def _ctx_for(claim: VisitorClaim) -> TenantContext:
    """A context for RLS. No actor and no role: a visitor has no membership.

    `actor_kind="customer"` is honest labelling for the audit trail. The absence
    of a role is what keeps this narrow -- every operator policy gate denies a
    role-less context, so this token cannot reach an operator endpoint even if a
    route were mis-wired.
    """
    return TenantContext(
        tenant_id=claim.tenant_id,
        actor_id=None,
        actor_kind="customer",
        role=None,
    )


async def _reject_if_ended(claim: VisitorClaim, session: AsyncSession) -> object | None:
    """Refuse a claim whose conversation has been ended, or None if it is live.

    The signature check in `verify` cannot see this: it is a pure function, and
    a twelve-hour-old token is still a validly signed twelve-hour-old token.
    The revocation record is the only thing that knows the customer closed the
    window, so every authenticated customer endpoint has to ask - which is why
    this is one helper called from four places rather than four copies that can
    drift, and why a new endpoint that forgets it is a bug the tests below
    cannot see.

    The response is the same 401 an invalid token gets, with the same opaque
    message. A caller must not be able to distinguish "this token was never
    valid" from "this token was withdrawn", or ending a session would confirm
    that a token once existed.
    """
    from platform_core.support_bridge.visitor_revocation import async_is_revoked

    if not await async_is_revoked(
        session,
        tenant_id=claim.tenant_id,
        token_jti=claim.token_jti,
    ):
        return None
    return error_response(
        AUTH_UNRESOLVED,
        f"visitor token rejected: {VisitorTokenError.__name__}",
        status_code=401,
        trace_id=new_trace_id(),
    )


@router.post("/sessions")
async def open_session(request: Request, body: SessionIn) -> object:
    """Open (or resume) a visitor conversation and return its token."""
    from platform_core.db import app_role_url, session_scope_with_url

    async with session_scope_with_url(app_role_url()) as session:
        tenant = (
            await session.execute(select(Tenant).where(Tenant.slug == body.tenant_slug))
        ).scalar_one_or_none()
        if tenant is None or tenant.status != TenantStatus.ACTIVE:
            # Same message for unknown and suspended: otherwise this endpoint
            # enumerates tenants.
            return error_response(
                VALIDATION_FAILED,
                "unknown tenant",
                status_code=404,
                trace_id=new_trace_id(),
            )
        tenant_id = tenant.id
        branding = await _branding_for(session, tenant_id)
        # Read inside the same scope as the tenant row: `is_open` touches
        # settings, not the database, but the window is part of the same
        # "what the customer sees on arrival" answer.
        support_window = {"open": is_open(), "opens_at_hour": opening_hour()}

    external = body.visitor_id or str(uuid.uuid4())
    # The one place a visitor conversation's identity is minted, from the
    # channel id the visitor holds. Everything downstream - the worker, the
    # operator's `/v1/conversations` surfaces, the agent's reply - reads the
    # ref this returns and never derives one again; that is the whole rule
    # (`support_bridge.conversation_ref`).
    conversation_ref = conversation_ref_for(tenant_id, external)
    # The external id still goes into the token as well as the ref, because
    # the worker re-derives from the payload it receives. A caller that later
    # needs a human to answer addresses the conversation by `conversation_ref`
    # - not by this id, which the platform surfaces never accept.
    token, expires_at = issue(
        tenant_id, conversation_ref, external, ttl_seconds=SESSION_TTL_SECONDS
    )
    return ok_response(
        {
            "token": token,
            "conversation_ref": str(conversation_ref),
            "expires_at": expires_at,
            "branding": branding,
            "support_window": support_window,
        },
        trace_id=new_trace_id(),
    )


@router.post("/sessions/end")
async def end_session(request: Request) -> object:
    """Withdraw this conversation's token.

    Takes no body and no path parameter on purpose. The only thing that
    identifies the conversation is the token in the header, so a caller can
    close exactly the session they hold and nothing else - a body-supplied
    `conversation_ref` would let anyone with any valid visitor token close a
    stranger's chat.

    The withdrawal is per *credential*: the token this request carries, named by
    its jti. That is the only reading that survives contact with a customer who
    ends a session and comes back - `POST /v1/support/sessions` mints a fresh
    token for the same conversation, and it works, while the ended one stays
    dead. Revoking the conversation instead would make returning impossible, and
    "end" is not "delete".

    Idempotent, because the page retries and a double-click must not look like
    a failure. Ending is not deleting: the transcript stays, which is what a
    support history has to be, and what the retention job is for.
    """
    claim = _claim(request)
    if not isinstance(claim, VisitorClaim):
        return claim

    from platform_core.support_bridge.visitor_revocation import async_record_revocation

    if claim.token_jti is None:
        # A token minted before revocation existed cannot be withdrawn, because
        # it carries nothing to name. Refusing it here would be the only honest
        # answer, and it expires on its own within the hour.
        return error_response(
            AUTH_UNRESOLVED,
            "visitor token rejected: VisitorTokenError",
            status_code=401,
            trace_id=new_trace_id(),
        )

    async with tenant_session(_ctx_for(claim)) as session:
        await async_record_revocation(
            session,
            tenant_id=claim.tenant_id,
            token_jti=claim.token_jti,
            conversation_ref=claim.conversation_ref,
        )
    return ok_response({"ended": True, "conversation_ref": str(claim.conversation_ref)})


@router.get("/timeline")
async def timeline(
    request: Request,
    limit: int = Query(default=50, ge=1, le=200),
) -> object:
    """The visitor's own exchange, oldest first.

    Carries the conversation's ownership alongside the turns, because the turns
    alone cannot express the one state a customer most needs to understand: the
    AI is no longer the one who will answer. I didn't say anything and nobody
    said anything" and "a colleague has this and will reply" are the same
    timeline and opposite experiences - the second one is why the customer keeps
    the window open.
    """
    claim = _claim(request)
    if not isinstance(claim, VisitorClaim):
        return claim

    async with tenant_session(_ctx_for(claim)) as session:
        ended = await _reject_if_ended(claim, session)
        if ended is not None:
            return ended
        items = await chat_service.read_timeline(
            session, ref_id=claim.conversation_ref, limit=limit
        )
        owner, mode = await lease_service.current_owner(
            session,
            tenant_id=claim.tenant_id,
            conversation_ref_id=claim.conversation_ref,
        )
        branding = await _branding_for(session, claim.tenant_id)
        rating = await _rating_for(session, claim)
        rating_eligible = owner == "closed" and await chat_service.has_human_reply(
            session, tenant_id=claim.tenant_id, ref_id=claim.conversation_ref
        )
    return ok_response(
        {
            "items": items,
            "conversation": {"owner": owner, "mode": mode},
            # Three things the window needs on *every* load, not only on the one
            # that opens the session: the tenant's own name and colour, and
            # whether anyone is there. Returning them here rather than from
            # `/sessions` alone is what stops a returning customer - the one who
            # reloads, or comes back tomorrow - from losing the brand name to the
            # hardcoded fallback, which is exactly what happened before
            # (measured 2026-09-23).
            "branding": branding,
            "support_window": {
                "open": is_open(),
                "opens_at_hour": opening_hour(),
            },
            # The score this conversation already has, so a reload shows the
            # rating the customer gave rather than an empty survey they have
            # already answered.
            "rating": rating,
            "rating_eligible": rating_eligible,
        },
        trace_id=new_trace_id(),
    )


async def _rating_for(session: AsyncSession, claim: VisitorClaim) -> int | None:
    """This conversation's recorded satisfaction score, or None.

    A read, so it is allowed to return None for "not asked yet" and "asked but
    unanswered" alike - the surface treats both the same way, and a conversation
    that is neither handed off nor rated shows no survey at all.
    """
    from platform_core.support_bridge.csat_models import CsatResponse

    row = (
        await session.execute(
            select(CsatResponse.score).where(
                CsatResponse.tenant_id == claim.tenant_id,
                CsatResponse.conversation_ref_id == claim.conversation_ref,
            )
        )
    ).scalar_one_or_none()
    return int(row) if row is not None else None


async def _branding_for(session: AsyncSession, tenant_id: uuid.UUID) -> dict[str, object]:
    """The tenant's public branding, for a surface that already holds a token.

    Small enough to inline, but it exists as a function because two responses
    need it and a copy that drifted would show one customer two different brand
    names on the same page load.
    """
    tenant = (
        await session.execute(select(Tenant).where(Tenant.id == tenant_id))
    ).scalar_one_or_none()
    if tenant is None:
        return {}
    return {
        "display_name": sanitize_display_name(tenant.brand_display_name) or tenant.name,
        "logo_url": tenant.brand_logo_url,
        "primary_color": tenant.brand_primary_color,
        "support_email": tenant.support_email,
    }


# --- When the AI is not the one who will answer ---------------------------
#
# Served by two routes: `GET /timeline` returns it as the conversation state,
# and `POST /messages` writes it as a `system` turn when it declines to queue an
# answer.
#
# Two texts rather than one, because "a person is on it" and "waiting for one"
# are different situations and the customer acts differently on each - waiting
# quietly versus expecting a name. Blurring them is the same class of error as
# reporting a missing record as a systems outage. Neither promises a time: how
# long a queue takes is not something this platform knows.
# How far back to look when deciding the language of a handoff notice. Bounded
# because this is a language sniff, not a transcript: one Chinese message
# anywhere in recent history settles it, and reading a whole conversation to
# answer a two-way question would make the notice path the slowest thing on
# `POST /messages`.
_NOTICE_HISTORY_TURNS = 50


def _handed_off_notice(owner_type: str, question: str, history: Sequence[str] = ()) -> str:
    """The notice a customer gets when a person, not the AI, owns the thread.

    The language comes from the **conversation**, not from `question` alone.
    Passing only the current message meant a customer who replied to the
    platform's own request for an order number ("请把单号一起告诉我" ->
    `SO-9001`) got the English text, because that message carries no CJK
    character - measured 2026-09-23, and it is the same class of error as
    answering a Chinese question in English. `history` is the customer's
    earlier messages; any Chinese among them makes the conversation Chinese.
    """
    chinese = conversation_is_chinese((question, *history))
    if owner_type == "human":
        return (
            "这条对话已由人工同事接手，他们会看到您刚发的消息。"
            if chinese
            else "A human colleague has taken over this conversation and will see your message."
        )
    return (
        "这条对话正在等待人工同事接入，您发的消息他们会看到，请稍候。"
        if chinese
        else "This conversation is waiting for a human colleague to pick it "
        "up. They will see your message."
    )


class MessageIn(BaseModel):
    text: str = Field(min_length=1, max_length=4000)


class VerifyIn(BaseModel):
    # Feature list 2.2: the proof an un-logged-in customer can offer. The
    # weakest demonstration-grade pair - order number plus the phone tail on
    # file - and the provider decides whether it matches.
    order_id: str = Field(min_length=1, max_length=63)
    phone_tail: str = Field(min_length=4, max_length=8)


@router.post("/verify")
async def verify_ownership(request: Request, body: VerifyIn) -> object:
    """Bind the session to the account that owns `order_id` (feature 2.2/2.5).

    Success re-issues the token with the account on it, so the proof lives for
    exactly as long as the credential does. Failure modes - unknown order,
    wrong tail, provider that cannot answer - are the **same response**, for
    the same reason `/sessions` does not distinguish unknown tenants: a diff
    here is an order-number oracle.

    The ownership check is the *provider's* to make (`verify_ownership` on the
    executor). This endpoint never compares the tail itself; it only refuses
    when the provider cannot prove ownership, which is the fail-closed
    direction.
    """
    claim = _claim(request)
    if not isinstance(claim, VisitorClaim):
        return claim

    async with tenant_session(_ctx_for(claim)) as session:
        ended = await _reject_if_ended(claim, session)
        if ended is not None:
            return ended
        from platform_core.tool_gateway.registry import ConnectorExecutorResolver

        executors = await ConnectorExecutorResolver(
            session,
            tenant_id=claim.tenant_id,
            ctx=_ctx_for(claim),
            trace_id=new_trace_id(),
        ).executors_for(["order.get_status"])
        executor = executors.get("order.get_status")
        if executor is None or not hasattr(executor, "verify_ownership"):
            # No connector to ask: fail closed rather than proving nothing and
            # calling it a success.
            return error_response(
                VALIDATION_FAILED,
                "ownership verification is unavailable for this tenant",
                status_code=503,
                trace_id=new_trace_id(),
            )
        account = await executor.verify_ownership(
            "order.get_status", body.order_id.strip(), body.phone_tail.strip()
        )

    if not account:
        return error_response(
            VALIDATION_FAILED,
            "we could not verify that order with the details given",
            status_code=403,
            trace_id=new_trace_id(),
        )

    token, expires_at = issue(
        claim.tenant_id,
        claim.conversation_ref,
        claim.external_ref,
        ttl_seconds=SESSION_TTL_SECONDS,
        account=account,
    )
    return ok_response(
        {
            "token": token,
            "expires_at": expires_at,
            "verified_account": account,
        },
        trace_id=new_trace_id(),
    )


class RatingIn(BaseModel):
    score: int = Field(ge=1, le=5)
    comment: str | None = Field(default=None, max_length=500)


@router.post("/rating")
async def rate_conversation(request: Request, body: RatingIn) -> object:
    """Record this conversation's satisfaction score (feature 7.10).

    The customer-facing half of a capability that had none. `csat.record_response`
    and `csat_summary` were written, migrated (0044) and tested, and **called by
    nothing** - so the platform asked no one how it did, and the satisfaction
    number the operations dashboard needs did not exist. Measured 2026-09-23 by
    grepping for production callers; there were none.

    A handoff starts human service; it does not finish it. A score is accepted
    only after the assigned human closes the conversation, and only when that
    person actually replied. The same eligibility is returned by /timeline.

    A second score replaces the first rather than adding a row, so a re-tap is
    not a second opinion (`record_response` says the same thing, and 0044's
    unique constraint enforces it).
    """
    claim = _claim(request)
    if not isinstance(claim, VisitorClaim):
        return claim

    from platform_core.support_bridge import csat

    idem = require_idempotency_key(request)
    if not idem:
        return error_response(
            IDEMPOTENCY_KEY_REQUIRED,
            "rating requires an Idempotency-Key header",
            status_code=400,
        )

    try:
        async with tenant_session(_ctx_for(claim)) as session:
            ended = await _reject_if_ended(claim, session)
            if ended is not None:
                return ended
            owner, _mode = await lease_service.current_owner(
                session, tenant_id=claim.tenant_id, conversation_ref_id=claim.conversation_ref
            )
            if owner != "closed" or not await chat_service.has_human_reply(
                session, tenant_id=claim.tenant_id, ref_id=claim.conversation_ref
            ):
                return error_response(
                    "CSAT_NOT_READY", "the interaction has not finished", status_code=409
                )
            row = await csat.record_response(
                session,
                tenant_id=claim.tenant_id,
                conversation_ref_id=claim.conversation_ref,
                score=body.score,
                comment=(body.comment or "").strip() or None,
                channel="web",
            )
            score = row.score
    except csat.CsatError as exc:
        # A refused score is the caller's mistake, so it is a 400 with the
        # service's own reason - not a 200 carrying an error body, which is the
        # shape five routers here used to have.
        return error_response(
            VALIDATION_FAILED,
            exc.detail or exc.code,
            status_code=400,
            trace_id=new_trace_id(),
        )

    return ok_response({"score": score}, trace_id=new_trace_id())


@router.post("/messages")
async def post_message(request: Request, body: MessageIn) -> object:
    """Accept a customer question and queue the run that answers it.

    One call rather than the operator surface's two (persist, then queue): the
    customer page should not have to know that turns and runs are separate
    things, and a customer who loses the second call would leave a question
    stored but never answered.

    **It does not queue when the AI is not the owner**, and that is the point of
    the ownership check below rather than an optimisation. A conversation handed
    to a person - or to the queue - can never receive an AI reply, because the
    pre-send lease gate refuses one. Queuing anyway meant the model was paid
    for, the draft was generated, and then discarded, with nothing written to
    the conversation: measured 2026-09-23, a customer who asked a follow-up
    question after a handoff saw no answer, no notice and no error, forever. So
    the question is still recorded, the customer is told who has it, and no run
    is spent on a reply that could not be delivered.
    """
    claim = _claim(request)
    if not isinstance(claim, VisitorClaim):
        return claim

    idem = require_idempotency_key(request)
    if not idem:
        return error_response(
            IDEMPOTENCY_KEY_REQUIRED,
            "posting a message requires an Idempotency-Key header",
            status_code=400,
        )

    ctx = _ctx_for(claim)
    trace_id = new_trace_id()

    async with tenant_session(ctx) as session:
        ended = await _reject_if_ended(claim, session)
        if ended is not None:
            return ended
        turn, duplicate = await chat_service.append_customer_turn(
            session,
            tenant_id=ctx.tenant_id,
            ref_id=claim.conversation_ref,
            text=body.text,
        )

        owner, mode = await lease_service.locked_owner(
            session,
            tenant_id=ctx.tenant_id,
            conversation_ref_id=claim.conversation_ref,
        )
        if owner == "closed":
            await session.rollback()
            return error_response(
                "CONVERSATION_CLOSED",
                "this conversation has ended; start a new session",
                status_code=409,
                trace_id=trace_id,
            )
        if owner != "ai":
            if owner == "human":
                await lease_service.mark_customer_replied(
                    session, tenant_id=ctx.tenant_id, conversation_ref_id=claim.conversation_ref
                )
                mode = "HUMAN_ACTIVE"
            # The question is kept either way, so declining to answer the AI's
            # way is not data loss - a person will read it. `append_system_turn`
            # dedupes, so two messages in a row do not produce two copies of the
            # same sentence.
            #
            # The notice's language is decided from the whole conversation, not
            # from `body.text`. A customer who has written Chinese is writing
            # Chinese, and a reply consisting only of an order number - which is
            # exactly what the platform asked for - must not switch the notice
            # to English. Read through the same function the timeline uses, so
            # the notice sees what the customer sees.
            history = [
                row["text"]
                for row in await chat_service.read_timeline(
                    session, ref_id=claim.conversation_ref, limit=_NOTICE_HISTORY_TURNS
                )
                if row.get("role") == "customer"
            ]
            await chat_service.append_system_turn(
                session,
                tenant_id=ctx.tenant_id,
                ref_id=claim.conversation_ref,
                text=_handed_off_notice(owner, body.text, history),
            )
            return ok_response(
                {
                    "turn_id": str(turn.id),
                    "conversation_ref": str(claim.conversation_ref),
                    "duplicate": duplicate,
                    "status": "waiting_for_human",
                    "conversation": {"owner": owner, "mode": mode},
                },
                trace_id=trace_id,
            )

        try:
            queued = await chat_service.queue_agent_run(
                session,
                ctx=ctx,
                conversation_ref_id=claim.conversation_ref,
                external_ref=claim.external_ref,
                trigger_message_ref=str(turn.id),
                idem=idem,
                mode="customer_reply",
                trace_id=trace_id,
                # Feature 2.5: "" = anonymous visitor (the worker's ownership
                # gate must fire); a proven account rides through to the run.
                verified_account=claim.account or "",
            )
        except chat_service.QueueRefused as refused:
            # The turn is persisted either way, so a refusal is not data loss:
            # the customer can retry and the question is still there.
            return refused.response

    return ok_response(
        {
            "turn_id": str(turn.id),
            "conversation_ref": str(claim.conversation_ref),
            "duplicate": duplicate,
            "status": "queued",
            "conversation": {"owner": owner, "mode": mode},
            **queued,
        },
        trace_id=trace_id,
    )
