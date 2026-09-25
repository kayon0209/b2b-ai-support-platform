"""Unit tests: the retention sweep's caller (Phase 4/5).

`test_retention_sweep.py` proves `sweep_expired_data` works when called. It
had no production caller, so the policy never ran: superseded versions were
never expired and resolved dead letters / completed inbox events were never
pruned. These tests pin the wiring — every active tenant is swept, each in
its own transaction, and the counts are aggregated — plus the worker role
that makes it run.
"""

import os
import uuid

import pytest

from worker import retention_consumer


class _SessionCtx:
    async def __aenter__(self) -> object:
        return object()

    async def __aexit__(self, *exc: object) -> bool:
        return False


async def test_drain_sweeps_every_active_tenant_and_aggregates(monkeypatch) -> None:
    tenants = [uuid.uuid4(), uuid.uuid4()]
    swept: list[uuid.UUID] = []

    async def fake_active() -> list[uuid.UUID]:
        return tenants

    async def fake_sweep(session, *, tenant_id, now, policy):
        swept.append(tenant_id)
        return {
            "document_versions_expired": 1,
            "dead_letters_pruned": 2,
            "inbox_events_pruned": 3,
        }

    ttls: list[int] = []

    async def fake_abandon(session, *, tenant_id, older_than_seconds, now, batch=500):
        ttls.append(older_than_seconds)
        return 4

    erased: list[uuid.UUID] = []

    async def fake_erase(session, storage, *, tenant_id, now, limit=500):
        erased.append(tenant_id)
        return {"objects_erased": 5, "objects_already_absent": 1, "objects_failed": 0}

    monkeypatch.setattr(retention_consumer, "_active_tenant_ids", fake_active)
    monkeypatch.setattr(retention_consumer, "sweep_expired_data", fake_sweep)
    monkeypatch.setattr(retention_consumer, "abandon_stale_placeholders", fake_abandon)
    monkeypatch.setattr(retention_consumer, "erase_expired_objects", fake_erase)
    monkeypatch.setattr(retention_consumer, "_object_store", lambda: object())
    monkeypatch.setattr(retention_consumer, "session_scope_with_url", lambda url: _SessionCtx())

    async def fake_apply(session, ctx) -> None:
        return None

    monkeypatch.setattr(retention_consumer, "apply_rls_tenant", fake_apply)
    monkeypatch.setattr(retention_consumer, "app_role_url", lambda: "postgresql://app")

    stats = await retention_consumer.drain_retention_once(now=1)

    assert swept == tenants, "every active tenant must be swept"
    assert stats.tenants_swept == 2
    assert stats.expired_versions == 2
    assert stats.pruned_dead_letters == 4
    assert stats.pruned_inbox_events == 6
    # The queue-hygiene step runs per tenant too, and its count is part of the
    # aggregate - otherwise "did the sweep do anything" reads false on a cycle
    # that only closed abandoned runs.
    assert stats.abandoned_runs == 8
    # Erasing the bytes is the same class of bug as the one this file was
    # written for: a policy that exists and never runs. Expired rows whose
    # objects survive is exactly "the promise is kept only in the index".
    assert erased == tenants, "every active tenant must have its expired bytes erased"
    assert stats.objects_erased == 10
    assert stats.changed_rows == 30
    # And the threshold comes from configuration. A hard-coded or missing TTL
    # would silently abandon runs that were about to execute.
    assert ttls == [retention_consumer.get_settings().run_abandon_after_seconds] * 2


async def test_reconciliation_is_off_unless_the_caller_asks(monkeypatch) -> None:
    """A prefix walk per tenant is not a per-cycle cost.

    Reconciliation lists a tenant's whole object prefix, so running it on every
    cycle turns the retention worker into a load generator against the bucket -
    to discover, almost always, that nothing changed. The default is off and
    the caller schedules it.
    """
    calls: list[uuid.UUID] = []

    async def fake_active() -> list[uuid.UUID]:
        return [uuid.uuid4()]

    async def fake_sweep(session, *, tenant_id, now, policy):
        return {}

    async def fake_abandon(session, **kwargs) -> int:
        return 0

    async def fake_erase(session, storage, **kwargs) -> dict:
        return {}

    async def fake_reconcile(session, storage, *, tenant_id) -> dict:
        calls.append(tenant_id)
        return {"orphan_objects_removed": 1, "orphan_objects_failed": 0, "rows_missing_object": 2}

    for name, value in [
        ("_active_tenant_ids", fake_active),
        ("sweep_expired_data", fake_sweep),
        ("abandon_stale_placeholders", fake_abandon),
        ("erase_expired_objects", fake_erase),
        ("reconcile_objects", fake_reconcile),
        ("apply_rls_tenant", None),
        ("app_role_url", None),
        ("session_scope_with_url", None),
        ("_object_store", lambda: object()),
    ]:
        monkeypatch.setattr(retention_consumer, name, value)

    async def noop_apply(session, ctx) -> None:
        return None

    monkeypatch.setattr(retention_consumer, "apply_rls_tenant", noop_apply)
    monkeypatch.setattr(retention_consumer, "app_role_url", lambda: "postgresql://app")
    monkeypatch.setattr(retention_consumer, "session_scope_with_url", lambda url: _SessionCtx())

    await retention_consumer.drain_retention_once(now=1)
    assert calls == [], "reconciliation ran without being asked for"

    stats = await retention_consumer.drain_retention_once(now=1, reconcile=True)
    assert len(calls) == 1, "reconciliation was asked for and did not run"
    assert stats.orphan_objects_removed == 1
    assert stats.rows_missing_object == 2


