"""SAML connection administration, replay prevention and identity resolution.

The boundary this module drew, and why
-------------------------------------
**A SAML login establishes identity; it does not mint an API session.** This
platform authenticates API calls with OIDC bearer tokens issued by Keycloak, and
there is no session-token issuer here to hand out. Inventing one for SAML would
mean a second, less-examined way to obtain a credential that reaches customer
data - which is a large security decision dressed up as a convenience. What the
ACS endpoint does instead is what SAML is actually for in this deployment:
prove who the user is, refuse them if the tenant never granted them a role, and
record the login in the audit trail.

**Just-in-time provisioning never grants a role.** A SAML attribute saying
`role=tenant_owner` is attacker-controlled text when the IdP is the attacker's,
and even a well-behaved IdP's attribute mapping is configured by whoever
administers it. So a first login creates the `User` and the
`ExternalIdentity`, and then **requires an existing active `Membership`**; with
no membership it refuses with `SAML_NO_MEMBERSHIP`. Granting access is a
deliberate act by the tenant (`POST /v1/identity/members/invite`, or SCIM with a
provisioning token the tenant holds), which is where it belongs.
"""

from __future__ import annotations

import hashlib
import time
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from platform_core.audit import service as audit_service
from platform_core.identity.models import (
    ExternalIdentity,
    Membership,
    MembershipRole,
    SamlConnection,
    User,
)
from platform_core.identity.saml import SamlConnectionConfig
from platform_core.identity.tenant_context import TenantContext

# The system key external identities from this adapter are stored under. Not
# "saml": a tenant may run two IdPs, and a single key would make the same
# NameID from `okta` and `entra` collide on one UNIQUE(system, subject) row.
SAML_SYSTEM_PREFIX = "saml:"

AUDIT_CONNECTION_CREATED = "saml.connection.created"
AUDIT_CONNECTION_UPDATED = "saml.connection.updated"
AUDIT_LOGIN = "saml.login"
AUDIT_LOGIN_REFUSED = "saml.login.refused"


class SamlServiceError(Exception):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code


@dataclass(frozen=True)
class ResolvedLogin:
    user_id: uuid.UUID
    membership_id: uuid.UUID
    role: str
    created_user: bool


# --- connection administration ---------------------------------------------


async def list_connections(session: AsyncSession, *, tenant_id: uuid.UUID) -> list[SamlConnection]:
    rows = await session.execute(
        select(SamlConnection)
        .where(SamlConnection.tenant_id == tenant_id)
        .order_by(SamlConnection.name)
    )
    return list(rows.scalars())


async def create_connection(
    session: AsyncSession,
    *,
    ctx: TenantContext,
    name: str,
    idp_entity_id: str,
    idp_sso_url: str,
    idp_certificate: str,
    sp_entity_id: str,
    trace_id: str | None = None,
) -> SamlConnection:
    name = name.strip()
    if not name:
        raise SamlServiceError("NAME_REQUIRED")
    if not idp_sso_url.startswith("https://"):
        # http:// would send the AuthnRequest, and the browser's session
        # cookie, in the clear. Refused rather than warned about, because a
        # warning on a security control is a control nobody applies.
        raise SamlServiceError("SSO_URL_NOT_HTTPS")
    if "PRIVATE KEY" in idp_certificate:
        raise SamlServiceError("CERTIFICATE_IS_A_PRIVATE_KEY")

    row = SamlConnection(
        tenant_id=ctx.tenant_id,
        name=name,
        idp_entity_id=idp_entity_id.strip(),
        idp_sso_url=idp_sso_url.strip(),
        idp_certificate=idp_certificate.strip(),
        sp_entity_id=sp_entity_id.strip(),
        status="active",
    )
    session.add(row)
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise SamlServiceError("CONNECTION_NAME_TAKEN", name) from exc

    await audit_service.record(
        session,
        ctx=ctx,
        action=AUDIT_CONNECTION_CREATED,
        resource_type="saml_connection",
        resource_id=row.id,
        decision="completed",
        reason_code="OK",
        metadata={"name": name, "idp_entity_id": row.idp_entity_id},
        trace_id=trace_id,
    )
    return row


async def set_connection_status(
    session: AsyncSession,
    *,
    ctx: TenantContext,
    connection: SamlConnection,
    status: str,
    trace_id: str | None = None,
) -> bool:
    if status not in ("active", "disabled"):
        raise SamlServiceError("STATUS_INVALID", status)
    changed = connection.status != status
    connection.status = status
    await session.flush()
    if changed:
        await audit_service.record(
            session,
            ctx=ctx,
            action=AUDIT_CONNECTION_UPDATED,
            resource_type="saml_connection",
            resource_id=connection.id,
            decision="completed",
            reason_code="OK",
            metadata={"status": status},
            trace_id=trace_id,
        )
    return changed


# --- the unauthenticated lookups -------------------------------------------


