"""OIDC authentication module (ticket 23, docs/security.md).

Pilot scheme:
- Keycloak issues access tokens (Authorization Code + PKCE for humans;
  client credentials for services).
- The API verifies tokens against the realm's JWKS: signature, issuer,
  audience, expiry.
- Claims map to platform identity through external_identities:
  (system, subject) -> user_id -> membership -> TenantContext.

Tenant context remains server-side: the token never carries a trusted
tenant_id; membership rows decide.
"""

import uuid
from dataclasses import dataclass
from typing import Any

import httpx
import jwt
from jwt import PyJWKClient

from platform_core.identity.tenant_context import TenantContext

TOKEN_LEEWAY_SECONDS = 30


class OidcError(Exception):
    """Fail-closed token verification error."""


class OidcVerifier:
    """Verifies Keycloak access tokens via the realm JWKS endpoint.

    The JWKS client caches keys and refreshes on unknown kid, per OIDC
    convention. All failures raise OidcError -> 401 (fail closed).
    """

    def __init__(
        self,
        issuer: str,
        audience: str = "platform-api",
        *,
        jwks_cache_seconds: int = 300,
        http_timeout: float = 5.0,
    ) -> None:
        self._issuer = issuer.rstrip("/")
        self._audience = audience
        self._jwks_uri = f"{self._issuer}/protocol/openid-connect/certs"
        self._jwks = PyJWKClient(self._jwks_uri, cache_jwk_set=True, lifespan=jwks_cache_seconds)
        self._http_timeout = http_timeout

    def verify(self, token: str, *, now: int | None = None) -> dict[str, Any]:
        """Return verified claims or raise OidcError."""
        try:
            signing_key = self._jwks.get_signing_key_from_jwt(token)
        except Exception as exc:
            raise OidcError(f"jwks_unavailable: {exc}") from exc

        try:
            claims = jwt.decode(
                token,
                signing_key.key,
                algorithms=["RS256"],
                issuer=self._issuer,
                leeway=TOKEN_LEEWAY_SECONDS,
                options={"require": ["exp", "iat", "iss", "sub"], "verify_aud": False},
            )
        except jwt.PyJWTError as exc:
            raise OidcError(f"token_invalid: {type(exc).__name__}") from exc

        # Audience check: Keycloak bearer-only access tokens carry azp (the
        # client id) and often aud="account". Accept either our client id
        # in aud or azp == client id; anything else fails closed.
        aud = claims.get("aud")
        aud_list = aud if isinstance(aud, list) else [aud]
        if self._audience not in aud_list and claims.get("azp") != self._audience:
            raise OidcError("audience_mismatch")

        if now is not None:
            iat = claims.get("iat")
            if isinstance(iat, (int, float)) and iat > now + TOKEN_LEEWAY_SECONDS:
                raise OidcError("token_issued_in_future")
            exp = claims.get("exp")
            if isinstance(exp, (int, float)) and exp <= now:
                raise OidcError("token_expired")
        return claims


@dataclass(frozen=True)
class ResolvedIdentity:
    user_id: uuid.UUID
    tenant_id: uuid.UUID
    role: str
    actor_kind: str  # user | service


class MembershipResolver:
    """Maps verified token claims to a TenantContext via external_identities.

    Only DB-resolved membership grants tenant context; a token with no
    matching identity row yields no context (fail closed).
    """

    def __init__(self, session_factory) -> None:
        self._session_factory = session_factory

    async def resolve(self, claims: dict[str, Any]) -> ResolvedIdentity | None:
        from sqlalchemy import select

        from platform_core.identity.models import Membership
        from platform_core.identity.tenant_context import TenantContextError

        subject = str(claims.get("sub", ""))
        idp_system = str(claims.get("iss", ""))  # issuer as external system id
        preferred_username = claims.get("preferred_username")
        actor_kind = (
            "service"
            if claims.get("client_credentials")
            or (preferred_username and str(preferred_username).startswith("service-"))
            else "user"
        )

        async with self._session_factory() as session:
            from platform_core.identity.models import ExternalIdentity, TenantStatus

            identity = (
                await session.execute(
                    select(ExternalIdentity).where(
                        ExternalIdentity.system == idp_system,
                        ExternalIdentity.subject == subject,
                    )
                )
            ).scalar_one_or_none()
            if identity is None:
                return None

            membership = (
                await session.execute(
                    select(Membership).where(
                        Membership.tenant_id == identity.tenant_id,
                        Membership.user_id == identity.user_id,
                        Membership.status == "active",
                    )
                )
            ).scalar_one_or_none()
            if membership is None:
                raise TenantContextError("no active membership")

            # Tenant row check: suspended tenants fail closed.
            from platform_core.identity.models import Tenant

            tenant = (
                await session.execute(select(Tenant).where(Tenant.id == identity.tenant_id))
            ).scalar_one_or_none()
            if tenant is None or tenant.status != TenantStatus.ACTIVE:
                raise TenantContextError("tenant inactive")

            return ResolvedIdentity(
                user_id=identity.user_id,
                tenant_id=identity.tenant_id,
                role=membership.role.value
                if hasattr(membership.role, "value")
                else str(membership.role),
                actor_kind=actor_kind,
            )

    async def tenant_context(self, claims: dict[str, Any]) -> TenantContext:
        resolved = await self.resolve(claims)
        if resolved is None:
            raise OidcError("identity_not_registered")
        return TenantContext(
            tenant_id=resolved.tenant_id,
            actor_id=resolved.user_id,
            actor_kind=resolved.actor_kind,  # type: ignore[arg-type]
            role=resolved.role,
        )


def fetch_realm_metadata(issuer: str) -> dict[str, Any]:
    """Fetch the realm's well-known configuration (used by setup tooling
    and health checks; not on the hot path)."""
    resp = httpx.get(
        f"{issuer.rstrip('/')}/.well-known/openid-configuration",
        timeout=5.0,
    )
    resp.raise_for_status()
    return resp.json()
