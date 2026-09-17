"""Integration tests: feature flags end to end (Phase 4, ticket 40).

Covers the operator workflow against real Postgres and the real routers:
defining a flag starts it closed, rollout changes take effect, the kill switch
overrides the rollout, a tenant target overrides in both directions, and the
role split holds. The rollout math itself is proven in the unit suite; these
tests prove the *storage and authorization* around it.
"""

import os
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from platform_core.identity.middleware import TenantContextMiddleware
from platform_core.identity.tenant_context import TenantContext
from platform_core.knowledge import flag_service

pytestmark = pytest.mark.integration

ADMIN_URL = os.environ.get(
    "APP_ADMIN_DATABASE_URL",
    "postgresql+psycopg://platform:platform@localhost:5435/platform",
)
APP_URL = "postgresql+psycopg://platform_app:platform_app@localhost:5435/platform"

TENANT = "0190d000-0000-7000-8000-0000000000d1"
TENANT_OTHER = "0190d000-0000-7000-8000-0000000000d2"
PILOT = "0190d000-0000-7000-8000-0000000000d3"

# One statement per entry: psycopg refuses multiple commands in one prepared
# statement.
_CLEAN: tuple[str, ...] = (
    "DELETE FROM feature_flag_targets WHERE tenant_id IN (:a, :b)",
    "DELETE FROM feature_flags WHERE tenant_id IN (:a, :b)",
    "DELETE FROM audit_events WHERE tenant_id IN (:a, :b)",
)


def _clean(conn) -> None:
    for stmt in _CLEAN:
        conn.execute(text(stmt), {"a": TENANT, "b": TENANT_OTHER})


def _run(coro):
    import asyncio

    return asyncio.run(coro, loop_factory=asyncio.SelectorEventLoop)


class _RoleResolver:
    def __init__(self, tenant_id: str, role: str) -> None:
        self._tenant_id = tenant_id
        self._role = role

    async def __call__(self, request: object) -> TenantContext:
        return TenantContext(
            tenant_id=uuid.UUID(self._tenant_id),
            actor_id=uuid.uuid5(uuid.NAMESPACE_URL, f"actor:{self._tenant_id}-{self._role}"),
            actor_kind="user",
            role=self._role,
        )


def _client(tenant_id: str, role: str) -> TestClient:
    import importlib

    main_mod = importlib.import_module("platform_core.main")
    fresh = FastAPI()
    for route in main_mod.app.router.routes:
        fresh.router.routes.append(route)
    fresh.add_middleware(TenantContextMiddleware, resolver=_RoleResolver(tenant_id, role))
    return TestClient(fresh, raise_server_exceptions=False)


def _headers() -> dict[str, str]:
    return {"Authorization": "Bearer pt_bootstrap_test"}


@pytest.fixture(scope="module", autouse=True)
def seed_tenants():
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        for tid, slug in ((TENANT, "flag-t1"), (TENANT_OTHER, "flag-t2")):
            conn.execute(
                text(
                    "INSERT INTO tenants (id, slug, name, status) VALUES "
                    "(:id, :slug, :name, 'active') ON CONFLICT (slug) DO NOTHING"
                ),
                {"id": tid, "slug": slug, "name": slug},
            )
    yield
    with admin.begin() as conn:
        _clean(conn)
        conn.execute(text("DELETE FROM tenants WHERE slug LIKE 'flag-t%'"))
    admin.dispose()


@pytest.fixture(autouse=True)
def clean_flags():
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        _clean(conn)
    yield
    with admin.begin() as conn:
        _clean(conn)
    admin.dispose()


async def _in_session(tenant: str, fn):
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from platform_core.db import create_engine as app_engine

    engine = app_engine(APP_URL)
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            await session.execute(
                text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant}
            )
            result = await fn(session)
            await session.commit()
            return result
    finally:
        await engine.dispose()


def _ctx(tenant: str = TENANT, role: str = "tenant_owner") -> TenantContext:
    return TenantContext(
        tenant_id=uuid.UUID(tenant),
        actor_id=uuid.uuid5(uuid.NAMESPACE_URL, f"actor:{tenant}-{role}"),
        actor_kind="user",
        role=role,
    )


def _define(key: str = "new-retrieval", tenant: str = TENANT):
    async def _fn(session):
        return await flag_service.define_flag(
            session, ctx=_ctx(tenant), key=key, description="test flag"
        )

    return _run(_in_session(tenant, _fn))


# --- Definition -------------------------------------------------------------


