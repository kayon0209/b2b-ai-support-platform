"""Retention sweep consumer (Phase 4/5: retention and compliance controls).

`sweep_expired_data` implements the per-tenant retention policy, but it had no
caller: the policy existed and never ran, so superseded document versions were
never expired and resolved dead letters / completed inbox events were never
pruned. This module is the caller.

Shape: a periodic, tenant-by-tenant sweep rather than a queue drain. There is
no work item to claim — the "queue" is every active tenant — so the loop lists
tenants (global reference data, readable by the app role) and sweeps each one
inside its own RLS-bound transaction. A crash mid-sweep loses only the
remaining tenants for that cycle, and the next cycle picks them up.
"""

import time
import uuid
from dataclasses import dataclass

from sqlalchemy import select

from observability import JsonLogger
from platform_core.db import session_scope_with_url
from platform_core.evaluation.pii import DEFAULT_RETENTION, RetentionPolicy, sweep_expired_data
from platform_core.identity.models import Tenant, TenantStatus
from platform_core.identity.tenant_context import TenantContext, apply_rls_tenant
from worker.wiring import app_role_url

logger = JsonLogger("platform.worker.retention")


@dataclass
class RetentionStats:
    tenants_swept: int = 0
    expired_versions: int = 0
    pruned_dead_letters: int = 0
    pruned_inbox_events: int = 0

    @property
    def changed_rows(self) -> int:
        return self.expired_versions + self.pruned_dead_letters + self.pruned_inbox_events


async def _active_tenant_ids() -> list[uuid.UUID]:
    """Active tenants.

    `tenants` is global reference data with no RLS, so the app role can list
    it before any tenant context exists — the same property auth resolution
    relies on. This is a read of ids only; every sweep below is tenant-bound.
    """
    async with session_scope_with_url(app_role_url()) as session:
        rows = await session.execute(
            select(Tenant.id).where(Tenant.status == TenantStatus.ACTIVE.value)
        )
        return list(rows.scalars().all())


async def drain_retention_once(
    *,
    policy: RetentionPolicy = DEFAULT_RETENTION,
    now: int | None = None,
) -> RetentionStats:
    """Sweep every active tenant once. Returns aggregate counts.

    Each tenant is swept in its own transaction bound to that tenant, so a
    failure on one tenant cannot roll back another's completed sweep, and RLS
    is a real boundary on the deletes rather than decoration.
    """
    ts = int(time.time()) if now is None else now
    stats = RetentionStats()

    for tenant_id in await _active_tenant_ids():
        async with session_scope_with_url(app_role_url()) as session:
            ctx = TenantContext(tenant_id=tenant_id, actor_id=None, actor_kind="system")
            await apply_rls_tenant(session, ctx)
            counts = await sweep_expired_data(session, tenant_id=tenant_id, now=ts, policy=policy)
        stats.tenants_swept += 1
        stats.expired_versions += counts.get("document_versions_expired", 0)
        stats.pruned_dead_letters += counts.get("dead_letters_pruned", 0)
        stats.pruned_inbox_events += counts.get("inbox_events_pruned", 0)

    if stats.changed_rows:
        logger.info(
            "retention_swept",
            tenants=stats.tenants_swept,
            expired_versions=stats.expired_versions,
            pruned_dead_letters=stats.pruned_dead_letters,
            pruned_inbox_events=stats.pruned_inbox_events,
        )
    return stats
