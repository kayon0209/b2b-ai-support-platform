"""Integration: tenant-configurable SLA targets.

The centrepiece is `test_no_configuration_is_identical_to_the_code_default`.
Everything else here is a feature; that one is the guarantee that made this
change safe to ship, because the whole design rests on "a tenant with no rows
behaves exactly as it did before the table existed". A test that merely checked
"the numbers look reasonable" would let the fallback drift by a minute and
nothing would notice until a customer's clock moved.
"""

from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import create_engine as _admin_engine
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from platform_core.db import create_engine

pytestmark = pytest.mark.integration

ADMIN_URL = os.environ.get(
    "APP_ADMIN_DATABASE_URL",
    "postgresql+psycopg://platform:platform@localhost:5435/platform",
)
APP_URL = os.environ.get(
    "APP_TEST_DATABASE_URL",
    "postgresql+psycopg://platform_app:platform_app@localhost:5435/platform",
)

_NS = uuid.uuid5(uuid.NAMESPACE_URL, "b2b-ai-support/tests/sla-policies")
TENANT = str(uuid.uuid5(_NS, "tenant"))
TENANT_OTHER = str(uuid.uuid5(_NS, "tenant-other"))

_TEARDOWN = (
    "case_attachments",
    "case_conversations",
    "case_escalations",
    "cases",
    "sla_policies",
    "enterprise_accounts",
)


def _run(coro):
    import asyncio

    return asyncio.run(coro, loop_factory=asyncio.SelectorEventLoop)


def _wipe(where: str, params: dict) -> None:
    admin = _admin_engine(ADMIN_URL)
    with admin.begin() as conn:
        for table in _TEARDOWN:
            conn.execute(text(f"DELETE FROM {table} WHERE {where}"), params)  # noqa: S608
    admin.dispose()


@pytest.fixture(autouse=True)
def _clean():
    _wipe("tenant_id IN (:a, :b)", {"a": TENANT, "b": TENANT_OTHER})
    yield
    _wipe("tenant_id IN (:a, :b)", {"a": TENANT, "b": TENANT_OTHER})


@pytest.fixture(scope="module", autouse=True)
def _tenants():
    admin = _admin_engine(ADMIN_URL)
    with admin.begin() as conn:
        for tid, slug in ((TENANT, "sla-a"), (TENANT_OTHER, "sla-b")):
            conn.execute(
                text(
                    "INSERT INTO tenants (id, slug, name, status) VALUES "
                    "(:id, :slug, :slug, 'active') ON CONFLICT (slug) DO NOTHING"
                ),
                {"id": tid, "slug": slug},
            )
    yield
    with admin.begin() as conn:
        sub = "(SELECT id FROM tenants WHERE slug LIKE 'sla-%')"
        for table in _TEARDOWN:
            conn.execute(text(f"DELETE FROM {table} WHERE tenant_id IN {sub}"))  # noqa: S608
        conn.execute(text("DELETE FROM tenants WHERE slug LIKE 'sla-%'"))
    admin.dispose()


async def _with_session(tenant: str, fn):
    engine = create_engine(APP_URL)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            await session.execute(
                text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant}
            )
            try:
                result = await fn(session)
            except Exception:
                await session.rollback()
                raise
            await session.commit()
            return result
    finally:
        await engine.dispose()


def _resolve(tenant: str, tier: str | None, contract_status: str | None = "active"):
    from platform_core.cases.sla_service import resolve_sla_policy

    return _run(
        _with_session(
            tenant,
            lambda s: resolve_sla_policy(
                s, tenant_id=uuid.UUID(tenant), tier=tier, contract_status=contract_status
            ),
        )
    )


def _configure(tenant: str, tier: str, first: int, resolution: int, **extra) -> None:
    from platform_core.cases.sla_service import upsert_sla_policy

    async def go(session):
        return await upsert_sla_policy(
            session,
            tenant_id=uuid.UUID(tenant),
            tier=tier,
            first_response_minutes=first,
            resolution_minutes=resolution,
            actor_id=uuid.uuid4(),
            **extra,
        )

    _run(_with_session(tenant, go))


# --- the guarantee ---------------------------------------------------------


