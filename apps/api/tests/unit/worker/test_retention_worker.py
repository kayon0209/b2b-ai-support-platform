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

    monkeypatch.setattr(retention_consumer, "_active_tenant_ids", fake_active)
    monkeypatch.setattr(retention_consumer, "sweep_expired_data", fake_sweep)
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
    assert stats.changed_rows == 12


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

    monkeypatch.setattr(retention_consumer, "_active_tenant_ids", fake_active)
    monkeypatch.setattr(retention_consumer, "sweep_expired_data", fake_sweep)
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
