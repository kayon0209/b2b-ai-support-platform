"""Integration: case.eq_confirm against the real schema.

An EQ confirmation is what releases production against a customer's board
file, so the tests here are about what the tool *refuses*: a case that is not
an EQ confirmation, a case that is not waiting on the customer, a reference
that matches more than one case, and another tenant's case. Each of those is
a way to release production against a spec nobody agreed, and each has to fail
closed.

Two structural claims are asserted here as well, because neither is visible
from the tool's own behaviour:

- the executor resolves **without a connector** (platform-internal tools have
  no provider), which is what makes the tool reachable through the HTTP API and
  not only through the orchestrator;
- the postcondition is *observed* rather than taken from the returned dict.
"""

from __future__ import annotations

import asyncio
import os
import uuid as _uuid

import pytest
from sqlalchemy import create_engine, text

pytestmark = pytest.mark.integration

ADMIN_URL = os.environ.get(
    "APP_ADMIN_DATABASE_URL",
    "postgresql+psycopg://platform:platform@localhost:5435/platform",
)

TENANT = "01900000-0000-7000-8000-0000000000e7"
TENANT_OTHER = "01900000-0000-7000-8000-0000000000e8"
SLUG = "eq-confirm"
SLUG_OTHER = "eq-confirm-other"

_CASE_INSERT = (
    "INSERT INTO cases (id, tenant_id, subject, description, status, priority, "
    "category, version, opened_at, elapsed_running_seconds, last_state_changed_at) "
    "VALUES (:id, :t, :subject, '', :status, 'p2', :category, 1, 0, 0, 0)"
)


def _run(coro):
    return asyncio.run(coro, loop_factory=asyncio.SelectorEventLoop)


@pytest.fixture(autouse=True)
def clean_tenants() -> None:
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        for tid, slug in ((TENANT, SLUG), (TENANT_OTHER, SLUG_OTHER)):
            conn.execute(
                text(
                    "INSERT INTO tenants (id, slug, name, status) VALUES "
                    "(:id, :slug, 'EQ', 'active') ON CONFLICT (slug) DO NOTHING"
                ),
                {"id": tid, "slug": slug},
            )
        conn.execute(
            text("DELETE FROM cases WHERE tenant_id IN (:a, :b)"), {"a": TENANT, "b": TENANT_OTHER}
        )
    yield
    with admin.begin() as conn:
        conn.execute(
            text("DELETE FROM cases WHERE tenant_id IN (:a, :b)"), {"a": TENANT, "b": TENANT_OTHER}
        )
        conn.execute(
            text("DELETE FROM audit_events WHERE tenant_id IN (:a, :b)"),
            {"a": TENANT, "b": TENANT_OTHER},
        )
        conn.execute(
            text("DELETE FROM tenants WHERE slug IN (:a, :b)"), {"a": SLUG, "b": SLUG_OTHER}
        )
    admin.dispose()


def _seed_case(
    *,
    tenant: str = TENANT,
    subject: str,
    status: str = "waiting_customer",
    category: str = "eq_confirmation",
) -> str:
    case_id = str(_uuid.uuid4())
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        conn.execute(
            text(_CASE_INSERT),
            {
                "id": case_id,
                "t": tenant,
                "subject": subject,
                "status": status,
                "category": category,
            },
        )
    admin.dispose()
    return case_id


async def _execute(case_ref: str, *, tenant: str = TENANT):
    from platform_core.db import app_role_url, session_scope_with_url
    from platform_core.identity.tenant_context import TenantContext, apply_rls_tenant
    from platform_core.tool_gateway.case_eq_confirm import CaseEqConfirmExecutor

    async with session_scope_with_url(app_role_url()) as session:
        ctx = TenantContext(tenant_id=_uuid.UUID(tenant), actor_id=None, actor_kind="service")
        await apply_rls_tenant(session, ctx)
        executor = CaseEqConfirmExecutor(session)
        out = await executor.execute("case.eq_confirm", {"case_ref": case_ref}, "idem-1")
        verified = await executor.verify_postcondition(
            "case.eq_confirm", {"case_ref": case_ref}, out
        )
    return out, verified


def _status_of(case_id: str) -> str | None:
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        row = conn.execute(
            text("SELECT status FROM cases WHERE id = :i"), {"i": case_id}
        ).scalar_one_or_none()
    admin.dispose()
    return row


# --- The happy path -------------------------------------------------------


def test_confirming_an_eq_moves_the_case_off_waiting_customer() -> None:
    """`in_progress`, not `waiting_internal`: the clock must run against us.

    Both waiting states are excluded from `DEFAULT_SLA.running_states`, so
    moving to `waiting_internal` would pause the resolution clock at the moment
    the customer has done their part and the work is ours.
    """
    case_id = _seed_case(subject="EQ 55501: confirm stackup before production")

    out, verified = _run(_execute("55501"))

    assert out is not None and out["ok"] is True, out
    assert out["already_confirmed"] is False
    assert out["status"] == "in_progress"
    assert verified is True
    assert _status_of(case_id) == "in_progress"


def test_a_uuid_reference_works_too() -> None:
    """A previous reply may quote the id rather than the subject number."""
    case_id = _seed_case(subject="EQ 55502: confirm stackup")

    out, _verified = _run(_execute(case_id))

    assert out is not None and out["ok"] is True, out
    assert _status_of(case_id) == "in_progress"