@pytest.mark.parametrize("tier", [None, "strategic", "enterprise", "standard", "basic", "weird"])
@pytest.mark.parametrize("contract_status", ["active", "suspended", None])
def test_no_configuration_is_identical_to_the_code_default(
    tier: str | None, contract_status: str | None
) -> None:
    """No rows => byte-identical to `sla_policy_for_tier`. The whole change rests
    on this, so it is asserted against the original function rather than against
    a restatement of its numbers."""
    from platform_core.cases.models import sla_policy_for_tier

    expected = sla_policy_for_tier(tier, contract_status=contract_status)
    actual = _resolve(TENANT, tier, contract_status)

    assert actual.first_response_minutes == expected.first_response_minutes
    assert actual.resolution_minutes == expected.resolution_minutes
    assert actual.priority_multipliers == expected.priority_multipliers
    assert actual.running_states == expected.running_states


# --- configuration ---------------------------------------------------------


def test_a_configured_tier_overrides_both_targets() -> None:
    _configure(TENANT, "strategic", 15, 120)

    policy = _resolve(TENANT, "strategic")
    assert policy.first_response_minutes == 15
    assert policy.resolution_minutes == 120
    # Workflow is not configurable, so it must be untouched.
    assert policy.running_states


def test_another_tier_is_unaffected_by_the_override() -> None:
    """A per-tier override must not leak across tiers."""
    from platform_core.cases.models import sla_policy_for_tier

    _configure(TENANT, "strategic", 15, 120)

    standard = _resolve(TENANT, "standard")
    assert standard.first_response_minutes == sla_policy_for_tier("standard").first_response_minutes


def test_a_suspended_contract_does_not_get_the_tier_override() -> None:
    """The rule the original comment warns about: a tighter clock a customer is
    no longer entitled to would fire escalations nobody agreed to."""
    from platform_core.cases.models import sla_policy_for_tier

    _configure(TENANT, "strategic", 15, 120)

    suspended = _resolve(TENANT, "strategic", "suspended")
    assert (
        suspended.first_response_minutes
        == sla_policy_for_tier("strategic", contract_status="suspended").first_response_minutes
    )


def test_priority_multipliers_fall_back_field_by_field() -> None:
    """A row that sets only the targets keeps the default p0/p3 spread."""
    from platform_core.cases.models import DEFAULT_SLA

    _configure(TENANT, "standard", 30, 240)

    policy = _resolve(TENANT, "standard")
    assert policy.priority_multipliers == DEFAULT_SLA.priority_multipliers


def test_priority_multipliers_are_used_when_supplied() -> None:
    _configure(TENANT, "standard", 30, 240, priority_multipliers={"p0": 0.1})

    assert _resolve(TENANT, "standard").priority_multipliers == {"p0": 0.1}


def test_reset_restores_the_default() -> None:
    from platform_core.cases.models import sla_policy_for_tier
    from platform_core.cases.sla_service import reset_sla_policy

    _configure(TENANT, "standard", 30, 240)
    assert _resolve(TENANT, "standard").first_response_minutes == 30

    removed = _run(
        _with_session(
            TENANT,
            lambda s: reset_sla_policy(s, tenant_id=uuid.UUID(TENANT), tier="standard"),
        )
    )
    assert removed is True
    assert (
        _resolve(TENANT, "standard").first_response_minutes
        == sla_policy_for_tier("standard").first_response_minutes
    )

    # Resetting something that was never configured is a no-op, not an error:
    # the caller asked for the default and now has it.
    again = _run(
        _with_session(
            TENANT,
            lambda s: reset_sla_policy(s, tenant_id=uuid.UUID(TENANT), tier="standard"),
        )
    )
    assert again is False


# --- validation ------------------------------------------------------------


def test_a_transposed_pair_is_refused() -> None:
    from platform_core.cases.sla_service import SlaPolicyError, upsert_sla_policy

    async def go(session):
        with pytest.raises(SlaPolicyError, match="cannot be less than"):
            await upsert_sla_policy(
                session,
                tenant_id=uuid.UUID(TENANT),
                tier="standard",
                first_response_minutes=480,
                resolution_minutes=60,
                actor_id=uuid.uuid4(),
            )

    _run(_with_session(TENANT, go))


