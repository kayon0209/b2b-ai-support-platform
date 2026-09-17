"""Feature flags with deterministic canary rollout (Phase 4, ticket 40).

`docs/deployment-and-operations.md` requires a release process where a change
is deployed behind a flag, enabled for the internal tenant, then canaried to
a pilot tenant, and rolled back by flipping the flag rather than redeploying.
That sequence only works if flag evaluation is *deterministic and
tenant-scoped*: a tenant must get the same answer on every request, in every
worker, forever, or a canary becomes a coin flip and a rollback becomes
unverifiable.

Design decisions that follow from that:

- **Rollout is a stable hash of (flag key, tenant id), not a counter or a
  random draw.** A counter would give different answers in different worker
  processes; a random draw would change per request. Hashing means the same
  tenant is always inside or outside the percentage, and lowering the
  percentage can only ever remove tenants (never add them), so a rollback is
  monotone.

- **Unknown flags evaluate to the default, never to enabled.** A typo in a
  flag name, or a flag that has not been created yet in this environment,
  must not silently turn a feature on. `evaluate` returns the code default
  (closed) unless a row says otherwise - fail-closed, per `AGENTS.md`'s
  stance on security-critical configuration.

- **Callers must pass an explicit default.** There is no ambient "on" state:
  a caller that forgets to supply a default gets `False`, and every call site
  documents what it is gating.

- **Kill switch wins over rollout.** `enabled=False` on a row disables it for
  every tenant including those inside the percentage, so an operator can stop
  a bad change immediately without reasoning about the canary math.

Scope note: this is per-tenant gating. Per-user bucketing would be wrong here
because tenant isolation and SLA/billing behaviour are tenant-level
properties, and a feature that is on for one seat inside a tenant but not
another cannot be reasoned about during an incident.
"""

import hashlib
import time
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from platform_core.audit import service as audit_service
from platform_core.identity.tenant_context import TenantContext
from platform_core.knowledge.flag_models import FeatureFlag, FlagTarget

# Bucket space for rollout hashing. 10_000 gives hundredth-of-a-percent
# resolution, which is finer than any canary anyone actually runs, and keeps
# the arithmetic in exact integers rather than floats that could round a
# tenant across the boundary.
BUCKET_SPACE = 10_000


