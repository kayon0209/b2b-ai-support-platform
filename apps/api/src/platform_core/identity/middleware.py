"""Authentication middleware: resolve tenant context server-side.

MVP uses a bootstrap service-token scheme; Phase 2 replaces resolution with
Keycloak OIDC claims -> membership mapping (docs/security.md). The invariant
already enforced here: tenant_id comes from server-side data, never from a
client header or payload.

The token names a tenant *slug*, not a tenant id. The slug is only a lookup
key: the authoritative tenant id and the caller's role are read from the
`tenants` / `memberships` tables. A token whose slug or membership does not
resolve is rejected - it never yields a synthesised tenant or a role-less
context, because a role-less context would be denied by every policy gate
and would look like a working login that silently cannot do anything.
"""

import logging
import uuid
from collections.abc import Awaitable, Callable
from typing import Any, cast

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

from platform_core.identity import tenant_context
from platform_core.identity.tenant_context import TenantContext

logger = logging.getLogger(__name__)

# Exempt paths: health and unauthenticated webhooks (webhooks resolve tenant
# from connector configuration, implemented in support_bridge).
EXEMPT_PATHS = {
    "/healthz",
    # Prometheus scrapes on a timer and cannot present a bearer token. The
    # endpoint is safe to exempt only because no metric it serves carries a
    # tenant-identifying label; see observability_router for the full
    # reasoning and the APP_METRICS_ENABLED exposure switch.
    "/metrics",
    "/openapi.json",
    "/docs",
    "/redoc",
    # Webhook authenticates via HMAC signature and resolves tenant from
    # trusted connector configuration, not bearer tokens.
    "/v1/webhooks/chatwoot",
    # Invite acceptance is how someone *joins* a tenant, so the caller has no
    # membership and therefore no bearer token yet. The single-use token in
    # the request body is the credential: the handler resolves the tenant from
    # it through a SECURITY DEFINER function and grants only the invited role.
    # Without this exemption accept is unreachable in the real app (401 before
    # the handler runs) even though the handler is correct.
    "/v1/identity/members/accept",
    # The tenant's public branding page, resolved from the Host header. It
    # carries no token by design and returns public branding only - no
    # tenant id, no configuration.
    "/v1/public/branding",
}

# Prefix exemptions, for routes with a path parameter.
#
# A signed webhook carries no bearer token - the signature *is* the
# authentication - and the connector webhook's path ends in the connector id,
# which exact matching cannot cover. The prefix is deliberately narrow
# (`/v1/webhooks/`): everything under it must verify a signature, and adding a
# route there without one would create an unauthenticated endpoint. Stating
# that here means whoever adds the next webhook route sees the obligation
# instead of inheriting a silent grant.
EXEMPT_PREFIXES = ("/v1/webhooks/",)


def is_exempt(path: str) -> bool:
    """Whether a path skips bearer-token resolution."""
    return path in EXEMPT_PATHS or path.startswith(EXEMPT_PREFIXES)


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
        if is_exempt(request.url.path):
            tenant_context.clear_tenant_context()
            return await call_next(request)

        try:
            ctx = await self._resolver(request)
        except Exception as exc:
            tenant_context.clear_tenant_context()
            # Log the cause. A bare 401 with no server-side trace makes an
            # authentication outage indistinguishable from a bad token, and
            # the only way to tell them apart would be to reproduce it under
            # a debugger. Path + exception type only: never the token.
            logger.warning(
                "auth resolution failed for %s %s: %s: %s",
                request.method,
                request.url.path,
                type(exc).__name__,
                exc,
            )
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
    """Resolve tenant + role from a pre-shared platform token.

    Resolution reads the tenant and membership rows server-side by actor
    identity; nothing the client sent is trusted beyond the token itself.
    """
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise PermissionError("missing bearer token")
    token = auth.removeprefix("Bearer ").strip()

    # MVP bootstrap token format: pt_<tenant-slug>_<user-id>
    # Phase 2 swaps this for OIDC introspection; call sites do not change.
    parts = token.split("_", 2)
    if len(parts) != 3 or parts[0] != "pt":
        raise PermissionError("malformed token")

    _, tenant_slug, user_id_raw = parts
    try:
        user_id = uuid.UUID(user_id_raw)
    except ValueError as exc:
        raise PermissionError("malformed actor id") from exc

    return await resolve_membership(tenant_slug, user_id)


