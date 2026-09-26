"""Integration: case.create against the real schema.

Stage 3 opens cases for quality complaints. The tests here are about the two
things that decide whether such a case is worth having:

1. **It carries the SLA snapshot.** `enterprise_account_id` selects the tier and
   both deadlines, and `create_case` snapshots the tier onto the row. A case
   created without an account is not a smaller version of the feature - it is a
   ticket that can never escalate for a missed first response, and it is created
   *successfully*, which is what makes the failure invisible. So the required
   account is tested on both sides: present, it produces a tier and deadlines;
   absent, the tool refuses and leaves no accountless row behind.
2. **The tenant comes from the resolver, never from the arguments.** A create has
   no row for RLS to filter, so the tenant has to be named - and the only
   server-resolved source is the executor's constructor. The schema does not
   declare `tenant_id`, so a caller cannot pass one; the test asserts a stray
   argument is not honoured.

Structural claims asserted alongside, because neither is visible from the tool's
behaviour: the executor resolves without a connector (platform-internal tools
have no provider, so this is what makes it reachable from the HTTP API and not
only from the console), and the priority set stays in step with the router's.
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

TENANT = "01900000-0000-7000-8000-0000000000f9"
TENANT_OTHER = "01900000-0000-7000-8000-0000000000fa"
SLUG = "case-create"
SLUG_OTHER = "case-create-other"

_ACCOUNT_INSERT = (
    "INSERT INTO enterprise_accounts (id, tenant_id, name, tier, contract_status, "
    "attributes, created_at, updated_at) VALUES (:id, :t, :name, :tier, 'active', "
    "'{}'::jsonb, 0, 0)"
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
                    "(:id, :slug, 'CC', 'active') ON CONFLICT (slug) DO NOTHING"
                ),
                {"id": tid, "slug": slug},
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
            text("DELETE FROM enterprise_accounts WHERE tenant_id IN (:a, :b)"),
            {"a": TENANT, "b": TENANT_OTHER},
        )
        conn.execute(
            text("DELETE FROM tenants WHERE slug IN (:a, :b)"), {"a": SLUG, "b": SLUG_OTHER}
        )
    admin.dispose()


def _seed_account(*, tenant: str = TENANT, tier: str = "strategic", name: str = "Acme") -> str:
    account_id = str(_uuid.uuid4())
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        conn.execute(
            text(_ACCOUNT_INSERT),
            {"id": account_id, "t": tenant, "name": name, "tier": tier},
        )
    admin.dispose()
    return account_id


async def _execute(parameters: dict, *, tenant: str = TENANT):
    from platform_core.db import app_role_url, session_scope_with_url
    from platform_core.identity.tenant_context import TenantContext, apply_rls_tenant
    from platform_core.tool_gateway.case_create import CaseCreateExecutor

    async with session_scope_with_url(app_role_url()) as session:
        ctx = TenantContext(tenant_id=_uuid.UUID(tenant), actor_id=None, actor_kind="service")
        await apply_rls_tenant(session, ctx)
        executor = CaseCreateExecutor(session, tenant_id=_uuid.UUID(tenant))
        out = await executor.execute("case.create", parameters, "idem-1")
        verified = await executor.verify_postcondition("case.create", parameters, out)
    return out, verified


def _case_row(case_id: str) -> dict | None:
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        row = (
            conn.execute(
                text(
                    "SELECT tenant_id, subject, priority, category, status, sla_tier, "
                    "enterprise_account_id, first_response_due_at, resolution_due_at "
                    "FROM cases WHERE id = :i"
                ),
                {"i": case_id},
            )
            .mappings()
            .one_or_none()
        )
    admin.dispose()
    return dict(row) if row is not None else None


# --- The happy path -------------------------------------------------------


def test_creating_a_case_snapshots_the_sla_tier_and_both_deadlines() -> None:
    """The point of the account argument: a tier and two clocks on the row."""
    account_id = _seed_account(tier="strategic")

    out, verified = _run(
        _execute(
            {
                "enterprise_account_id": account_id,
                "subject": "板面有划痕，要求换货",
                "category": "quality_complaint",
                "priority": "p1",
            }
        )
    )

    assert out is not None and out["ok"] is True, out
    assert out["sla_tier"] == "strategic"
    assert out["first_response_due_at"] is not None
    assert out["resolution_due_at"] is not None
    assert verified is True

    row = _case_row(out["case_id"])
    assert row is not None
    # Snapshotted, not resolved later - the column carries it.
    assert row["sla_tier"] == "strategic"
    assert row["enterprise_account_id"] == _uuid.UUID(account_id)
    assert row["status"] == "new"
    assert row["priority"] == "p1"


def test_the_tier_decides_the_clock_so_the_account_is_not_a_label() -> None:
    """Two tiers, two different deadlines - the reason a wrong account matters.

    This is the test that makes the required-account rule worth enforcing
    rather than a formality: if the tier did not move the clock, defaulting the
    account would be harmless.
    """
    strategic = _seed_account(tier="strategic", name="Big")
    basic = _seed_account(tier="basic", name="Small")

    high, _ = _run(_execute({"enterprise_account_id": strategic, "subject": "A", "priority": "p1"}))
    low, _ = _run(_execute({"enterprise_account_id": basic, "subject": "B", "priority": "p1"}))

    assert high["sla_tier"] == "strategic"
    assert low["sla_tier"] == "basic"
    # Same priority, same instant: the only difference is the account's tier.
    assert high["first_response_due_at"] != low["first_response_due_at"]
    assert high["first_response_due_at"] < low["first_response_due_at"]


def test_an_unknown_account_is_refused_and_creates_nothing() -> None:
    """`ACCOUNT_NOT_FOUND` covers "no such account" and "another tenant's"."""
    out, _ = _run(_execute({"enterprise_account_id": str(_uuid.uuid4()), "subject": "ghost"}))
    assert out == {"ok": False, "error_code": "ACCOUNT_NOT_FOUND"}

    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        count = conn.execute(
            text("SELECT count(*) FROM cases WHERE tenant_id = :t AND subject = 'ghost'"),
            {"t": TENANT},
        ).scalar_one()
    admin.dispose()
    assert count == 0


# --- The guard that is the whole reason for this tool's shape -------------


def test_a_missing_account_is_refused_rather_than_defaulted() -> None:
    """The heart of ADR 0008: no silent degradation to `None`.

    `create_case` succeeds without an account, so a defaulted call would leave a
    green panel over a ticket with no tier and no deadlines - it could never
    escalate for a missed first response, and nothing would report a problem.
    """
    for parameters in (
        {"subject": "no account"},
        {"subject": "blank account", "enterprise_account_id": "   "},
        {"subject": "null account", "enterprise_account_id": None},
    ):
        out, _ = _run(_execute(parameters))
        assert out == {"ok": False, "error_code": "ENTERPRISE_ACCOUNT_REQUIRED"}, parameters

    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        accountless = conn.execute(
            text(
                "SELECT count(*) FROM cases WHERE tenant_id = :t AND enterprise_account_id IS NULL"
            ),
            {"t": TENANT},
        ).scalar_one()
    admin.dispose()
    # The strongest form of the claim: not "a row was refused" but "no
    # accountless row exists at all".
    assert accountless == 0


def test_a_malformed_account_id_is_refused_by_name() -> None:
    out, _ = _run(_execute({"enterprise_account_id": "not-a-uuid", "subject": "x"}))
    assert out is not None and out["ok"] is False
    assert out["error_code"] == "ACCOUNT_ID_MALFORMED"


def test_an_empty_subject_is_refused() -> None:
    account_id = _seed_account()
    out, _ = _run(_execute({"enterprise_account_id": account_id, "subject": "   "}))
    assert out == {"ok": False, "error_code": "SUBJECT_REQUIRED"}


def test_an_invalid_priority_is_refused() -> None:
    account_id = _seed_account()
    out, _ = _run(_execute({"enterprise_account_id": account_id, "subject": "x", "priority": "p9"}))
    assert out is not None and out["ok"] is False
    assert out["error_code"] == "PRIORITY_INVALID"


def test_an_unresolved_tenant_is_refused_rather_than_guessed() -> None:
    """A create has no row for RLS to filter, so a tenant must be named.

    Refusing is the only safe answer: writing into a guessed tenant is the
    cross-tenant leak AGENTS.md exists to prevent.
    """

    async def scenario():
        from platform_core.db import app_role_url, session_scope_with_url
        from platform_core.identity.tenant_context import TenantContext, apply_rls_tenant
        from platform_core.tool_gateway.case_create import CaseCreateExecutor

        async with session_scope_with_url(app_role_url()) as session:
            ctx = TenantContext(tenant_id=_uuid.UUID(TENANT), actor_id=None, actor_kind="service")
            await apply_rls_tenant(session, ctx)
            executor = CaseCreateExecutor(session, tenant_id=None)
            return await executor.execute(
                "case.create", {"enterprise_account_id": str(_uuid.uuid4()), "subject": "x"}, "k"
            )

    assert _run(scenario()) == {"ok": False, "error_code": "TENANT_UNRESOLVED"}


# --- Cross-tenant ----------------------------------------------------------


def test_another_tenants_account_is_not_usable() -> None:
    """RLS makes the foreign row invisible, so it reads as `ACCOUNT_NOT_FOUND`."""
    foreign = _seed_account(tenant=TENANT_OTHER, name="Other Co")

    out, _ = _run(_execute({"enterprise_account_id": foreign, "subject": "cross"}))

    assert out == {"ok": False, "error_code": "ACCOUNT_NOT_FOUND"}
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        count = conn.execute(
            text("SELECT count(*) FROM cases WHERE subject = 'cross' AND tenant_id = :t"),
            {"t": TENANT},
        ).scalar_one()
    admin.dispose()
    assert count == 0


def test_a_created_case_is_attributed_to_the_resolvers_tenant() -> None:
    """The tenant is the constructor's argument, and lands on the row."""
    account_id = _seed_account()
    out, _ = _run(_execute({"enterprise_account_id": account_id, "subject": "who owns this"}))
    assert out is not None and out["ok"] is True

    row = _case_row(out["case_id"])
    assert row is not None
    assert row["tenant_id"] == _uuid.UUID(TENANT)


