"""Tenant custom domains: normalisation, claiming, verification and resolution.

Security posture, in one paragraph
----------------------------------
A `Host` header is caller-controlled, so anything derived from it must never
select a tenant for an *authenticated* request - that would be "tenant id from
the request", which AGENTS.md forbids and which every layer of this platform
exists to prevent. What Host resolution is allowed to do is narrower and
useful: serve a tenant's **public branding** page. That is why
`resolve_tenant_for_host` is only called from the public branding endpoint and
why no API route takes a tenant from it.

The second half is that only **verified** domains resolve. If an unverified
claim resolved, a tenant could claim `example-bank.com` and have the platform
publish their branding on a domain they do not own.

Verification is an operator attestation today
---------------------------------------------
Automated verification means reading a DNS TXT record, which needs a DNS
resolver dependency (and a decision about caching, retries and failure modes)
that this repository does not have. Rather than pretend, `verify_domain`
records an **explicit, audited attestation** by a tenant admin: the operator
confirms they checked the published record. That is a real control at pilot
scale and a weak one at large scale, and it is named here so the gap is
visible instead of implied by a `verified_at` column that looks automatic.
"""

from __future__ import annotations

import re
import secrets
import time
import uuid
from dataclasses import dataclass

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from platform_core.audit import service as audit_service
from platform_core.identity.models import TenantDomain
from platform_core.identity.tenant_context import TenantContext

# A hostname, optionally with the leading label being a wildcard-free name.
#
# Deliberately strict: no scheme, no port, no path, no userinfo, no underscore,
# no leading or trailing dot. `evil.com#@bank.com` and `bank.com:8080` are the
# shapes this refuses, and a permissive pattern would accept both while
# `resolve_domain_tenant` compared them against stored hosts that never look
# like that - so the failure would be a confusing "domain not verified" rather
# than a clear rejection at claim time.
_HOSTNAME = re.compile(
    r"^(?=.{1,253}$)(?!-)[a-z0-9-]{1,63}(?<!-)"
    r"(?:\.(?!-)[a-z0-9-]{1,63}(?<!-))*$"
)

# Domains that must never be claimed. `localhost` and a bare IP would let a
# tenant intercept traffic in a local or container environment, and `.internal`
# style suffixes resolve inside a network rather than on the public internet.
FORBIDDEN_SUFFIXES = (".local", ".localhost", ".internal", ".invalid", ".test")
FORBIDDEN_EXACT = frozenset({"localhost"})

# A public host has at least two labels (`acme.example`). A bare label is an
# intranet name that no public DNS record can verify.
MIN_LABELS = 2

AUDIT_CLAIMED = "tenant_domain.claimed"
AUDIT_VERIFIED = "tenant_domain.verified"
AUDIT_REMOVED = "tenant_domain.removed"