def test_an_out_of_range_target_is_refused() -> None:
    from platform_core.cases.sla_service import SlaPolicyError, upsert_sla_policy

    async def go(session):
        with pytest.raises(SlaPolicyError, match="between"):
            await upsert_sla_policy(
                session,
                tenant_id=uuid.UUID(TENANT),
                tier="standard",
                first_response_minutes=0,
                resolution_minutes=60,
                actor_id=uuid.uuid4(),
            )

    _run(_with_session(TENANT, go))


def test_an_empty_tier_is_refused() -> None:
    from platform_core.cases.sla_service import SlaPolicyError, upsert_sla_policy

    async def go(session):
        with pytest.raises(SlaPolicyError, match="needs a tier"):
            await upsert_sla_policy(
                session,
                tenant_id=uuid.UUID(TENANT),
                tier="   ",
                first_response_minutes=60,
                resolution_minutes=120,
                actor_id=uuid.uuid4(),
            )

    _run(_with_session(TENANT, go))


# --- isolation -------------------------------------------------------------


def test_another_tenant_neither_sees_nor_inherits_our_policy() -> None:
    from platform_core.cases.models import sla_policy_for_tier
    from platform_core.cases.sla_service import list_sla_policies

    _configure(TENANT, "standard", 30, 240)

    theirs = _run(
        _with_session(
            TENANT_OTHER, lambda s: list_sla_policies(s, tenant_id=uuid.UUID(TENANT_OTHER))
        )
    )
    assert theirs == []
    # ...and their resolution still falls back to the code default.
    assert (
        _resolve(TENANT_OTHER, "standard").first_response_minutes
        == sla_policy_for_tier("standard").first_response_minutes
    )


# --- the wiring ------------------------------------------------------------


def test_creating_a_case_uses_the_configured_policy() -> None:
    """The end-to-end proof: the override reaches a real Case's deadline.

    Without this the resolution logic could be perfect and unwired, which is the
    recurring defect in this repository - a capability with no consumer.
    """
    from platform_core.cases.service import CaseService

    # 7 is deliberately not a number the code default can produce for
    # `strategic` (60 * 0.25 = 15) - otherwise this test would pass with the
    # wiring removed, which is the defect it exists to catch.
    _configure(TENANT, "strategic", 7, 90)
    account_id = uuid.uuid4()
    admin = _admin_engine(ADMIN_URL)
    with admin.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO enterprise_accounts (id, tenant_id, name, tier, contract_status) "
                "VALUES (:id, :t, 'Acme', 'strategic', 'active')"
            ),
            {"id": str(account_id), "t": TENANT},
        )
    admin.dispose()

    async def create(session):
        return await CaseService(session).create_case(
            tenant_id=uuid.UUID(TENANT),
            subject="加急",
            priority="p2",
            enterprise_account_id=account_id,
        )

    case = _run(_with_session(TENANT, create))

    assert case.sla_tier == "strategic"
    # p2 multiplier is 1.0, so the deadline is exactly the configured window.
    assert case.first_response_due_at == case.opened_at + 7 * 60
    assert case.resolution_due_at == case.opened_at + 90 * 60


def test_a_case_for_a_tenant_with_no_configuration_keeps_the_default_window() -> None:
    """The same path, unconfigured - so the two tests differ only by the row."""
    from platform_core.cases.service import CaseService

    account_id = uuid.uuid4()
    admin = _admin_engine(ADMIN_URL)
    with admin.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO enterprise_accounts (id, tenant_id, name, tier, contract_status) "
                "VALUES (:id, :t, 'Acme', 'strategic', 'active')"
            ),
            {"id": str(account_id), "t": TENANT},
        )
    admin.dispose()

    async def create(session):
        return await CaseService(session).create_case(
            tenant_id=uuid.UUID(TENANT),
            subject="加急",
            priority="p2",
            enterprise_account_id=account_id,
        )

    case = _run(_with_session(TENANT, create))
    # strategic's code multiplier is 0.25 on a 60-minute default.
    assert case.first_response_due_at == case.opened_at + 15 * 60
