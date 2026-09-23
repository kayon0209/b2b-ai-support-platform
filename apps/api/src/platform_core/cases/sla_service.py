"""Resolving, reading and writing tenant SLA targets.

The load-bearing function is `resolve_sla_policy`, and its whole contract is one
sentence: **a tenant with no configuration gets exactly what the code default
gave it before this module existed.** Not approximately - the fallback calls
`sla_policy_for_tier` itself rather than reimplementing its arithmetic, so the
two cannot drift and the existing SLA tests keep asserting the same numbers.

Three smaller decisions:

- **The tier is normalised before lookup, the same way the default normalises
  it.** A contract that is not active resolves to `standard`, and an unknown or
  absent tier resolves to `standard`, because that is what
  `TIER_TARGET_MULTIPLIERS.get(tier or "", 1.0)` already does. Looking up the
  raw tier instead would let a suspended account match a `strategic` override
  and keep a 15-minute clock it is no longer entitled to - the exact failure the
  original comment warns about.
- **`priority_multipliers` falls back per field, not per row.** A row that sets
  only the two targets keeps the default multipliers; it does not silently get
  an empty multiplier map, which would make every priority equal and quietly
  undo the p0/p3 spread.
- **`reset_sla_policy` deletes the row.** Absence is what means "default", so a
  delete *is* the reset - there is no second way to express it.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import replace

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from platform_core.cases.models import DEFAULT_SLA, SlaPolicy, sla_policy_for_tier
from platform_core.cases.sla_models import (
    MAX_TARGET_MINUTES,
    MAX_TIER,
    MIN_TARGET_MINUTES,
    SlaPolicyRow,
)

# What `sla_policy_for_tier` treats as the baseline tier. Named here so the
# lookup key and the fallback cannot disagree about it.
BASELINE_TIER = "standard"


class SlaPolicyError(ValueError):
    """A refused write. Mapped to 400 by the router."""


def _clean_tier(value: str) -> str:
    tier = (value or "").strip()[:MAX_TIER]
    if not tier:
        raise SlaPolicyError("a policy needs a tier")
    return tier


def _check_minutes(value: int, *, field: str) -> int:
    minutes = int(value)
    if not (MIN_TARGET_MINUTES <= minutes <= MAX_TARGET_MINUTES):
        raise SlaPolicyError(
            f"{field} must be between {MIN_TARGET_MINUTES} and {MAX_TARGET_MINUTES} minutes"
        )
    return minutes


def effective_tier_key(tier: str | None, contract_status: str | None) -> str:
    """The tier key a lookup should use, mirroring the default's own rules.

    Not active -> `standard`; unknown or absent -> `standard`. See the module
    docstring for why this must match rather than merely resemble the default.
    """
    from platform_core.cases.models import _is_active

    if not _is_active(contract_status):
        return BASELINE_TIER
    return (tier or "").strip()[:MAX_TIER] or BASELINE_TIER


async def get_sla_policy_row(
    session: AsyncSession, *, tenant_id: uuid.UUID, tier: str
) -> SlaPolicyRow | None:
    return (
        await session.execute(
            select(SlaPolicyRow).where(
                SlaPolicyRow.tenant_id == tenant_id, SlaPolicyRow.tier == tier
            )
        )
    ).scalar_one_or_none()


async def list_sla_policies(session: AsyncSession, *, tenant_id: uuid.UUID) -> list[SlaPolicyRow]:
    return list(
        (
            await session.execute(
                select(SlaPolicyRow)
                .where(SlaPolicyRow.tenant_id == tenant_id)
                .order_by(SlaPolicyRow.tier)
            )
        )
        .scalars()
        .all()
    )


async def resolve_sla_policy(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    tier: str | None,
    contract_status: str | None = "active",
) -> SlaPolicy:
    """The policy in force, configured or default.

    This is what the case service calls. It is async and the default is not,
    because the answer now depends on a row - but the row is optional, and when
    it is absent the answer is computed by the original function unchanged.
    """
    row = await get_sla_policy_row(
        session, tenant_id=tenant_id, tier=effective_tier_key(tier, contract_status)
    )
    if row is None:
        # Byte-identical to the pre-configuration behaviour, by construction.
        return sla_policy_for_tier(tier, contract_status=contract_status)

    multipliers = row.priority_multipliers
    return replace(
        DEFAULT_SLA,
        first_response_minutes=max(int(row.first_response_minutes), 1),
        resolution_minutes=max(int(row.resolution_minutes), 1),
        priority_multipliers=(
            dict(multipliers) if multipliers else DEFAULT_SLA.priority_multipliers
        ),
    )


async def upsert_sla_policy(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    tier: str,
    first_response_minutes: int,
    resolution_minutes: int,
    actor_id: uuid.UUID | None,
    priority_multipliers: dict[str, float] | None = None,
) -> SlaPolicyRow:
    """Create or replace one tier's targets. The caller owns the transaction."""
    clean_tier = _clean_tier(tier)
    first = _check_minutes(first_response_minutes, field="first_response_minutes")
    resolution = _check_minutes(resolution_minutes, field="resolution_minutes")
    if resolution < first:
        # A resolution window shorter than the first response is not a policy
        # anyone means; it is a transposed pair of numbers, and it would make
        # every case breach resolution before it could possibly be answered.
        raise SlaPolicyError("resolution_minutes cannot be less than first_response_minutes")

    row = await get_sla_policy_row(session, tenant_id=tenant_id, tier=clean_tier)
    now = int(time.time())
    if row is None:
        row = SlaPolicyRow(tenant_id=tenant_id, tier=clean_tier, created_at=now, updated_at=now)
        session.add(row)
    row.first_response_minutes = first
    row.resolution_minutes = resolution
    row.priority_multipliers = dict(priority_multipliers) if priority_multipliers else None
    row.updated_by = actor_id
    row.updated_at = now
    await session.flush()
    return row


async def reset_sla_policy(session: AsyncSession, *, tenant_id: uuid.UUID, tier: str) -> bool:
    """Delete one tier's configuration, restoring the code default.

    Returns whether a row was removed. Deleting a tier that was never
    configured is a no-op rather than an error - the caller asked for the
    default, and the default is what they now have.
    """
    row = await get_sla_policy_row(session, tenant_id=tenant_id, tier=_clean_tier(tier))
    if row is None:
        return False
    await session.delete(row)
    await session.flush()
    return True