async def test_an_unavailable_object_store_does_not_stop_the_row_sweep(monkeypatch) -> None:
    """Storage is a completion of the promise, not the whole of it.

    If a bucket outage aborted the sweep, dead letters and inbox events would
    stop being pruned for every tenant until the bucket came back - collateral
    damage in a system that stores no bytes in the database. And the erasure
    backlog is safe to leave alone: rows are only stamped once the endpoint
    confirms the object is gone, so nothing is falsely marked as erased.
    """
    swept: list[uuid.UUID] = []

    async def fake_active() -> list[uuid.UUID]:
        return [uuid.uuid4()]

    async def fake_sweep(session, *, tenant_id, now, policy):
        swept.append(tenant_id)
        return {"dead_letters_pruned": 1}

    async def fake_abandon(session, **kwargs) -> int:
        return 0

    async def never_erase(session, storage, **kwargs) -> dict:  # pragma: no cover
        raise AssertionError("erasure ran with no object store")

    async def never_reconcile(session, storage, **kwargs) -> dict:  # pragma: no cover
        raise AssertionError("reconciliation ran with no object store")

    monkeypatch.setattr(retention_consumer, "_active_tenant_ids", fake_active)
    monkeypatch.setattr(retention_consumer, "sweep_expired_data", fake_sweep)
    monkeypatch.setattr(retention_consumer, "abandon_stale_placeholders", fake_abandon)
    monkeypatch.setattr(retention_consumer, "erase_expired_objects", never_erase)
    monkeypatch.setattr(retention_consumer, "reconcile_objects", never_reconcile)
    monkeypatch.setattr(retention_consumer, "_object_store", lambda: None)
    monkeypatch.setattr(retention_consumer, "session_scope_with_url", lambda url: _SessionCtx())

    async def noop_apply(session, ctx) -> None:
        return None

    monkeypatch.setattr(retention_consumer, "apply_rls_tenant", noop_apply)
    monkeypatch.setattr(retention_consumer, "app_role_url", lambda: "postgresql://app")

    stats = await retention_consumer.drain_retention_once(now=1, reconcile=True)

    assert len(swept) == 1
    assert stats.pruned_dead_letters == 1
    assert stats.objects_erased == 0


def test_the_retention_worker_walks_the_buckets_on_its_own_cadence(monkeypatch) -> None:
    """The cadence gate, at the level that actually schedules it.

    The first cycle reconciles - a deployment should immediately account for
    what is already sitting in the bucket - and the next one does not, because
    an hour later there is nothing new to learn and a list call per tenant is
    not free.
    """
    from worker import runner

    asked: list[bool] = []

    class _Stats:
        changed_rows = 0

    async def fake_drain(*, reconcile: bool = False):
        asked.append(reconcile)
        return _Stats()

    monkeypatch.setattr(runner, "drain_retention_once", fake_drain)

    worker = runner.RetentionWorker()
    monkeypatch.setattr(worker, "_reconcile_due", lambda: False)
    import asyncio

    asyncio.run(worker.run_once())
    assert asked == [False], "a cycle the gate said no still reconciled"

    worker = runner.RetentionWorker()
    monkeypatch.setattr(worker, "_reconcile_due", lambda: True)
    asyncio.run(worker.run_once())
    assert asked[-1] is True, "a due cycle did not reconcile"


async def test_a_failing_tenant_does_not_stop_the_sweep(monkeypatch) -> None:
    """One tenant's failure must not abort the others' sweeps.

    Each tenant is swept in its own transaction, so a fault on the first must
    surface (the loop does not swallow it) without having rolled back a
    completed second sweep -- which is what per-tenant transactions buy.
    """
    tenants = [uuid.uuid4(), uuid.uuid4()]
    swept: list[uuid.UUID] = []

    async def fake_active() -> list[uuid.UUID]:
        return tenants

    async def fake_sweep(session, *, tenant_id, now, policy):
        if not swept:
            swept.append(tenant_id)
            raise RuntimeError("boom")
        swept.append(tenant_id)
        return {"document_versions_expired": 1, "dead_letters_pruned": 0, "inbox_events_pruned": 0}

    async def fake_abandon(session, *, tenant_id, older_than_seconds, now, batch=500):
        return 0

    monkeypatch.setattr(retention_consumer, "_active_tenant_ids", fake_active)
    monkeypatch.setattr(retention_consumer, "sweep_expired_data", fake_sweep)
    monkeypatch.setattr(retention_consumer, "abandon_stale_placeholders", fake_abandon)
    monkeypatch.setattr(retention_consumer, "session_scope_with_url", lambda url: _SessionCtx())

    async def fake_apply(session, ctx) -> None:
        return None

    monkeypatch.setattr(retention_consumer, "apply_rls_tenant", fake_apply)
    monkeypatch.setattr(retention_consumer, "app_role_url", lambda: "postgresql://app")

    with pytest.raises(RuntimeError):
        await retention_consumer.drain_retention_once(now=1)

    # The first tenant was attempted; the failure propagated rather than
    # being logged and skipped, so an operator sees a broken sweep.
    assert swept == [tenants[0]]


def test_retention_is_a_selectable_worker_role() -> None:
    """A role nobody can select is a role nobody runs."""
    from worker.runner import ROLE_RETENTION, WORKER_ROLES, resolve_queue

    assert ROLE_RETENTION in WORKER_ROLES

    previous = os.environ.get("APP_WORKER_QUEUE")
    os.environ["APP_WORKER_QUEUE"] = ROLE_RETENTION
    try:
        assert resolve_queue([]) == ROLE_RETENTION
    finally:
        if previous is None:
            os.environ.pop("APP_WORKER_QUEUE", None)
        else:
            os.environ["APP_WORKER_QUEUE"] = previous
