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

Two object passes live here as well, and they are not the same kind of work:

- **Erasure** runs every cycle. It touches only rows whose erasure is still
  unproven (`bytes_deleted_at IS NULL`), so a steady-state tenant does no
  storage work at all, and a tenant with a backlog works through it gradually.
- **Reconciliation** walks a tenant's prefix in the bucket, so its cost scales
  with the tenant's object count rather than with its backlog. At a one-second
  poll interval that would be a list call per tenant per second, forever. The
  caller decides when it is due (see `runner.RetentionRunner`); the default is
  off.

Storage failure never fails the row sweep. A bucket that is down should still
prune dead letters and inbox events; and because erasure only stamps
`bytes_deleted_at` after the endpoint confirms the object is gone, a failed pass
leaves the work queued rather than recording an erasure that did not happen.
"""

import time
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select

from observability import JsonLogger
from platform_core.agent_runtime.abandoned import abandon_stale_placeholders
from platform_core.config import get_settings
from platform_core.db import session_scope_with_url
from platform_core.evaluation.pii import (
    DEFAULT_RETENTION,
    RetentionPolicy,
    erase_expired_objects,
    reconcile_objects,
    sweep_expired_data,
)
from platform_core.identity.models import Tenant, TenantStatus
from platform_core.identity.tenant_context import TenantContext, apply_rls_tenant
from platform_core.knowledge.service import object_storage
from worker.wiring import app_role_url

logger = JsonLogger("platform.worker.retention")


@dataclass
class RetentionStats:
    tenants_swept: int = 0
    expired_versions: int = 0
    pruned_dead_letters: int = 0
    pruned_inbox_events: int = 0
    abandoned_runs: int = 0
    objects_erased: int = 0
    objects_failed: int = 0
    orphan_objects_removed: int = 0
    orphan_objects_failed: int = 0
    # Reported, never repaired - a row whose bytes are gone without a confirmed
    # erasure is evidence that we owe the tenant something we cannot prove.
    rows_missing_object: int = 0

    @property
    def changed_rows(self) -> int:
        return (
            self.expired_versions
            + self.pruned_dead_letters
            + self.pruned_inbox_events
            + self.abandoned_runs
            + self.objects_erased
            + self.orphan_objects_removed
        )


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


def _object_store() -> Any | None:
    """The shared object-store client, or None when it cannot be constructed.

    Returning None rather than raising is deliberate. Object erasure is a
    *completion* of the retention promise, not the whole of it, and a storage
    misconfiguration must not stop dead letters and inbox events from being
    pruned. What it must not do is mark anything as erased - and it does not:
    the erasure pass only stamps a row after the endpoint confirms the bytes
    are gone, so an unavailable store simply leaves the backlog queued.
    """
    try:
        return object_storage(get_settings())
    except Exception as exc:  # noqa: BLE001 - misconfiguration must not stop pruning
        # `error_code` (the class), not `error` (the message). The allowlist
        # drops free text on purpose - an exception message can carry a
        # connection string with a password in it - and a field the logger
        # silently discards is a warning nobody will ever read.
        logger.warning("object_store_unavailable", error_code=type(exc).__name__)
        return None


async def drain_retention_once(
    *,
    policy: RetentionPolicy = DEFAULT_RETENTION,
    now: int | None = None,
    reconcile: bool = False,
) -> RetentionStats:
    """Sweep every active tenant once. Returns aggregate counts.

    Each tenant is swept in its own transaction bound to that tenant, so a
    failure on one tenant cannot roll back another's completed sweep, and RLS
    is a real boundary on the deletes rather than decoration.

    Object erasure runs in a *second* transaction per tenant, after the row
    sweep has committed. Rolling both into one would be tidier and would also
    make every object delete - each a network round trip - hold a database
    transaction open, and would let a storage hiccup roll back the pruning that
    already succeeded.
    """
    ts = int(time.time()) if now is None else now
    stats = RetentionStats()
    storage = _object_store()

    for tenant_id in await _active_tenant_ids():
        async with session_scope_with_url(app_role_url()) as session:
            ctx = TenantContext(tenant_id=tenant_id, actor_id=None, actor_kind="system")
            await apply_rls_tenant(session, ctx)
            counts = await sweep_expired_data(session, tenant_id=tenant_id, now=ts, policy=policy)
            # Queue hygiene, in the same per-tenant transaction: an accepted
            # run that no worker ever picked up should not stay `queued`
            # forever, or the word stops distinguishing "waiting" from
            # "abandoned" and the quota counter has to guess. See
            # `agent_runtime/abandoned.py`.
            abandoned = await abandon_stale_placeholders(
                session,
                tenant_id=tenant_id,
                older_than_seconds=get_settings().run_abandon_after_seconds,
                now=ts,
            )
        stats.tenants_swept += 1
        stats.expired_versions += counts.get("document_versions_expired", 0)
        stats.pruned_dead_letters += counts.get("dead_letters_pruned", 0)
        stats.pruned_inbox_events += counts.get("inbox_events_pruned", 0)
        stats.abandoned_runs += abandoned

        if storage is None:
            continue

        async with session_scope_with_url(app_role_url()) as session:
            ctx = TenantContext(tenant_id=tenant_id, actor_id=None, actor_kind="system")
            await apply_rls_tenant(session, ctx)
            erased = await erase_expired_objects(session, storage, tenant_id=tenant_id, now=ts)
            if reconcile:
                report = await reconcile_objects(session, storage, tenant_id=tenant_id)
                stats.orphan_objects_removed += report.get("orphan_objects_removed", 0)
                stats.orphan_objects_failed += report.get("orphan_objects_failed", 0)
                stats.rows_missing_object += report.get("rows_missing_object", 0)
        stats.objects_erased += erased.get("objects_erased", 0)
        stats.objects_failed += erased.get("objects_failed", 0)

    if stats.changed_rows:
        logger.info(
            "retention_swept",
            tenants=stats.tenants_swept,
            expired_versions=stats.expired_versions,
            pruned_dead_letters=stats.pruned_dead_letters,
            pruned_inbox_events=stats.pruned_inbox_events,
            abandoned_runs=stats.abandoned_runs,
            objects_erased=stats.objects_erased,
            objects_failed=stats.objects_failed,
            orphan_objects_removed=stats.orphan_objects_removed,
            rows_missing_object=stats.rows_missing_object,
        )
    if stats.objects_failed or stats.rows_missing_object:
        # Surfaced separately: neither is a crash, and a count that is only
        # ever logged inside the `changed_rows` branch would hide them on
        # every cycle where nothing else moved.
        logger.warning(
            "retention_object_gaps",
            objects_failed=stats.objects_failed,
            rows_missing_object=stats.rows_missing_object,
            orphan_objects_failed=stats.orphan_objects_failed,
        )
    return stats