async def resolve_connection(
    session: AsyncSession, *, connection_id: uuid.UUID
) -> SamlConnectionConfig | None:
    """Resolve a connection **before** any tenant is known.

    Called through the narrow SECURITY DEFINER function from migration 0030,
    because `saml_connections` is FORCE-RLS'd on the binding being resolved.
    Returning a projection rather than an ORM row keeps the unauthenticated
    path from holding a mutable object it could be extended to read.
    """
    row = (
        await session.execute(
            text(
                "SELECT tenant_id, idp_entity_id, idp_sso_url, idp_certificate, "
                "sp_entity_id, status FROM resolve_saml_connection(:cid)"
            ),
            {"cid": connection_id},
        )
    ).one_or_none()
    if row is None:
        return None
    return SamlConnectionConfig(
        connection_id=connection_id,
        tenant_id=row[0],
        idp_entity_id=row[1],
        idp_sso_url=row[2],
        idp_certificate=row[3],
        sp_entity_id=row[4],
        status=row[5],
    )


async def record_assertion_once(
    session: AsyncSession, *, tenant_id: uuid.UUID, connection_id: uuid.UUID, assertion_id: str
) -> bool:
    """`True` the first time, `False` forever after.

    `ON CONFLICT DO NOTHING` rather than insert-then-catch: catching means
    rolling back, and a rollback would undo the login's own audit write. The
    constraint is the authority - a check-then-insert loses the race between two
    replicas, and this is a bearer credential being replayed.
    """
    inserted = (
        await session.execute(
            text(
                "INSERT INTO saml_consumed_assertions "
                "(id, tenant_id, connection_id, assertion_id, consumed_at) "
                "VALUES (gen_random_uuid(), :t, :c, :a, :now) "
                "ON CONFLICT ON CONSTRAINT uq_saml_assertion_once DO NOTHING "
                "RETURNING id"
            ),
            {
                "t": tenant_id,
                "c": connection_id,
                "a": assertion_id[:255],
                "now": int(time.time()),
            },
        )
    ).one_or_none()
    return inserted is not None


# --- identity resolution ----------------------------------------------------


async def resolve_login(
    session: AsyncSession,
    *,
    ctx: TenantContext,
    connection: SamlConnectionConfig,
    name_id: str,
    attributes: dict[str, list[str]],
    trace_id: str | None = None,
) -> ResolvedLogin:
    """Map a verified NameID to a user with a role in this tenant.

    Refuses with `SAML_NO_MEMBERSHIP` when the tenant has never granted this
    person access - see the module docstring for why a first login must not
    create one.
    """
    system = f"{SAML_SYSTEM_PREFIX}{connection.connection_id}"
    subject = name_id.strip()
    if not subject:
        raise SamlServiceError("SAML_NAMEID_MISSING")

    identity = (
        await session.execute(
            select(ExternalIdentity).where(
                ExternalIdentity.system == system, ExternalIdentity.subject == subject
            )
        )
    ).scalar_one_or_none()

    created_user = False
    if identity is None:
        user = (
            await session.execute(select(User).where(User.primary_email == _emailish(subject)))
        ).scalar_one_or_none()
        if user is None:
            user = User(
                primary_email=_emailish(subject),
                display_name=(attributes.get("displayName") or [subject])[0],
                is_service_account=False,
            )
            session.add(user)
            await session.flush()
            created_user = True
        session.add(
            ExternalIdentity(
                tenant_id=ctx.tenant_id, system=system, subject=subject, user_id=user.id
            )
        )
        await session.flush()
        user_id = user.id
    else:
        user_id = identity.user_id

    membership = (
        await session.execute(
            select(Membership).where(
                Membership.tenant_id == ctx.tenant_id,
                Membership.user_id == user_id,
                Membership.status == "active",
            )
        )
    ).scalar_one_or_none()

    if membership is None:
        raise SamlServiceError("SAML_NO_MEMBERSHIP", subject)

    await audit_service.record(
        session,
        ctx=ctx,
        action=AUDIT_LOGIN,
        resource_type="saml_connection",
        resource_id=connection.connection_id,
        decision="completed",
        reason_code="OK",
        metadata={
            "subject_hash": hashlib.sha256(subject.encode()).hexdigest()[:32],
            "created_user": created_user,
        },
        trace_id=trace_id,
    )
    return ResolvedLogin(
        user_id=user_id,
        membership_id=membership.id,
        role=_role_value(membership.role),
        created_user=created_user,
    )


def _emailish(subject: str) -> str:
    """The NameID format this SP requests is `emailAddress`, but an IdP may send
    something else. A synthetic address keeps `users.primary_email` (UNIQUE and
    NOT NULL) satisfiable without pretending the value is a real mailbox."""
    if "@" in subject:
        return subject.lower()[:255]
    digest = hashlib.sha256(subject.encode()).hexdigest()[:16]
    return f"saml-{digest}@idp.invalid"


def _role_value(role: Any) -> str:
    return role.value if isinstance(role, MembershipRole) else str(role)


async def find_connection(
    session: AsyncSession, *, tenant_id: uuid.UUID, connection_id: uuid.UUID
) -> SamlConnection | None:
    return (
        await session.execute(
            select(SamlConnection).where(
                SamlConnection.id == connection_id, SamlConnection.tenant_id == tenant_id
            )
        )
    ).scalar_one_or_none()


__all__ = [
    "AUDIT_CONNECTION_CREATED",
    "AUDIT_CONNECTION_UPDATED",
    "AUDIT_LOGIN",
    "AUDIT_LOGIN_REFUSED",
    "SAML_SYSTEM_PREFIX",
    "ResolvedLogin",
    "SamlServiceError",
    "create_connection",
    "find_connection",
    "list_connections",
    "record_assertion_once",
    "resolve_connection",
    "resolve_login",
    "set_connection_status",
]