class FlagError(Exception):
    """A flag definition or transition was refused."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(detail or code)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class FlagDecision:
    """Why a flag resolved the way it did.

    `reason` is deliberately explicit rather than a bool: when a canary does
    not behave as expected, "it was excluded because the tenant is not in the
    rollout" and "it was excluded because the kill switch is on" are very
    different pages for the person on call.
    """

    key: str
    enabled: bool
    reason: str
    rollout_percent: int


def stable_bucket(*, flag_key: str, tenant_id: uuid.UUID) -> int:
    """Map (flag, tenant) to a bucket in [0, BUCKET_SPACE).

    Uses the flag key in the hash input so two flags at 10% do not select the
    same tenants. That matters: rolling out feature B to the same 10% that
    already has feature A compounds the blast radius and makes a failure hard
    to attribute.
    """
    material = f"{flag_key}:{tenant_id}".encode()
    digest = hashlib.sha256(material).digest()
    return int.from_bytes(digest[:8], "big") % BUCKET_SPACE


def is_in_rollout(*, flag_key: str, tenant_id: uuid.UUID, percent: int) -> bool:
    """Whether a tenant falls inside a rollout percentage."""
    if percent <= 0:
        # Fast path, and the reason a 0% flag means "nobody" rather than
        # "whoever hashes to bucket 0".
        return False
    if percent >= 100:
        return True
    return stable_bucket(flag_key=flag_key, tenant_id=tenant_id) < percent * (BUCKET_SPACE // 100)


async def evaluate(
    session: AsyncSession,
    *,
    flag_key: str,
    tenant_id: uuid.UUID,
    default: bool = False,
) -> FlagDecision:
    """Resolve a flag for one tenant.

    `default` is what an unknown or undefined flag resolves to. It is a
    parameter rather than a constant so a call site can say "this flag gates
    a new code path, absent means use the old one" (default False) instead of
    relying on a global convention.
    """
    row = await _load_flag(session, flag_key=flag_key)
    if row is None:
        return FlagDecision(key=flag_key, enabled=default, reason="UNKNOWN_FLAG", rollout_percent=0)

    if not row.enabled:
        # Kill switch. Checked before the rollout math so an operator does
        # not have to reason about percentages to stop a bad rollout.
        return FlagDecision(
            key=flag_key,
            enabled=False,
            reason="DISABLED",
            rollout_percent=row.rollout_percent,
        )

    # An explicit tenant target overrides the percentage in both directions:
    # it can force a tenant in (the internal tenant during step 6) or pin a
    # tenant out (a known-bad customer during an incident).
    target = await _load_target(session, flag_id=row.id, tenant_id=tenant_id)
    if target is not None:
        return FlagDecision(
            key=flag_key,
            enabled=target.enabled,
            reason="TENANT_TARGET",
            rollout_percent=row.rollout_percent,
        )

    inside = is_in_rollout(flag_key=flag_key, tenant_id=tenant_id, percent=row.rollout_percent)
    return FlagDecision(
        key=flag_key,
        enabled=inside,
        reason="ROLLOUT" if inside else "NOT_IN_ROLLOUT",
        rollout_percent=row.rollout_percent,
    )


async def evaluate_many(
    session: AsyncSession,
    *,
    flag_keys: list[str],
    tenant_id: uuid.UUID,
    defaults: dict[str, bool] | None = None,
) -> dict[str, FlagDecision]:
    """Resolve several flags in one round trip.

    A request needs several flags at once; resolving them one at a time would
    put N queries on the hot path of every answer.
    """
    if not flag_keys:
        return {}

    rows = (
        await session.execute(select(FeatureFlag).where(FeatureFlag.key.in_(flag_keys)))
    ).scalars()
    by_key = {row.key: row for row in rows}
    targets = await _load_targets(
        session, flag_ids=[r.id for r in by_key.values()], tenant_id=tenant_id
    )
    resolved = defaults or {}

    out: dict[str, FlagDecision] = {}
    for key in flag_keys:
        row = by_key.get(key)
        default = resolved.get(key, False)
        if row is None:
            out[key] = FlagDecision(
                key=key, enabled=default, reason="UNKNOWN_FLAG", rollout_percent=0
            )
            continue
        if not row.enabled:
            out[key] = FlagDecision(
                key=key, enabled=False, reason="DISABLED", rollout_percent=row.rollout_percent
            )
            continue
        target = targets.get(row.id)
        if target is not None:
            out[key] = FlagDecision(
                key=key,
                enabled=target,
                reason="TENANT_TARGET",
                rollout_percent=row.rollout_percent,
            )
            continue
        inside = is_in_rollout(flag_key=key, tenant_id=tenant_id, percent=row.rollout_percent)
        out[key] = FlagDecision(
            key=key,
            enabled=inside,
            reason="ROLLOUT" if inside else "NOT_IN_ROLLOUT",
            rollout_percent=row.rollout_percent,
        )
    return out


# --- Operator workflow ------------------------------------------------------


async def define_flag(
    session: AsyncSession,
    *,
    ctx: TenantContext,
    key: str,
    description: str,
    rollout_percent: int = 0,
) -> FeatureFlag:
    """Create a flag, closed.

    Always starts at 0% and disabled: a flag created already-on would mean
    the act of declaring a feature enables it, which is the opposite of what
    a release process is for.
    """
    _validate_key(key)
    _validate_percent(rollout_percent)

    existing = await _load_flag(session, flag_key=key, owner_tenant_id=ctx.tenant_id)
    if existing is not None:
        raise FlagError("ALREADY_EXISTS", f"flag {key!r} already exists")

    row = FeatureFlag(
        tenant_id=ctx.tenant_id,
        key=key,
        description=description,
        enabled=False,
        rollout_percent=0,
        created_at=int(time.time()),
    )
    session.add(row)
    await session.flush()

    await audit_service.record(
        session,
        ctx=ctx,
        action="feature_flag.defined",
        resource_type="feature_flag",
        resource_id=row.id,
        after={"key": key, "impact": "starts disabled at 0% rollout"},
    )
    return row


async def set_rollout(
    session: AsyncSession,
    *,
    ctx: TenantContext,
    key: str,
    rollout_percent: int,
) -> FeatureFlag:
    """Change a rollout percentage. This is the canary step and the rollback."""
    _validate_percent(rollout_percent)

    row = await _load_flag(session, flag_key=key, owner_tenant_id=ctx.tenant_id)
    if row is None:
        raise FlagError("NOT_FOUND", f"no such flag {key!r} for this tenant")

    before = {"rollout_percent": row.rollout_percent, "enabled": row.enabled}
    row.rollout_percent = rollout_percent
    await session.flush()

    await audit_service.record(
        session,
        ctx=ctx,
        action="feature_flag.rollout_changed",
        resource_type="feature_flag",
        resource_id=row.id,
        before=before,
        after={"rollout_percent": rollout_percent, "enabled": row.enabled},
    )
    return row


async def set_enabled(
    session: AsyncSession, *, ctx: TenantContext, key: str, enabled: bool
) -> FeatureFlag:
    """The kill switch.

    Separate from `set_rollout` because the two answer different questions:
    "how far has this rolled out?" and "is this allowed to run at all?".
    Turning a flag off here disables it for every tenant immediately.
    """
    row = await _load_flag(session, flag_key=key, owner_tenant_id=ctx.tenant_id)
    if row is None:
        raise FlagError("NOT_FOUND", f"no such flag {key!r} for this tenant")

    before = {"rollout_percent": row.rollout_percent, "enabled": row.enabled}
    row.enabled = enabled
    await session.flush()

    await audit_service.record(
        session,
        ctx=ctx,
        action="feature_flag.enabled_changed",
        resource_type="feature_flag",
        resource_id=row.id,
        decision="completed" if enabled else "denied",
        before=before,
        after={"rollout_percent": row.rollout_percent, "enabled": enabled},
    )
    return row


async def target_tenant(
    session: AsyncSession,
    *,
    ctx: TenantContext,
    key: str,
    tenant_id: uuid.UUID,
    enabled: bool,
) -> FlagTarget:
    """Pin one tenant in or out of a flag.

    This is how step 6 of the release process ("enable for internal tenant,
    then pilot tenant canary") is expressed without abusing percentages.
    """
    row = await _load_flag(session, flag_key=key, owner_tenant_id=ctx.tenant_id)
    if row is None:
        raise FlagError("NOT_FOUND", f"no such flag {key!r} for this tenant")

    if tenant_id == ctx.tenant_id:
        # Guard against an operator pinning their own tenant by accident
        # while intending to target a pilot: self-targeting is almost always
        # a mistake, and it silently defeats the canary in the test
        # environment while looking like it worked.
        raise FlagError(
            "SELF_TARGET",
            "a tenant cannot target itself; use the kill switch or rollout instead",
        )

    existing = await _load_target(session, flag_id=row.id, tenant_id=tenant_id)
    if existing is not None:
        existing.enabled = enabled
        await session.flush()
        return existing

    target = FlagTarget(
        tenant_id=ctx.tenant_id,
        flag_id=row.id,
        target_tenant_id=tenant_id,
        enabled=enabled,
    )
    session.add(target)
    await session.flush()

    await audit_service.record(
        session,
        ctx=ctx,
        action="feature_flag.tenant_targeted",
        resource_type="feature_flag",
        resource_id=row.id,
        decision="completed" if enabled else "denied",
        after={"target_tenant_id": str(tenant_id), "enabled": enabled},
    )
    return target


async def list_flags(session: AsyncSession, *, tenant_id: uuid.UUID) -> list[FeatureFlag]:
    rows = (
        await session.execute(
            select(FeatureFlag).where(FeatureFlag.tenant_id == tenant_id).order_by(FeatureFlag.key)
        )
    ).scalars()
    return list(rows)


async def rollout_preview(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    key: str,
    percents: list[int] | None = None,
) -> list[dict[str, Any]]:
    """How a percentage change would affect a set of tenants.

    Provided because "lowering the percentage can only remove tenants, never
    add them" is the property that makes a rollback monotone, and it is worth
    being able to check that before an incident rather than during one.
    """
    candidates = percents or [0, 5, 10, 25, 50, 100]
    out: list[dict[str, Any]] = []
    for percent in candidates:
        out.append(
            {
                "percent": percent,
                "tenant_included": is_in_rollout(
                    flag_key=key,
                    tenant_id=tenant_id,
                    percent=percent,
                ),
            }
        )
    return out


# --- Internals --------------------------------------------------------------


def _validate_key(key: str) -> None:
    if not key.strip():
        raise FlagError("KEY_REQUIRED", "a flag needs a key")
    if len(key) > 127:
        raise FlagError("KEY_TOO_LONG", "a flag key is at most 127 characters")
    if not all(c.isalnum() or c in "._-" for c in key):
        # Restricted so a key is safe to use in URLs, logs and metric labels.
        raise FlagError(
            "INVALID_KEY",
            "a flag key may contain only letters, digits, dot, underscore and hyphen",
        )


def _validate_percent(percent: int) -> None:
    if not 0 <= percent <= 100:
        raise FlagError("INVALID_PERCENT", "rollout must be between 0 and 100")


async def _load_flag(
    session: AsyncSession, *, flag_key: str, owner_tenant_id: uuid.UUID | None = None
) -> FeatureFlag | None:
    """Load a flag definition, optionally scoped to its owning tenant.

    `evaluate` deliberately omits the owner scope and relies on RLS: the
    tenant being *evaluated* is not necessarily the tenant that *owns* the
    flag definition, so adding an owner predicate there would break the
    pilot-canary case. The operator paths (`set_rollout`, `set_enabled`,
    `target_tenant`) pass `owner_tenant_id` because there the actor and the
    owner are the same tenant, and a definition must not be editable across
    tenants even if RLS were ever misconfigured.
    """
    stmt = select(FeatureFlag).where(FeatureFlag.key == flag_key)
    if owner_tenant_id is not None:
        stmt = stmt.where(FeatureFlag.tenant_id == owner_tenant_id)
    return (await session.execute(stmt)).scalars().first()


async def _load_target(
    session: AsyncSession, *, flag_id: uuid.UUID, tenant_id: uuid.UUID
) -> FlagTarget | None:
    stmt = select(FlagTarget).where(
        FlagTarget.flag_id == flag_id,
        FlagTarget.target_tenant_id == tenant_id,
    )
    return (await session.execute(stmt)).scalars().first()


async def _load_targets(
    session: AsyncSession, *, flag_ids: list[uuid.UUID], tenant_id: uuid.UUID
) -> dict[uuid.UUID, bool]:
    if not flag_ids:
        return {}
    stmt = select(FlagTarget).where(
        FlagTarget.flag_id.in_(flag_ids),
        FlagTarget.target_tenant_id == tenant_id,
    )
    return {row.flag_id: row.enabled for row in (await session.execute(stmt)).scalars()}


__all__ = [
    "BUCKET_SPACE",
    "FlagDecision",
    "FlagError",
    "define_flag",
    "evaluate",
    "evaluate_many",
    "is_in_rollout",
    "list_flags",
    "rollout_preview",
    "set_enabled",
    "set_rollout",
    "stable_bucket",
    "target_tenant",
]
