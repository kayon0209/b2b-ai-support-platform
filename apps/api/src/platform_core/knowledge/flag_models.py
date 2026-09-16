"""Feature flag models (Phase 4, ticket 40).

Two tables because they answer different questions:

- `feature_flags` is the definition: the key, whether it is live at all, and
  what percentage of tenants should see it.
- `feature_flag_targets` is an override: pin one specific tenant in or out,
  which is how the release process canaries a pilot tenant without pretending
  a percentage is a target.

`tenant_id` here is the tenant that *owns the flag definition*, not the
tenant being evaluated. For a flag row to be tenant-owned is what lets one
customer's rollout schedule be independent of another's, and it keeps the
flag table under the same RLS policy as every other business table
(`AGENTS.md` rule 5). The tenant being evaluated appears only as
`target_tenant_id`.
"""

import uuid

from sqlalchemy import BigInteger, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from platform_core.orm_base import Base, PkMixin, TenantMixin


class FeatureFlag(Base, PkMixin, TenantMixin):
    """A named switch with a percentage rollout."""

    __tablename__ = "feature_flags"
    __table_args__ = (UniqueConstraint("tenant_id", "key", name="uq_flag_key_per_tenant"),)

    key: Mapped[str] = mapped_column(String(127), nullable=False, index=True)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # The kill switch. False means off for everyone regardless of rollout.
    enabled: Mapped[bool] = mapped_column(nullable=False, default=False)
    # Percentage of tenants included, 0-100. Evaluated as a stable hash so a
    # given tenant's answer never changes between workers or requests.
    rollout_percent: Mapped[int] = mapped_column(nullable=False, default=0)
    created_at: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)


class FlagTarget(Base, PkMixin, TenantMixin):
    """An explicit per-tenant override of a flag's rollout.

    Overrides the percentage in both directions. Forcing a tenant in is the
    internal-tenant and pilot canary step; forcing a tenant out is how a
    known-bad customer is excluded during an incident without changing the
    rollout everyone else is on.
    """

    __tablename__ = "feature_flag_targets"
    __table_args__ = (
        UniqueConstraint("flag_id", "target_tenant_id", name="uq_flag_target"),
        Index("ix_flag_targets_flag", "flag_id"),
    )

    flag_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("feature_flags.id", ondelete="CASCADE"), nullable=False
    )
    target_tenant_id: Mapped[uuid.UUID] = mapped_column(nullable=False, index=True)
    enabled: Mapped[bool] = mapped_column(nullable=False, default=False)


__all__ = ["FeatureFlag", "FlagTarget"]