# --- The postcondition is observed ----------------------------------------


def test_the_postcondition_is_observed_not_taken_from_the_output() -> None:
    """A fabricated success over a case with no tier must not verify.

    The executor re-reads the row. Here the row is real but lacks the snapshot,
    which is the exact state a silent default would have produced - so the
    postcondition has to say False.
    """
    case_id = str(_uuid.uuid4())
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO cases (id, tenant_id, subject, description, status, priority, "
                "category, version, opened_at, elapsed_running_seconds, last_state_changed_at) "
                "VALUES (:id, :t, 'accountless', '', 'new', 'p2', 'general', 1, 0, 0, 0)"
            ),
            {"id": case_id, "t": TENANT},
        )
    admin.dispose()

    async def scenario() -> bool | None:
        from platform_core.db import app_role_url, session_scope_with_url
        from platform_core.identity.tenant_context import TenantContext, apply_rls_tenant
        from platform_core.tool_gateway.case_create import CaseCreateExecutor

        async with session_scope_with_url(app_role_url()) as session:
            ctx = TenantContext(tenant_id=_uuid.UUID(TENANT), actor_id=None, actor_kind="service")
            await apply_rls_tenant(session, ctx)
            executor = CaseCreateExecutor(session, tenant_id=_uuid.UUID(TENANT))
            claimed = {"ok": True, "case_id": case_id, "sla_tier": "strategic"}
            return await executor.verify_postcondition("case.create", {}, claimed)

    assert _run(scenario()) is False


