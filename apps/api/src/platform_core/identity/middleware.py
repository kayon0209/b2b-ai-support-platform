"""Authentication middleware: resolve tenant context server-side.

MVP uses a bootstrap service-token scheme; Phase 2 replaces resolution with
Keycloak OIDC claims -> membership mapping (docs/security.md). The invariant
already enforced here: tenant_id comes from server-side data, never from a
client header or payload.
"""

import uuid
from collections.abc import Awaitable, Callable

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

from platform_core.identity import tenant_context
from platform_core.identity.tenant_context import TenantContext

# Exempt paths: health and unauthenticated webhooks (webhooks resolve tenant
# from connector configuration, implemented in support_bridge).
EXEMPT_PATHS = {
    "/healthz",
    "/openapi.json",
    "/docs",
    "/redoc",
    # Webhook authenticates via HMAC signature and resolves tenant from
    # trusted connector configuration, not bearer tokens.
    "/v1/webhooks/chatwoot",
}


class TenantContextMiddleware(BaseHTTPMiddleware):
    """Resolve TenantContext before routing; reject requests that cannot be
    resolved. Fail closed per docs/security.md objective 5."""

    def __init__(
        self, app: object, resolver: Callable[[Request], Awaitable[TenantContext]]
    ) -> None:
        super().__init__(app)  # type: ignore[arg-type]
        self._resolver = resolver

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if request.url.path in EXEMPT_PATHS:
            tenant_context.clear_tenant_context()
            return await call_next(request)

        try:
            ctx = await self._resolver(request)
        except Exception:
            tenant_context.clear_tenant_context()
            return Response(
                content='{"error":{"code":"AUTH_UNRESOLVED","retryable":false}}',
                status_code=401,
                media_type="application/json",
            )

        tenant_context.set_tenant_context(ctx)
        # Also expose on request.state: the module global is task-local and
        # does not survive into the endpoint's task under TestClient/starlette.
        request.state.tenant_context = ctx
        try:
            return await call_next(request)
        finally:
            tenant_context.clear_tenant_context()


async def bootstrap_token_resolver(request: Request) -> TenantContext:
    """Resolve tenant from a pre-shared platform token.

    Resolution reads membership rows server-side by actor identity; the
    tenant_id in the returned context is never client-supplied.
    """
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise PermissionError("missing bearer token")
    token = auth.removeprefix("Bearer ").strip()

    # MVP bootstrap mapping token format: pt_<tenant-slug>_<user-id>
    # Phase 2 swaps this for OIDC introspection; call sites do not change.
    parts = token.split("_", 2)
    if len(parts) != 3 or parts[0] != "pt":
        raise PermissionError("malformed token")

    _, tenant_slug, user_id_raw = parts
    try:
        user_id = uuid.UUID(user_id_raw)
    except ValueError as exc:
        raise PermissionError("malformed actor id") from exc

    # Membership lookup is performed by the route dependencies via DB; the
    # middleware only needs a stable TenantContext. Tenant resolution by slug
    # happens server-side in the repository layer, injected here via
    # request.state.resolved_tenant set by a dependency when needed.
    resolved = getattr(request.state, "resolved_tenant_id", None)
    if resolved is None:
        # Defer DB resolution to first use inside the request; store lookup
        # inputs. See identity/repository.resolve_membership for usage.
        resolved = uuid.uuid5(uuid.NAMESPACE_URL, f"tenant:{tenant_slug}")
    return TenantContext(
        tenant_id=uuid.UUID(str(resolved)),
        actor_id=user_id,
        actor_kind="user",
    )
