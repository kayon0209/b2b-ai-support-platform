"""Tenant, User, Membership, Role models (docs/domain-model.md)."""

import enum
import uuid
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    Enum,
    ForeignKey,
    ForeignKeyConstraint,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from platform_core.orm_base import Base, PkMixin, TenantMixin


class TenantStatus(enum.StrEnum):
    ACTIVE = "active"
    SUSPENDED = "suspended"


def _enum_values(enum_cls: type[enum.Enum]) -> list[str]:
    """Persist enum *values*, not member names.

    SQLAlchemy's Enum type defaults to storing member names (ACTIVE), but
    every migration in this repo creates a plain String column seeded with
    lowercase values ("active"). Without `values_callable` the ORM and the
    schema disagree, and reads fail at result-processing time with
    "LookupError: 'active' is not among the defined enum values".
    """
    return [str(member.value) for member in enum_cls]


class Tenant(Base, PkMixin):
    __tablename__ = "tenants"

    slug: Mapped[str] = mapped_column(String(63), unique=True)
    name: Mapped[str] = mapped_column(String(255))
    status: Mapped[TenantStatus] = mapped_column(
        Enum(TenantStatus, native_enum=False, values_callable=_enum_values),
        default=TenantStatus.ACTIVE,
    )
    default_timezone: Mapped[str] = mapped_column(String(63), default="UTC")
    data_region: Mapped[str] = mapped_column(String(31), default="cn-north-1")

    # Branding (Phase 5). Nullable: unset means "use the platform default".
    brand_display_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    brand_logo_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    brand_primary_color: Mapped[str | None] = mapped_column(String(31), nullable=True)
    support_email: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Agent runs allowed per calendar month. NULL means unlimited.
    monthly_run_quota: Mapped[int | None] = mapped_column(nullable=True)


class User(Base, PkMixin):
    __tablename__ = "users"

    # Platform-local user. External IdP subjects live in external_identities.
    primary_email: Mapped[str] = mapped_column(String(255), unique=True)
    display_name: Mapped[str] = mapped_column(String(255))
    is_service_account: Mapped[bool] = mapped_column(Boolean, default=False)


class MembershipRole(enum.StrEnum):
    TENANT_OWNER = "tenant_owner"
    SECURITY_ADMIN = "security_admin"
    SUPPORT_ADMIN = "support_admin"
    KNOWLEDGE_MANAGER = "knowledge_manager"
    SUPPORT_AGENT = "support_agent"
    SUPPORT_VIEWER = "support_viewer"
    INTEGRATION_SERVICE = "integration_service"
    AUDITOR = "auditor"


class Membership(Base, PkMixin, TenantMixin):
    """Links a user to a tenant with one role. Role set is intentionally
    single-role for MVP; policy conditions arrive in Phase 2 ABAC."""

    __tablename__ = "memberships"
    __table_args__ = (
        UniqueConstraint("tenant_id", "user_id", name="uq_membership_tenant_user"),
        UniqueConstraint("id", "tenant_id", name="uq_memberships_id_tenant"),
        # Composite so a membership cannot be filed under another tenant's
        # department. RLS hides the other tenant's row rather than rejecting
        # the value, so a plain FK would let the write succeed and then read
        # back as "no department" - a silent, unqueryable inconsistency.
        ForeignKeyConstraint(
            ["department_id", "tenant_id"],
            ["departments.id", "departments.tenant_id"],
            name="fk_memberships_department_same_tenant",
        ),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    role: Mapped[MembershipRole] = mapped_column(
        Enum(MembershipRole, native_enum=False, values_callable=_enum_values), nullable=False
    )
    status: Mapped[str] = mapped_column(String(31), default="active")
    # Named in docs/domain-model.md since the beginning and absent from the
    # schema until migration 0028. Optional: membership is not conditional on
    # an org chart existing, and a tenant with no departments is a tenant where
    # every member has this null.
    department_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True, index=True)


class ExternalIdentity(Base, PkMixin, TenantMixin):
    """Links an external IdP subject to a platform user (ticket 23).

    UNIQUE(system, subject): one IdP subject maps to exactly one user per
    system. Tenant context is derived from these rows, never from tokens.
    """

    __tablename__ = "external_identities"
    __table_args__ = (UniqueConstraint("system", "subject", name="uq_external_identity_subject"),)

    system: Mapped[str] = mapped_column(String(255), nullable=False)
    subject: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)