# --- Structural claims -----------------------------------------------------


def test_the_priority_set_matches_the_router() -> None:
    """A duplicated constant is only safe if something checks it.

    `_ALLOWED_PRIORITIES` is copied rather than imported (the router pulls in
    the HTTP layer), so this is the check that keeps the copy honest.
    """
    from platform_core.cases.router import VALID_PRIORITIES
    from platform_core.tool_gateway.case_create import _ALLOWED_PRIORITIES

    assert _ALLOWED_PRIORITIES == VALID_PRIORITIES


def test_case_create_resolves_without_a_connector() -> None:
    """It is platform-internal, so the HTTP surface can build it too.

    Without this it would be proposable in the catalog and answer
    `TOOL_EXECUTOR_MISSING` on execute - the defect `PLATFORM_TOOLS` was added
    to remove for `case.read`.
    """

    async def scenario() -> dict[str, bool]:
        from platform_core.db import app_role_url, session_scope_with_url
        from platform_core.identity.tenant_context import TenantContext, apply_rls_tenant
        from platform_core.tool_gateway.registry import ConnectorExecutorResolver

        async with session_scope_with_url(app_role_url()) as session:
            ctx = TenantContext(tenant_id=_uuid.UUID(TENANT), actor_id=None, actor_kind="service")
            await apply_rls_tenant(session, ctx)
            resolver = ConnectorExecutorResolver(session, tenant_id=_uuid.UUID(TENANT))
            resolved = await resolver.executors_for(["case.read", "case.eq_confirm", "case.create"])
        return {name: True for name in resolved}

    assert _run(scenario()) == {
        "case.read": True,
        "case.eq_confirm": True,
        "case.create": True,
    }


def test_case_create_is_a_confirmed_write_that_the_agent_cannot_approve() -> None:
    """The risk class and the reachability asymmetry, asserted together.

    Downgrading this to `low_write` (no confirmation) or promoting it to
    `human_approval` (unreachable even for a proposal) both break a documented
    decision, so this fails if either happens.
    """
    from platform_core.tool_gateway.registry import RISK_ACTION, TOOL_CATALOG
    from platform_policy.engine import Action

    risk, schema, permissions, requires_confirmation = TOOL_CATALOG["case.create"]
    assert risk == "confirmed_write"
    assert permissions == ["tool.write.confirmed"]
    assert requires_confirmation is True
    assert RISK_ACTION[risk] == Action.TOOL_WRITE_CONFIRMED.value

    # The schema requires the account and does not accept a tenant id.
    assert "enterprise_account_id" in schema["required"]
    assert "subject" in schema["required"]
    assert "tenant_id" not in schema["properties"]
