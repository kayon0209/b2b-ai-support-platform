"""Shared HTTP helpers for the platform API (docs/api-contracts.md).

Every JSON endpoint in this service returns the same two shapes so clients
(and the admin web app) can handle them generically:

- success: the endpoint's own payload plus `trace_id`
- error:   {"error": {code, message, retryable, details}, "trace_id"}

Errors use stable machine-readable codes. Stack traces, prompts,
credentials and raw provider responses are never exposed (docs/security.md).

This module deliberately contains no business logic: it is the shared
vocabulary that routers and the policy gate agree on.
"""

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from platform_core.config import get_settings
from platform_core.db import session_scope_with_url
from platform_core.identity import tenant_context
from platform_core.identity.tenant_context import TenantContext, apply_rls_tenant
from platform_policy import Action, Decision, PolicyEngine, Principal, Resource

# --- Error codes -----------------------------------------------------------
#
# Kept as constants so routers, tests and the admin UI cannot drift on
# spelling. Codes are part of the public contract: renaming one is a
# breaking change.

AUTH_UNRESOLVED = "AUTH_UNRESOLVED"
POLICY_DENIED = "POLICY_DENIED"
NOT_FOUND = "NOT_FOUND"
VALIDATION_FAILED = "VALIDATION_FAILED"
IDEMPOTENCY_KEY_REQUIRED = "IDEMPOTENCY_KEY_REQUIRED"
PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
INTERNAL_ERROR = "INTERNAL_ERROR"

# Domain-specific codes referenced by docs/api-contracts.md
CASE_NOT_FOUND = "CASE_NOT_FOUND"
CASE_TRANSITION_NOT_ALLOWED = "CASE_TRANSITION_NOT_ALLOWED"
CASE_VERSION_CONFLICT = "CASE_VERSION_CONFLICT"

RETRYABLE_CODES = frozenset({PROVIDER_UNAVAILABLE, INTERNAL_ERROR})


def error_response(
    code: str,
    message: str = "",
    *,
    status_code: int = 400,
    details: dict[str, Any] | None = None,
    retryable: bool | None = None,
    trace_id: str = "",
) -> JSONResponse:
    """Build the documented error envelope.

    `retryable` defaults from the code table so callers do not have to
    remember which failures are worth retrying.
    """
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "code": code,
                "message": message or code,
                "retryable": RETRYABLE_CODES.__contains__(code) if retryable is None else retryable,
                "details": details or {},
            },
            "trace_id": trace_id,
        },
    )


def ok_response(payload: dict[str, Any], *, trace_id: str = "") -> dict[str, Any]:
    """Attach `trace_id` to a success payload.

    Every response exposes trace_id (docs/api-contracts.md), so this is
    applied centrally rather than per endpoint.
    """
    body = dict(payload)
    body["trace_id"] = trace_id or new_trace_id()
    return body


def new_trace_id() -> str:
    return str(uuid.uuid4())


# --- Request context -------------------------------------------------------


def get_context(request: Request) -> TenantContext | None:
    """Read the resolved tenant context.

    The middleware writes to request.state because the module global is
    task-local and does not survive into the endpoint's task under
    starlette/TestClient.
    """
    ctx = getattr(request.state, "tenant_context", None)
    if isinstance(ctx, TenantContext):
        return ctx
    try:
        return tenant_context.get_tenant_context()
    except tenant_context.TenantContextError:
        return None


def principal_from_context(ctx: TenantContext) -> Principal:
    """Build the policy principal from server-resolved context only.

    Never construct a Principal from request payload fields: a caller must
    not be able to grant themselves a role or a tenant.
    """
    return Principal(
        tenant_id=str(ctx.tenant_id),
        actor_id=str(ctx.actor_id) if ctx.actor_id else "",
        role=ctx.role or "unknown",
    )


def check_policy(
    ctx: TenantContext, action: Action, *, resource: Resource | None = None
) -> Decision:
    """Evaluate one action for the request principal."""
    engine = PolicyEngine()
    return engine.check(principal_from_context(ctx), action, resource).decision


def require_policy(ctx: TenantContext, action: Action) -> JSONResponse | None:
    """Return a 403 envelope when denied, else None.

    Returning None means "allowed": callers keep the happy path flat.
    """
    if check_policy(ctx, action) != Decision.ALLOW:
        return error_response(
            POLICY_DENIED,
            f"action {action.value} is not permitted for this principal",
            status_code=403,
        )
    return None


def require_idempotency_key(request: Request) -> str | None:
    """Read Idempotency-Key. Every write command must carry one."""
    return request.headers.get("Idempotency-Key") or None


@asynccontextmanager
async def tenant_session(ctx: TenantContext) -> AsyncIterator[AsyncSession]:
    """Session bound to the non-bypass app role with RLS applied.

    The bootstrap owner role is a superuser and would bypass RLS entirely,
    so request handling always connects as `platform_app`. The tenant is
    bound per transaction as additional defence on top of the query
    filters.
    """
    settings = get_settings()
    app_url = _app_role_url(settings.database_url)
    async with session_scope_with_url(app_url) as session:
        await apply_rls_tenant(session, ctx)
        yield session


def _app_role_url(database_url: str) -> str:
    """Swap the bootstrap owner for the non-bypass application role."""
    return database_url.replace("platform:platform@", "platform_app:platform_app@")


def parse_uuid(value: str, *, field: str) -> uuid.UUID:
    """Parse a path/body identifier, raising a ValueError for bad input.

    Callers convert this into a 400 envelope; keeping the parse here means
    the error message names the offending field.
    """
    try:
        return uuid.UUID(value)
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError(f"{field} is not a valid uuid") from exc
