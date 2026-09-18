"""SLA escalation consumer: the caller `is_breached` never had.

`cases.models.is_breached` could answer "has this deadline passed" and had no
caller, so a breached Case produced no event, no notification and no record.
This is the missing caller.

Shape: a periodic sweep, tenant by tenant, one RLS-bound transaction each -
the same shape as the retention sweep and for the same reason. There is no work
item to claim (the "queue" is every open Case with a passed deadline), and a
crash mid-sweep loses only the remaining tenants for that cycle. Idempotency
does not come from claiming: it comes from `case_escalations`' unique key, so a
second worker starting mid-sweep is safe by construction rather than by
coordination.
"""

import time
import uuid
from dataclasses import dataclass

from sqlalchemy import select

from observability import JsonLogger
from platform_core.cases.escalation import EscalationStats, escalate_due_cases
from platform_core.db import session_scope_with_url
from platform_core.identity.models import Tenant, TenantStatus
from platform_core.identity.tenant_context import TenantContext, apply_rls_tenant
from worker.wiring import app_role_url

logger = JsonLogger("platform.worker.sla")


@dataclass
class SlaSweepStats:
    tenants_swept: int = 0
    scanned: int = 0
    escalated: int = 0
    skipped_already_escalated: int = 0


async def _active_tenant_ids() -> list[uuid.UUID]:
    """`tenants` is global reference data with no RLS, so the app role can list
    it before any tenant context exists - the same property auth resolution
    relies on. Ids only; every sweep below is tenant-bound."""
    async with session_scope_with_url(app_role_url()) as session:
        rows = await session.execute(
            select(Tenant.id).where(Tenant.status == TenantStatus.ACTIVE.value)
        )
        return list(rows.scalars().all())


async def drain_sla_once(*, now: int | None = None, limit: int = 200) -> SlaSweepStats:
    """One sweep across every active tenant.

    The transaction is per tenant and committed here, which is the opposite of
    the ingestion drain's contract (it leaves the unit of work to its caller).
    The difference is deliberate: a sweep has no batch boundary to respect -
    every tenant is independent - and holding one transaction across all of
    them would make a single slow tenant stall the rest.
    """
    now = now or int(time.time())
    stats = SlaSweepStats()

    for tenant_id in await _active_tenant_ids():
        try:
            per_tenant = await _sweep_tenant(tenant_id, now=now, limit=limit)
        except Exception as exc:  # noqa: BLE001 - one tenant must not stop the sweep
            logger.error(
                "sla_sweep_failed",
                error_code=type(exc).__name__,
                tenant_ref=str(tenant_id),
            )
            continue

        stats.tenants_swept += 1
        stats.scanned += per_tenant.scanned
        stats.escalated += per_tenant.escalated
        stats.skipped_already_escalated += per_tenant.skipped_already_escalated

    if stats.escalated:
        # A nonzero count is worth a line: it means commitments were missed,
        # and the number is the only place that is summarized rather than
        # buried in per-Case audit events.
        logger.warning("sla_escalations_recorded", count=stats.escalated)
    return stats


async def _sweep_tenant(tenant_id: uuid.UUID, *, now: int, limit: int) -> EscalationStats:
    ctx = TenantContext(tenant_id=tenant_id, actor_id=None, actor_kind="service")
    async with session_scope_with_url(app_role_url()) as session:
        await apply_rls_tenant(session, ctx)
        stats = await escalate_due_cases(
            session, tenant_id=tenant_id, ctx=ctx, now=now, limit=limit
        )
        await session.commit()
    return stats


__all__ = ["SlaSweepStats", "drain_sla_once"]
