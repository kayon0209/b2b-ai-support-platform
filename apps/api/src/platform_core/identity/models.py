"""Tenant, User, Membership, Role models (docs/domain-model.md)."""

import enum
import uuid

from sqlalchemy import BigInteger, Boolean, Enum, ForeignKey, String, UniqueConstraint
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
    __table_args__ = (UniqueConstraint("tenant_id", "user_id", name="uq_membership_tenant_user"),)

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    role: Mapped[MembershipRole] = mapped_column(
        Enum(MembershipRole, native_enum=False, values_callable=_enum_values), nullable=False
    )
    status: Mapped[str] = mapped_column(String(31), default="active")


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