class InvitationStatus(enum.StrEnum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    EXPIRED = "expired"
    REVOKED = "revoked"


class MembershipInvitation(Base, PkMixin, TenantMixin):
    """Single-use invitation token for tenant self-service (Phase 5).

    A tenant_owner or security_admin invites an email address; the invitee
    receives an opaque token (delivered via their chosen side channel in
    production, returned directly in local/test) and consumes it to create a
    User + Membership in the tenant.

    The token is single-use and expires. Consumed tokens are marked
    ACCEPTED, never deleted, so the audit trail shows who was invited and
    when -- even if the invitee never accepted.
    """

    __tablename__ = "membership_invitations"
    __table_args__ = (
        UniqueConstraint("tenant_id", "email", name="uq_invite_per_tenant_email"),
        UniqueConstraint("tenant_id", "token", name="uq_invite_per_tenant_token"),
    )

    email: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[MembershipRole] = mapped_column(
        Enum(
            MembershipRole, native_enum=False, values_callable=lambda e: [str(m.value) for m in e]
        ),
        nullable=False,
    )
    token: Mapped[uuid.UUID] = mapped_column(nullable=False, unique=True, index=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    created_at: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    expires_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
    accepted_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    status: Mapped[str] = mapped_column(
        String(31), nullable=False, default=InvitationStatus.PENDING.value
    )


class TenantDomain(Base, PkMixin, TenantMixin):
    """A host a tenant claims, and whether that claim has been proven.

    The migration carries the reasoning; the two facts worth repeating here are
    that `domain` uniqueness is global (two tenants owning one host would make
    Host -> tenant resolution depend on row order) and that **only verified
    rows resolve**. An unverified claim must never be served: a request's Host
    header is caller-controlled, so serving an unverified claim would let a
    tenant publish their branding on a domain they do not own.
    """

    __tablename__ = "tenant_domains"
    __table_args__ = (UniqueConstraint("domain", name="uq_tenant_domain"),)

    # Stored lowercase (a CHECK constraint enforces it); normalised at the API
    # boundary so a raw insert cannot create a second row describing the same
    # host with different casing.
    domain: Mapped[str] = mapped_column(String(255), nullable=False)
    # Proves the claim was made by someone holding this value out of band.
    # Not a secret for authentication - it identifies the claim, and is what an
    # operator checks against the DNS record the tenant published.
    verification_token: Mapped[str] = mapped_column(String(64), nullable=False)
    # NULL means unproven. Nothing serves an unproven domain.
    verified_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")


class AccountTier(enum.StrEnum):
    """Contract tier. Closed, because an SLA clock is derived from it.

    A free-text tier would let a typo fall through to the default policy
    silently, and the symptom would be a customer with a tighter contractual
    window than they were actually given. The API validates against this set
    and the column carries a CHECK constraint, so the value is enforced at both
    boundaries.
    """

    STRATEGIC = "strategic"
    ENTERPRISE = "enterprise"
    STANDARD = "standard"
    BASIC = "basic"


class ContractStatus(enum.StrEnum):
    """`churned` is a value rather than a deletion: the account's history and
    its closed Cases must remain readable, and an audit of "what did we agree
    to" is answered from rows that still exist."""

    ACTIVE = "active"
    PENDING = "pending"
    SUSPENDED = "suspended"
    CHURNED = "churned"


class EnterpriseAccount(Base, PkMixin, TenantMixin):
    """The tenant's own customer/account hierarchy.

    `docs/domain-model.md` warns not to confuse this with a Chatwoot Account,
    and it is right to: Chatwoot owns the conversation, this owns the contract.
    `cases.enterprise_account_id` points here.

    The composite foreign keys (`(parent_id, tenant_id)` -> `(id, tenant_id)`)
    are the reason the parent link is trustworthy. A single-column FK would
    accept another tenant's account as a parent, and because RLS hides that row
    the mistake would not raise - the child would simply read as a root, which
    is indistinguishable from a deliberate root.
    """

    __tablename__ = "enterprise_accounts"
    __table_args__ = (
        UniqueConstraint("id", "tenant_id", name="uq_enterprise_accounts_id_tenant"),
        ForeignKeyConstraint(
            ["parent_id", "tenant_id"],
            ["enterprise_accounts.id", "enterprise_accounts.tenant_id"],
            name="fk_enterprise_accounts_parent_same_tenant",
        ),
    )

    parent_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True, index=True)
    # The CRM's identifier for this account, so a sync can upsert instead of
    # inserting a second copy. UNIQUE per tenant (partial, see migration 0028).
    external_crm_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    tier: Mapped[AccountTier] = mapped_column(
        Enum(AccountTier, native_enum=False, values_callable=_enum_values),
        nullable=False,
        default=AccountTier.STANDARD,
        server_default=AccountTier.STANDARD.value,
    )
    contract_status: Mapped[ContractStatus] = mapped_column(
        Enum(ContractStatus, native_enum=False, values_callable=_enum_values),
        nullable=False,
        default=ContractStatus.ACTIVE,
        server_default=ContractStatus.ACTIVE.value,
    )
    attributes: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    created_at: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    updated_at: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")


class Department(Base, PkMixin, TenantMixin):
    """An internal org unit, used to route work and to scope ABAC conditions.

    Hierarchy for the same reason as `EnterpriseAccount`, and with the same
    composite-FK guard. `slug` is the stable handle an IdP group mapping or a
    Jira project binding would target - names get renamed, so mapping off the
    name would silently re-point a binding.
    """

    __tablename__ = "departments"
    __table_args__ = (
        UniqueConstraint("id", "tenant_id", name="uq_departments_id_tenant"),
        UniqueConstraint("tenant_id", "slug", name="uq_departments_tenant_slug"),
        ForeignKeyConstraint(
            ["parent_id", "tenant_id"],
            ["departments.id", "departments.tenant_id"],
            name="fk_departments_parent_same_tenant",
        ),
    )

    parent_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    # Stored lowercase; a CHECK constraint enforces it so a raw insert cannot
    # create a second row that is the same department with different casing.
    slug: Mapped[str] = mapped_column(String(63), nullable=False)
    external_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    updated_at: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")


class SamlConnection(Base, PkMixin, TenantMixin):
    """A tenant's own identity provider.

    Per tenant, not per deployment: an IdP signing certificate, entity id and
    SSO URL belong to the tenant's identity provider, and two tenants using two
    IdPs is the first thing an enterprise asks for.

    `idp_certificate` holds a **public** certificate. The platform verifies with
    it and has no use for a private key, so there is no column for one - a
    private key here would be a liability with no feature attached.
    """

    __tablename__ = "saml_connections"
    __table_args__ = (
        UniqueConstraint("tenant_id", "name", name="uq_saml_connection_name"),
        UniqueConstraint("id", "tenant_id", name="uq_saml_connections_id_tenant"),
    )

    name: Mapped[str] = mapped_column(String(63), nullable=False)
    idp_entity_id: Mapped[str] = mapped_column(String(512), nullable=False)
    idp_sso_url: Mapped[str] = mapped_column(String(1024), nullable=False)
    idp_certificate: Mapped[str] = mapped_column(Text, nullable=False)
    sp_entity_id: Mapped[str] = mapped_column(String(512), nullable=False)
    status: Mapped[str] = mapped_column(String(31), nullable=False, server_default="active")
    created_at: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    updated_at: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")


class SamlConsumedAssertion(Base, PkMixin, TenantMixin):
    """The replay guard: one row per assertion this tenant has accepted.

    `UNIQUE (connection_id, assertion_id)` makes a second presentation
    unrepresentable rather than unlikely - the same mechanism as
    `uq_inbox_delivery`, and for the same reason: a check-then-insert loses the
    race, and a constraint does not.

    Scoped by connection, not by tenant: assertion ids are the IdP's to
    allocate, two IdPs may legitimately issue the same one, and a tenant-wide
    key would reject a valid second IdP's assertion as a replay.
    """

    __tablename__ = "saml_consumed_assertions"
    __table_args__ = (
        UniqueConstraint("connection_id", "assertion_id", name="uq_saml_assertion_once"),
        ForeignKeyConstraint(
            ["connection_id", "tenant_id"],
            ["saml_connections.id", "saml_connections.tenant_id"],
            name="fk_saml_assertion_same_tenant",
        ),
    )

    connection_id: Mapped[uuid.UUID] = mapped_column(nullable=False, index=True)
    assertion_id: Mapped[str] = mapped_column(String(255), nullable=False)
    consumed_at: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")


class ScimToken(Base, PkMixin, TenantMixin):
    """A provisioning bearer token, stored as a SHA-256 hash.

    The token can create users and move them between departments, and it is
    presented on every provisioning request - so a plaintext copy would sit in
    every log and every backup.

    SHA-256 rather than bcrypt: this is a high-entropy random string, not a
    password, so there is nothing to slow a guess down for, and a per-request
    KDF would put latency on the provisioning path for no security gain.

    `revoked_at` is a timestamp rather than a delete, so "when did this stop
    working" survives and a revoked token cannot be resurrected by re-inserting
    the same hash.
    """

    __tablename__ = "scim_tokens"
    __table_args__ = (
        UniqueConstraint("token_hash", name="uq_scim_token_hash"),
        UniqueConstraint("tenant_id", "name", name="uq_scim_token_name"),
    )

    name: Mapped[str] = mapped_column(String(63), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    revoked_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
