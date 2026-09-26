"""Tenant-configurable SLA targets.

The gap this closes is small in code and large in practice: `DEFAULT_SLA` and
`TIER_TARGET_MULTIPLIERS` lived in `cases/models.py`, so a tenant that agreed a
four-hour first response with one customer had to change the source and redeploy
- which means, in reality, that nobody changed it and every tenant ran on the
same 60-minute clock.

What is configurable, and what deliberately is not
--------------------------------------------------
Configurable: the two **target minutes** per tier, and optionally the priority
multipliers.

Not configurable: `running_states`. The existing comment on `sla_policy_for_tier`
already argues this and the argument has not weakened - *which statuses stop a
clock is a property of the support workflow, not of what the customer bought*.
Exposing it per tenant would let two customers with the same workflow pause at
different points, and "why did this clock stop" would have no single answer. A
tenant that genuinely needs a different workflow needs a workflow change, not a
column.

Absence means "use the code default"
------------------------------------
There is no `enabled` flag and no seeded row. A tenant with no configuration gets
exactly `sla_policy_for_tier`'s answer - the behaviour that shipped before this
table existed - so the fallback is byte-identical rather than approximately the
same. That also makes **DELETE the natural reset**: removing a row restores the
default instead of leaving a disabled row that means the same thing twice.

The tier key is the one snapshotted on the Case
-----------------------------------------------
`Case.sla_tier` is written at open and re-read on a priority change, on purpose:
re-reading the account would let a mid-Case contract edit move a clock that is
already running. The lookup here honours that - it is given the snapshot, never
the account's current tier.
"""

import uuid

from sqlalchemy import BigInteger, Index, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from platform_core.orm_base import Base, PkMixin, TenantMixin

MAX_TIER = 31
# A target outside this range is a typo, not a policy. One minute is the floor
# the code default already applies; a year is beyond any support agreement and
# catches "480000" meaning "480".
MIN_TARGET_MINUTES = 1
MAX_TARGET_MINUTES = 366 * 24 * 60


class SlaPolicyRow(Base, PkMixin, TenantMixin):
    """One tier's targets for one tenant."""

    __tablename__ = "sla_policies"
    __table_args__ = (
        UniqueConstraint("tenant_id", "tier", name="uq_sla_policy_tier"),
        Index("ix_sla_policies_tenant", "tenant_id"),
    )

    # The contract tier this row overrides: `strategic`, `enterprise`,
    # `standard`, `basic`. Free-form rather than an enum: a tenant may agree a
    # tier this codebase has never heard of, and refusing it here would send
    # them back to editing source.
    tier: Mapped[str] = mapped_column(String(MAX_TIER), nullable=False)
    first_response_minutes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    resolution_minutes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # Optional. Empty means "use the default multipliers" - `DEFAULT_SLA`'s,
    # which are workflow-shaped and rarely worth overriding.
    priority_multipliers: Mapped[dict[str, float] | None] = mapped_column(JSONB, nullable=True)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    created_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
    updated_at: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