def test_confirming_twice_is_reported_as_already_confirmed() -> None:
    """The customer saying yes twice is one fact, not a failure.

    Reported as success so an operator is not sent looking for a problem that
    does not exist - but the second call must not re-apply the transition,
    which the state machine would refuse anyway.
    """
    _seed_case(subject="EQ 55503: confirm stackup")

    first, _ = _run(_execute("55503"))
    second, verified = _run(_execute("55503"))

    assert first is not None and first["ok"] is True
    assert second is not None and second["ok"] is True
    assert second["already_confirmed"] is True
    assert second["status"] == "in_progress"
    assert verified is True


# --- The refusals, which are the point ------------------------------------


def test_an_ordinary_case_is_refused() -> None:
    """The category is what keeps this tool off tickets that are not EQs."""
    case_id = _seed_case(subject="EQ 55504: not really an EQ", category="general")

    out, verified = _run(_execute("55504"))

    assert out is not None and out["ok"] is False, out
    assert out["error_code"] == "CASE_NOT_AN_EQ_CONFIRMATION"
    assert verified is False
    assert _status_of(case_id) == "waiting_customer"


def test_a_case_that_is_not_waiting_on_the_customer_is_refused() -> None:
    """A confirmation recorded before the question was asked is not a
    confirmation. `new` is used rather than a later state so the refusal is
    about *not waiting*, not about being already confirmed."""
    case_id = _seed_case(subject="EQ 55505: confirm stackup", status="new")

    out, verified = _run(_execute("55505"))

    assert out is not None and out["ok"] is False, out
    assert out["error_code"] == "CASE_NOT_AWAITING_CONFIRMATION"
    assert out["status"] == "new"
    assert verified is False
    assert _status_of(case_id) == "new"


def test_an_ambiguous_reference_is_refused_rather_than_guessed() -> None:
    """Two cases matching one reference means the reference was not specific.

    Picking the first would release production against whichever case the
    database happened to return first.
    """
    _seed_case(subject="EQ 55506: rev A stackup")
    _seed_case(subject="EQ 55506: rev B stackup")

    out, _verified = _run(_execute("55506"))

    assert out is not None and out["ok"] is False, out
    assert out["error_code"] == "CASE_NOT_FOUND"


def test_an_unknown_reference_is_refused() -> None:
    out, _verified = _run(_execute("99999"))

    assert out is not None and out["ok"] is False
    assert out["error_code"] == "CASE_NOT_FOUND"


def test_another_tenants_case_is_invisible() -> None:
    """RLS, not a filter: the row is not there to be found."""
    case_id = _seed_case(tenant=TENANT_OTHER, subject="EQ 55507: other tenant")

    out, _verified = _run(_execute("55507", tenant=TENANT))

    assert out is not None and out["ok"] is False
    assert out["error_code"] == "CASE_NOT_FOUND"
    assert _status_of(case_id) == "waiting_customer"


def test_the_postcondition_is_observed_not_taken_from_the_output() -> None:
    """A returned dict claiming success is not evidence.

    Called with an output the executor never produced, verification must still
    go and look at the row - that is the difference between a postcondition and
    a restatement.
    """
    case_id = _seed_case(subject="EQ 55508: confirm stackup")

    async def scenario():
        from platform_core.db import app_role_url, session_scope_with_url
        from platform_core.identity.tenant_context import TenantContext, apply_rls_tenant
        from platform_core.tool_gateway.case_eq_confirm import CaseEqConfirmExecutor

        async with session_scope_with_url(app_role_url()) as session:
            ctx = TenantContext(tenant_id=_uuid.UUID(TENANT), actor_id=None, actor_kind="service")
            await apply_rls_tenant(session, ctx)
            executor = CaseEqConfirmExecutor(session)
            # A fabricated success for a case that is still waiting.
            claimed = {"ok": True, "case_id": case_id, "status": "in_progress"}
            return await executor.verify_postcondition("case.eq_confirm", {}, claimed)

    assert _run(scenario()) is False


# --- Reachability, which is what the registry fix bought ------------------


def test_platform_tools_resolve_without_a_connector() -> None:
    """`case.eq_confirm` and `case.read` have no provider by design.

    Before the registry built them itself, `case.read` was executable only by
    the orchestrator - which injected the executor by hand - and answered
    `TOOL_EXECUTOR_MISSING` through `POST /v1/tool-proposals/{id}/execute`.
    A tool the catalog advertises has to be runnable from every surface that
    can propose it.
    """

    async def scenario() -> dict[str, bool]:
        from platform_core.db import app_role_url, session_scope_with_url
        from platform_core.identity.tenant_context import TenantContext, apply_rls_tenant
        from platform_core.tool_gateway.registry import ConnectorExecutorResolver

        async with session_scope_with_url(app_role_url()) as session:
            ctx = TenantContext(tenant_id=_uuid.UUID(TENANT), actor_id=None, actor_kind="service")
            await apply_rls_tenant(session, ctx)
            resolver = ConnectorExecutorResolver(session, tenant_id=_uuid.UUID(TENANT))
            resolved = await resolver.executors_for(["case.read", "case.eq_confirm"])
        return {name: True for name in resolved}

    assert _run(scenario()) == {"case.read": True, "case.eq_confirm": True}