class DomainError(Exception):
    """A domain that cannot be claimed, verified or removed."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code


@dataclass(frozen=True)
class HostResolution:
    """What a Host header resolved to. No tenant details, by construction."""

    tenant_id: uuid.UUID


def normalise_domain(raw: str) -> str:
    """Reduce any accepted spelling of a host to the stored form.

    Lowercase, no trailing dot, no port. A trailing dot is a legal fully
    -qualified name (`example.com.`) and a port is what a browser omits from
    `Host` on the default ports but includes otherwise - both would otherwise
    create a row that `resolve_domain_tenant` never matches.
    """
    candidate = (raw or "").strip().lower()
    if not candidate:
        raise DomainError("DOMAIN_EMPTY")
    # Strip a scheme if someone pastes a URL.
    for prefix in ("https://", "http://"):
        if candidate.startswith(prefix):
            candidate = candidate[len(prefix) :]
    # Strip a path, query or fragment.
    for separator in ("/", "?", "#"):
        candidate = candidate.split(separator, 1)[0]
    # Strip a port: a hostname has no colon.
    if ":" in candidate:
        candidate = candidate.split(":", 1)[0]
    # Strip a single trailing dot (FQDN form).
    if candidate.endswith("."):
        candidate = candidate[:-1]
    return candidate


def validate_domain(domain: str) -> None:
    """Raise `DomainError` unless the host is claimable."""
    if not domain:
        raise DomainError("DOMAIN_EMPTY")
    if domain in FORBIDDEN_EXACT or domain.endswith(FORBIDDEN_SUFFIXES):
        raise DomainError("DOMAIN_RESERVED", domain)
    if domain.count(".") < MIN_LABELS - 1:
        raise DomainError("DOMAIN_NOT_FULLY_QUALIFIED", domain)
    if not _HOSTNAME.match(domain):
        raise DomainError("DOMAIN_MALFORMED", domain)


def new_verification_token() -> str:
    return secrets.token_hex(16)


async def list_domains(session: AsyncSession, *, tenant_id: uuid.UUID) -> list[TenantDomain]:
    stmt = (
        select(TenantDomain)
        .where(TenantDomain.tenant_id == tenant_id)
        .order_by(TenantDomain.created_at, TenantDomain.id)
    )
    return list((await session.execute(stmt)).scalars().all())


async def claim_domain(
    session: AsyncSession,
    *,
    ctx: TenantContext,
    raw_domain: str,
    now: int | None = None,
    trace_id: str | None = None,
) -> TenantDomain:
    """Register a claim. Starts unverified, and unverified domains are not served.

    `define_flag`-style closure: claiming a domain must not publish anything.
    """
    domain = normalise_domain(raw_domain)
    validate_domain(domain)

    existing = (
        await session.execute(select(TenantDomain).where(TenantDomain.domain == domain))
    ).scalar_one_or_none()
    if existing is not None:
        # This branch is only reachable for the caller's OWN domain, because
        # RLS hides every other tenant's rows. That is useful: the two cases
        # have different codes, and this one can be specific.
        raise DomainError("DOMAIN_ALREADY_CLAIMED", domain)

    row = TenantDomain(
        tenant_id=ctx.tenant_id,
        domain=domain,
        verification_token=new_verification_token(),
        verified_at=None,
        created_at=now if now is not None else int(time.time()),
    )
    session.add(row)
    try:
        await session.flush()
    except IntegrityError as exc:
        # The domain is someone else's. RLS means the SELECT above could not
        # see it, so the authoritative check is the constraint - which is the
        # right place for it anyway, since uniqueness is a database property
        # and two concurrent claims would race past any application check.
        #
        # The rollback is not optional and not cosmetic: a failed flush leaves
        # the session unusable, and without this the router's later `commit()`
        # raises `PendingRollbackError` - so the caller sees a 500 instead of
        # a 409. `begin_nested` was the first attempt and did not help here.
        # Rolling back the whole transaction is safe because nothing else has
        # been written at this point: the claim is the first write in the
        # request, and the audit event below has not been recorded yet.
        await session.rollback()
        raise DomainError("DOMAIN_UNAVAILABLE", domain) from exc

    await audit_service.record(
        session,
        ctx=ctx,
        action=AUDIT_CLAIMED,
        resource_type="tenant_domain",
        resource_id=row.id,
        decision="completed",
        reason_code="UNVERIFIED",
        after={"domain": domain, "verified": False},
        trace_id=trace_id,
    )
    return row


async def verify_domain(
    session: AsyncSession,
    row: TenantDomain,
    *,
    ctx: TenantContext,
    now: int | None = None,
    trace_id: str | None = None,
) -> bool:
    """Mark a claim verified. Returns whether this call changed it.

    An **attestation**, not a check - see the module docstring. Idempotent,
    because re-confirming is not an incident and must not write a second audit
    event for a transition that did not happen.
    """
    if row.verified_at is not None:
        return False

    row.verified_at = now if now is not None else int(time.time())
    await audit_service.record(
        session,
        ctx=ctx,
        action=AUDIT_VERIFIED,
        resource_type="tenant_domain",
        resource_id=row.id,
        decision="completed",
        reason_code="OPERATOR_ATTESTATION",
        after={"domain": row.domain, "verified": True},
        trace_id=trace_id,
    )
    await session.flush()
    return True


async def remove_domain(
    session: AsyncSession, row: TenantDomain, *, ctx: TenantContext, trace_id: str | None = None
) -> None:
    """Release a host. Frees the globally-unique domain for another tenant."""
    domain = row.domain
    await session.delete(row)
    await audit_service.record(
        session,
        ctx=ctx,
        action=AUDIT_REMOVED,
        resource_type="tenant_domain",
        resource_id=row.id,
        decision="completed",
        reason_code="RELEASED",
        before={"domain": domain},
        trace_id=trace_id,
    )
    await session.flush()


async def resolve_tenant_for_host(session: AsyncSession, host: str) -> HostResolution | None:
    """Map a Host header to a tenant, or None.

    Returns None for unverified domains, unknown hosts, and suspended tenants -
    all three are "do not serve", and distinguishing them to the caller would
    turn this into a way to enumerate claims.

    Runs through the SECURITY DEFINER resolver, because `tenant_domains` is
    FORCE-RLS'd on a binding this function exists to discover.
    """
    domain = normalise_domain(host)
    if not domain:
        return None
    row = (
        await session.execute(text("SELECT resolve_domain_tenant(CAST(:d AS text))"), {"d": domain})
    ).scalar_one_or_none()
    if row is None:
        return None
    return HostResolution(tenant_id=row)