async def resolve_membership(tenant_slug: str, user_id: uuid.UUID) -> TenantContext:
    """Resolve slug + user to a role-carrying TenantContext.

    Two reads, because of how RLS is laid out:

    1. `tenants` is global reference data (no RLS), so the slug -> id lookup
       works before any context exists.
    2. `memberships` is tenant-owned and FORCE-RLS'd, so the membership read
       must happen *inside* a transaction that has already bound
       `app.tenant_id` to the id discovered in step 1. Without that binding
       the policy predicate compares against NULL and returns zero rows -
       which is precisely why an unbound lookup can never be used to resolve
       a role.

    Any failure (unknown slug, suspended tenant, no active membership) raises
    PermissionError, which the middleware maps to 401. A role-less context is
    never returned: it would pass authentication and then be denied by every
    policy gate, disguising a broken login as a working one.
    """
    from platform_core.config import get_settings
    from platform_core.db import session_scope_with_url
    from platform_core.identity.repository import load_context

    settings = get_settings()
    app_url = _app_database_url(settings.database_url)
    async with session_scope_with_url(app_url) as session:
        try:
            return await load_context(session, tenant_slug, user_id)
        except Exception as exc:
            logger.warning("membership resolution traceback", exc_info=exc)
            raise PermissionError(f"membership unresolvable: {type(exc).__name__}") from exc


def _app_database_url(database_url: str) -> str:
    """Swap the bootstrap superuser for the non-bypass application role.

    The bootstrap owner is a superuser and would silently bypass RLS, so
    every request-path read uses the RLS-bound role.
    """
    return database_url.replace("platform:platform@", "platform_app:platform_app@")


# --- Authentication strategy selection ------------------------------------
#
# Two implementations, one contract (Request -> TenantContext, or raise).
# Selection happens once at startup so a misconfigured deployment fails on
# boot rather than per request, and so an unsigned token can never be accepted
# just because no realm happened to be reachable.


async def oidc_token_resolver(request: Request) -> TenantContext:
    """Verify a Keycloak access token and map it to a membership.

    Signature, issuer, audience and expiry are all checked against the realm
    JWKS before any database read. The tenant is then taken from
    `external_identities` + `memberships`, never from the token, so a token
    cannot select its own tenant.
    """
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise PermissionError("missing bearer token")
    token = auth.removeprefix("Bearer ").strip()

    verifier, resolver = _oidc_dependencies()
    try:
        claims = verifier.verify(token)
    except Exception as exc:
        # Verification failure is a 401, not a 500. The reason is logged
        # server-side; the client is not told which check failed.
        logger.warning("oidc verification failed: %s", type(exc).__name__)
        raise PermissionError("token verification failed") from exc

    try:
        # `resolver` is imported lazily (Any); the resolver contract is
        # tenant_context(claims) -> TenantContext.
        return cast(TenantContext, await resolver.tenant_context(claims))
    except PermissionError:
        raise
    except Exception as exc:
        logger.warning("oidc membership mapping failed: %s", type(exc).__name__)
        raise PermissionError("membership unresolvable") from exc


_oidc_cache: tuple[object, object] | None = None


def _oidc_dependencies() -> tuple[Any, Any]:
    """Build (verifier, membership resolver) once, on first use."""
    global _oidc_cache
    if _oidc_cache is not None:
        return _oidc_cache

    from sqlalchemy.ext.asyncio import async_sessionmaker

    from platform_core.config import get_settings
    from platform_core.db import create_engine
    from platform_core.identity.oidc import MembershipResolver, OidcVerifier

    settings = get_settings()
    if settings.oidc_issuer is None:  # pragma: no cover - guarded by build_resolver
        raise PermissionError("oidc issuer not configured")

    verifier = OidcVerifier(
        issuer=settings.oidc_issuer,
        audience=settings.oidc_audience,
        jwks_cache_seconds=settings.oidc_jwks_cache_seconds,
    )
    # The app role (not the superuser) so RLS applies to the lookup.
    engine = create_engine(_app_database_url(settings.database_url))
    resolver = MembershipResolver(async_sessionmaker(engine, expire_on_commit=False))
    _oidc_cache = (verifier, resolver)
    return verifier, resolver


def build_resolver() -> Callable[[Request], Awaitable[TenantContext]]:
    """Choose the authentication path for this deployment.

    OIDC wins whenever an issuer is configured. The bootstrap scheme is only
    selected when it has been explicitly enabled (and `get_settings` has
    already refused to start if that happened outside local/test), so the
    insecure path cannot be reached by omission.
    """
    from platform_core.config import get_settings

    settings = get_settings()
    if settings.oidc_issuer:
        logger.info("authentication: OIDC (issuer=%s)", settings.oidc_issuer)
        return oidc_token_resolver

    if settings.allow_bootstrap_tokens:
        logger.warning(
            "authentication: UNSIGNED bootstrap tokens are enabled "
            "(environment=%s). This is a development-only path.",
            settings.environment,
        )
        return bootstrap_token_resolver

    # get_settings() rejects this combination, so reaching here means the
    # settings were bypassed. Refuse rather than default to anything.
    raise RuntimeError("no usable authentication strategy configured")