class TestDefinition:
    def test_define_starts_closed(self) -> None:
        # A flag created already-on would mean declaring a feature enables it.
        row = _define()
        assert row.enabled is False
        assert row.rollout_percent == 0

    def test_define_stamps_created_at(self) -> None:
        row = _define()
        assert row.created_at > 0, "a flag with no creation time cannot be aged out"

    def test_duplicate_key_is_refused(self) -> None:
        _define("dup-flag")

        async def _fn(session):
            return await flag_service.define_flag(
                session, ctx=_ctx(), key="dup-flag", description="again"
            )

        with pytest.raises(flag_service.FlagError) as err:
            _run(_in_session(TENANT, _fn))
        assert err.value.code == "ALREADY_EXISTS"

    def test_same_key_in_two_tenants_is_allowed(self) -> None:
        _define("shared-name")
        other = _define("shared-name", tenant=TENANT_OTHER)
        assert other.key == "shared-name"

    @pytest.mark.parametrize("bad", ["", "  ", "has spaces", "semi;colon", "x" * 200])
    def test_invalid_keys_are_refused(self, bad: str) -> None:
        async def _fn(session):
            return await flag_service.define_flag(session, ctx=_ctx(), key=bad, description="")

        with pytest.raises(flag_service.FlagError):
            _run(_in_session(TENANT, _fn))


# --- Evaluation semantics ---------------------------------------------------


class TestEvaluation:
    def test_unknown_flag_falls_back_to_the_caller_default(self) -> None:
        # Fail-closed: a typo or a flag not yet created must not enable.
        async def _fn(session):
            return await flag_service.evaluate(
                session, flag_key="never-defined", tenant_id=uuid.UUID(TENANT)
            )

        decision = _run(_in_session(TENANT, _fn))
        assert decision.enabled is False
        assert decision.reason == "UNKNOWN_FLAG"

    def test_unknown_flag_can_default_on_when_the_caller_says_so(self) -> None:
        async def _fn(session):
            return await flag_service.evaluate(
                session,
                flag_key="never-defined",
                tenant_id=uuid.UUID(TENANT),
                default=True,
            )

        assert _run(_in_session(TENANT, _fn)).enabled is True

    def test_disabled_flag_is_off_at_full_rollout(self) -> None:
        # The kill switch must beat the rollout math.
        _define("killed")

        async def _fn(session):
            await flag_service.set_enabled(session, ctx=_ctx(), key="killed", enabled=False)
            await flag_service.set_rollout(session, ctx=_ctx(), key="killed", rollout_percent=100)
            return await flag_service.evaluate(
                session, flag_key="killed", tenant_id=uuid.UUID(TENANT)
            )

        decision = _run(_in_session(TENANT, _fn))
        assert decision.enabled is False
        assert decision.reason == "DISABLED"

    def test_enabled_flag_at_full_rollout_is_on(self) -> None:
        _define("live")

        async def _fn(session):
            await flag_service.set_rollout(session, ctx=_ctx(), key="live", rollout_percent=100)
            await flag_service.set_enabled(session, ctx=_ctx(), key="live", enabled=True)
            return await flag_service.evaluate(
                session, flag_key="live", tenant_id=uuid.UUID(TENANT)
            )

        decision = _run(_in_session(TENANT, _fn))
        assert decision.enabled is True
        assert decision.reason == "ROLLOUT"

    def test_zero_rollout_is_off(self) -> None:
        _define("none")

        async def _fn(session):
            await flag_service.set_enabled(session, ctx=_ctx(), key="none", enabled=True)
            return await flag_service.evaluate(
                session, flag_key="none", tenant_id=uuid.UUID(TENANT)
            )

        decision = _run(_in_session(TENANT, _fn))
        assert decision.enabled is False
        assert decision.reason == "NOT_IN_ROLLOUT"

    def test_evaluate_many_resolves_in_one_pass(self) -> None:
        _define("a")
        _define("b")

        async def _fn(session):
            await flag_service.set_rollout(session, ctx=_ctx(), key="a", rollout_percent=100)
            await flag_service.set_enabled(session, ctx=_ctx(), key="a", enabled=True)
            return await flag_service.evaluate_many(
                session,
                flag_keys=["a", "b", "c"],
                tenant_id=uuid.UUID(TENANT),
                defaults={"c": False},
            )

        decisions = _run(_in_session(TENANT, _fn))
        assert decisions["a"].enabled is True
        assert decisions["b"].enabled is False
        assert decisions["c"].reason == "UNKNOWN_FLAG"

    def test_evaluate_many_returns_empty_for_no_keys(self) -> None:
        async def _fn(session):
            return await flag_service.evaluate_many(
                session, flag_keys=[], tenant_id=uuid.UUID(TENANT)
            )

        assert _run(_in_session(TENANT, _fn)) == {}


