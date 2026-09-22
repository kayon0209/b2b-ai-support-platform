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

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import select

from platform_core.agent_runtime import chat_service
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
        branding = {
            "display_name": tenant.brand_display_name or tenant.name,
            "logo_url": tenant.brand_logo_url,
            "primary_color": tenant.brand_primary_color,
            "support_email": tenant.support_email,
        }

    external = body.visitor_id or str(uuid.uuid4())
    conversation_ref = conversation_ref_for(tenant_id, external)
    # The external id goes into the token as well as the derived ref. The worker
    # re-derives the ref from what we hand it, so only the raw id reproduces the
    # same conversation; passing the ref back would derive it a second time.
    token, expires_at = issue(
        tenant_id, conversation_ref, external, ttl_seconds=SESSION_TTL_SECONDS
    )
    return ok_response(
        {
            "token": token,
            "conversation_ref": str(conversation_ref),
            "expires_at": expires_at,
            "branding": branding,
        },
        trace_id=new_trace_id(),
    )


@router.get("/timeline")
async def timeline(
    request: Request,
    limit: int = Query(default=50, ge=1, le=200),
) -> object:
    """The visitor's own exchange, oldest first."""
    claim = _claim(request)
    if not isinstance(claim, VisitorClaim):
        return claim

    async with tenant_session(_ctx_for(claim)) as session:
        items = await chat_service.read_timeline(
            session, ref_id=claim.conversation_ref, limit=limit
        )
    return ok_response({"items": items}, trace_id=new_trace_id())


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


@router.post("/messages")
async def post_message(request: Request, body: MessageIn) -> object:
    """Accept a customer question and queue the run that answers it.

    One call rather than the operator surface's two (persist, then queue): the
    customer page should not have to know that turns and runs are separate
    things, and a customer who loses the second call would leave a question
    stored but never answered.
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
        turn, duplicate = await chat_service.append_customer_turn(
            session,
            tenant_id=ctx.tenant_id,
            ref_id=claim.conversation_ref,
            text=body.text,
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
            **queued,
        },
        trace_id=trace_id,
    )