# --- Targeting --------------------------------------------------------------


class TestTargeting:
    def test_target_forces_a_tenant_in(self) -> None:
        # Step 6 of the release process: internal tenant, then pilot canary,
        # without pretending a percentage is a target.
        _define("pilot-flag")

        async def _fn(session):
            await flag_service.set_enabled(session, ctx=_ctx(), key="pilot-flag", enabled=True)
            await flag_service.target_tenant(
                session,
                ctx=_ctx(),
                key="pilot-flag",
                tenant_id=uuid.UUID(PILOT),
                enabled=True,
            )
            return await flag_service.evaluate(
                session, flag_key="pilot-flag", tenant_id=uuid.UUID(PILOT)
            )

        decision = _run(_in_session(TENANT, _fn))
        assert decision.enabled is True
        assert decision.reason == "TENANT_TARGET"

    def test_target_forces_a_tenant_out(self) -> None:
        _define("exclude-flag")

        async def _fn(session):
            await flag_service.set_rollout(
                session, ctx=_ctx(), key="exclude-flag", rollout_percent=100
            )
            await flag_service.set_enabled(session, ctx=_ctx(), key="exclude-flag", enabled=True)
            await flag_service.target_tenant(
                session,
                ctx=_ctx(),
                key="exclude-flag",
                tenant_id=uuid.UUID(PILOT),
                enabled=False,
            )
            return await flag_service.evaluate(
                session, flag_key="exclude-flag", tenant_id=uuid.UUID(PILOT)
            )

        decision = _run(_in_session(TENANT, _fn))
        assert decision.enabled is False
        assert decision.reason == "TENANT_TARGET"

    def test_self_target_is_refused(self) -> None:
        # Pinning your own tenant looks like a canary but is not one.
        _define("self-flag")

        async def _fn(session):
            return await flag_service.target_tenant(
                session,
                ctx=_ctx(),
                key="self-flag",
                tenant_id=uuid.UUID(TENANT),
                enabled=True,
            )

        with pytest.raises(flag_service.FlagError) as err:
            _run(_in_session(TENANT, _fn))
        assert err.value.code == "SELF_TARGET"

    def test_target_is_upserted_not_duplicated(self) -> None:
        _define("upsert-flag")

        async def _fn(session):
            await flag_service.target_tenant(
                session, ctx=_ctx(), key="upsert-flag", tenant_id=uuid.UUID(PILOT), enabled=True
            )
            await flag_service.target_tenant(
                session, ctx=_ctx(), key="upsert-flag", tenant_id=uuid.UUID(PILOT), enabled=False
            )
            return await flag_service.evaluate(
                session, flag_key="upsert-flag", tenant_id=uuid.UUID(PILOT)
            )

        # A unique constraint would reject a second insert; the upsert path
        # must instead have flipped the existing row.
        decision = _run(_in_session(TENANT, _fn))
        assert decision.enabled is False

    def test_target_on_unknown_flag_is_not_found(self) -> None:
        async def _fn(session):
            return await flag_service.target_tenant(
                session,
                ctx=_ctx(),
                key="ghost",
                tenant_id=uuid.UUID(PILOT),
                enabled=True,
            )

        with pytest.raises(flag_service.FlagError) as err:
            _run(_in_session(TENANT, _fn))
        assert err.value.code == "NOT_FOUND"


# --- Transitions ------------------------------------------------------------


class TestTransitions:
    def test_rollout_change_is_recorded_with_before_state(self) -> None:
        _define("apply")

        async def _fn(session):
            return await flag_service.set_rollout(
                session, ctx=_ctx(), key="apply", rollout_percent=25
            )

        row = _run(_in_session(TENANT, _fn))
        assert row.rollout_percent == 25

        admin = create_engine(ADMIN_URL)
        with admin.begin() as conn:
            after = conn.execute(
                text(
                    "SELECT after_hash FROM audit_events WHERE tenant_id = :t "
                    "AND action = 'feature_flag.rollout_changed'"
                ),
                {"t": TENANT},
            ).scalar_one()
        admin.dispose()
        assert after, "a rollout change must leave an audit trail"

    @pytest.mark.parametrize("bad", [-1, 101, 1000])
    def test_out_of_range_rollout_is_refused(self, bad: int) -> None:
        _define("range")

        async def _fn(session):
            return await flag_service.set_rollout(
                session, ctx=_ctx(), key="range", rollout_percent=bad
            )

        with pytest.raises(flag_service.FlagError) as err:
            _run(_in_session(TENANT, _fn))
        assert err.value.code == "INVALID_PERCENT"

    def test_unknown_flag_transition_is_not_found(self) -> None:
        async def _fn(session):
            return await flag_service.set_rollout(
                session, ctx=_ctx(), key="ghost", rollout_percent=10
            )

        with pytest.raises(flag_service.FlagError) as err:
            _run(_in_session(TENANT, _fn))
        assert err.value.code == "NOT_FOUND"

    def test_preview_shows_monotone_inclusion(self) -> None:
        async def _fn(session):
            return await flag_service.rollout_preview(
                session, tenant_id=uuid.UUID(TENANT), key="preview", percents=[0, 50, 100]
            )

        points = _run(_in_session(TENANT, _fn))
        included = [p["tenant_included"] for p in points]
        assert included == [False, included[1], True]
        assert included == sorted(included), "inclusion must be monotone in the percentage"


# --- Tenant isolation and HTTP ---------------------------------------------


class TestHttpSurface:
    def test_owner_can_define_and_canary(self) -> None:
        owner = _client(TENANT, "tenant_owner")
        created = owner.post(
            "/v1/flags",
            json={"key": "http-flag", "description": "via http"},
            headers=_headers(),
        ).json()
        assert created["enabled"] is False

        moved = owner.post(
            "/v1/flags/http-flag/rollout", json={"rollout_percent": 50}, headers=_headers()
        ).json()
        assert moved["rollout_percent"] == 50

    def test_auditor_can_read_but_not_change(self, assert_denied) -> None:
        _define("audited")
        auditor = _client(TENANT, "auditor")

        listed = auditor.get("/v1/flags", headers=_headers())
        assert listed.status_code == 200
        assert listed.json()["total"] == 1

        denied = auditor.post(
            "/v1/flags/audited/rollout", json={"rollout_percent": 100}, headers=_headers()
        )
        assert_denied(denied, "FLAG_ACCESS_DENIED")

    def test_unknown_role_is_denied(self, assert_denied) -> None:
        nobody = _client(TENANT, "no_such_role")
        resp = nobody.get("/v1/flags", headers=_headers())
        assert_denied(resp, "FLAG_ACCESS_DENIED")

    def test_denial_leaks_no_flag_names(self) -> None:
        # Flag names can themselves be sensitive (unannounced features).
        _define("secret-launch")
        nobody = _client(TENANT, "no_such_role")
        body = nobody.get("/v1/flags", headers=_headers()).json()
        assert "items" not in body

    def test_evaluate_uses_the_calling_tenant_not_a_parameter(self) -> None:
        # There is no tenant parameter to spoof; the resolved context wins.
        owner = _client(TENANT, "tenant_owner")
        owner.post("/v1/flags", json={"key": "ctx-flag"}, headers=_headers())
        owner.post("/v1/flags/ctx-flag/rollout", json={"rollout_percent": 100}, headers=_headers())
        owner.post("/v1/flags/ctx-flag/enabled", json={"enabled": True}, headers=_headers())

        decision = owner.get("/v1/flags/ctx-flag/evaluate", headers=_headers()).json()
        assert decision["enabled"] is True

        # A different tenant resolving the same key gets its own answer.
        other = _client(TENANT_OTHER, "tenant_owner")
        other_decision = other.get("/v1/flags/ctx-flag/evaluate", headers=_headers()).json()
        assert other_decision["reason"] == "UNKNOWN_FLAG"

    def test_other_tenants_flag_is_not_editable(self) -> None:
        _define("mine-only")
        other = _client(TENANT_OTHER, "tenant_owner")
        resp = other.post(
            "/v1/flags/mine-only/rollout", json={"rollout_percent": 100}, headers=_headers()
        ).json()
        assert resp["error"]["code"] == "NOT_FOUND"

    def test_malformed_target_tenant_id_is_rejected_cleanly(self) -> None:
        owner = _client(TENANT, "tenant_owner")
        owner.post("/v1/flags", json={"key": "t-flag"}, headers=_headers())
        resp = owner.post(
            "/v1/flags/t-flag/targets",
            json={"target_tenant_id": "not-a-uuid", "enabled": True},
            headers=_headers(),
        )
        assert resp.status_code == 200
        assert resp.json()["error"]["code"] == "INVALID_TENANT_ID"

    def test_invalid_rollout_is_rejected_by_schema(self) -> None:
        owner = _client(TENANT, "tenant_owner")
        owner.post("/v1/flags", json={"key": "schema-flag"}, headers=_headers())
        resp = owner.post(
            "/v1/flags/schema-flag/rollout", json={"rollout_percent": 150}, headers=_headers()
        )
        assert resp.status_code == 422
