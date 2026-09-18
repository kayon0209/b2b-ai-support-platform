"""Integration tests: dead-letter producer, listing and resolution.

The defect this pins: `DeadLetterItem` had a model and a retention sweep but
**no producer**, so a connector operation that failed every bounded retry left
nothing behind except a failed `ToolExecution` row that nothing listed.

The load-bearing test here is
`test_a_failed_connector_call_writes_a_dead_letter`: it drives
`ConnectorOutcomeExecutor` with a real database session, so it proves the
producer is wired rather than that the service function works when called by a
test. Every other test in this file would pass even if nothing in production
ever wrote a row.
"""

import asyncio
import os
import uuid
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from platform_core.identity.middleware import TenantContextMiddleware
from platform_core.identity.tenant_context import TenantContext
from platform_core.integrations import dead_letter

pytestmark = pytest.mark.integration

ADMIN_URL = os.environ.get(
    "APP_ADMIN_DATABASE_URL",
    "postgresql+psycopg://platform:platform@localhost:5435/platform",
)
APP_URL = "postgresql+psycopg://platform_app:platform_app@localhost:5435/platform"

TENANT = "0190d000-0000-7000-8000-0000000000f1"
TENANT_OTHER = "0190d000-0000-7000-8000-0000000000f2"

_CLEAN: tuple[str, ...] = (
    "DELETE FROM dead_letter_items WHERE tenant_id IN (:a, :b)",
    "DELETE FROM connectors WHERE tenant_id IN (:a, :b)",
    "DELETE FROM audit_events WHERE tenant_id IN (:a, :b)",
)


def _clean(conn) -> None:
    for stmt in _CLEAN:
        conn.execute(text(stmt), {"a": TENANT, "b": TENANT_OTHER})


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
    return {
        "Authorization": "Bearer pt_bootstrap_test",
        "Idempotency-Key": str(uuid.uuid4()),
    }


def _run(coro):
    return asyncio.run(coro, loop_factory=asyncio.SelectorEventLoop)


@pytest.fixture(scope="module", autouse=True)
def seed_tenants():
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        for tid, slug in ((TENANT, "dl-t1"), (TENANT_OTHER, "dl-t2")):
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
        conn.execute(text("DELETE FROM tenants WHERE slug LIKE 'dl-t%'"))
    admin.dispose()


@pytest.fixture(autouse=True)
def clean_rows():
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        _clean(conn)
    yield
    with admin.begin() as conn:
        _clean(conn)
    admin.dispose()


async def _in_app_session(tenant: str, fn: Any) -> Any:
    """Run `fn(session)` inside the RLS-bound app role for one tenant."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from platform_core.db import create_engine as app_engine
    from platform_core.identity.tenant_context import apply_rls_tenant

    engine = app_engine(APP_URL)
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            await apply_rls_tenant(
                session,
                TenantContext(tenant_id=uuid.UUID(tenant), actor_id=None, actor_kind="service"),
            )
            result = await fn(session)
            await session.commit()
            return result
    finally:
        await engine.dispose()


def _count_rows() -> int:
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        n = conn.execute(
            text("SELECT count(*) FROM dead_letter_items WHERE tenant_id = :t"), {"t": TENANT}
        ).scalar_one()
    admin.dispose()
    return int(n)


# --- the producer is wired -------------------------------------------------


class _FailingExecutor:
    """A ToolExecutor whose adapter reported a retry-exhausted failure."""

    def __init__(self, output: dict[str, Any]) -> None:
        self._output = output

    async def execute(
        self, tool_name: str, parameters: dict[str, Any], idempotency_key: str
    ) -> dict[str, Any] | None:
        return self._output

    async def verify_postcondition(
        self, tool_name: str, parameters: dict[str, Any], output: dict[str, Any] | None
    ) -> bool | None:
        return False


def _insert_connector() -> str:
    """Seed a connector row.

    `dead_letter_items.connector_id` is a real foreign key, so the producer
    test has to reference a connector that exists - a fabricated id fails the
    constraint, which is the schema correctly refusing a dangling reference.
    """
    cid = str(uuid.uuid4())
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO connectors (id, tenant_id, provider, name, status, "
                "capabilities, configuration, credential_ref) VALUES "
                "(:id, :tid, 'crm', 'acme-crm', 'active', "
                "CAST('[\"update_account\"]' AS jsonb), CAST('{}' AS jsonb), NULL)"
            ),
            {"id": cid, "tid": TENANT},
        )
    admin.dispose()
    return cid


def test_a_failed_connector_call_writes_a_dead_letter() -> None:
    """The end-to-end producer check.

    Without this, every other test in the file could pass while production
    never wrote a row - which is exactly the state the repo was in before,
    with a model, a retention sweep and no producer.
    """
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from platform_core.db import create_engine as app_engine
    from platform_core.identity.tenant_context import apply_rls_tenant
    from platform_core.integrations.models import Connector
    from platform_core.tool_gateway.registry import ConnectorOutcomeExecutor

    cid = _insert_connector()
    inner = _FailingExecutor(
        {"ok": False, "error_code": "CONNECTOR_UNAVAILABLE", "attempts": 3, "ambiguous": True}
    )

    async def _drive() -> None:
        engine = app_engine(APP_URL)
        try:
            factory = async_sessionmaker(engine, expire_on_commit=False)
            async with factory() as session:
                await apply_rls_tenant(
                    session,
                    TenantContext(tenant_id=uuid.UUID(TENANT), actor_id=None, actor_kind="service"),
                )
                connector = (
                    await session.execute(select(Connector).where(Connector.id == uuid.UUID(cid)))
                ).scalar_one()
                executor = ConnectorOutcomeExecutor(
                    inner, session=session, connector=connector, ctx=None
                )
                await executor.execute("crm.update_account", {"account_ref": "acme-42"}, "idem-1")
                await session.commit()
        finally:
            await engine.dispose()

    _run(_drive())

    assert _count_rows() == 1

    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        row = conn.execute(
            text(
                "SELECT operation, error_code, attempts, status, error_detail, connector_id "
                "FROM dead_letter_items WHERE tenant_id = :t"
            ),
            {"t": TENANT},
        ).one()
    admin.dispose()

    operation, error_code, attempts, status, detail, connector_id = row
    assert operation == "crm.update_account"
    assert error_code == "CONNECTOR_UNAVAILABLE"
    assert attempts == 3
    assert status == "pending"
    assert str(connector_id) == cid
    # The operator's first question about an ambiguous row is "did it land?",
    # and that is not answerable from the error code.
    assert "AMBIGUOUS_OUTCOME" in str(detail)


def test_an_auth_failure_does_not_write_a_dead_letter() -> None:
    """An auth rejection has its own queue (NEEDS_REAUTH). Duplicating it here
    would fill the dead-letter list with rows whose fix is "rotate the
    credential", not "retry the operation"."""
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from platform_core.db import create_engine as app_engine
    from platform_core.identity.tenant_context import apply_rls_tenant
    from platform_core.integrations.models import Connector
    from platform_core.tool_gateway.registry import ConnectorOutcomeExecutor

    cid = _insert_connector()
    inner = _FailingExecutor({"ok": False, "error_code": "CONNECTOR_AUTH_EXPIRED"})

    async def _drive() -> None:
        engine = app_engine(APP_URL)
        try:
            factory = async_sessionmaker(engine, expire_on_commit=False)
            async with factory() as session:
                await apply_rls_tenant(
                    session,
                    TenantContext(tenant_id=uuid.UUID(TENANT), actor_id=None, actor_kind="service"),
                )
                connector = (
                    await session.execute(select(Connector).where(Connector.id == uuid.UUID(cid)))
                ).scalar_one()
                executor = ConnectorOutcomeExecutor(
                    inner, session=session, connector=connector, ctx=None
                )
                await executor.execute("crm.update_account", {}, "idem-2")
                await session.commit()
        finally:
            await engine.dispose()

    _run(_drive())

    assert _count_rows() == 0
    # The connector was parked instead - one incident, one queue.
    assert _connector_status(cid) == "needs_reauth"


def _connector_status(connector_id: str) -> str:
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        row = conn.execute(
            text("SELECT status FROM connectors WHERE id = :id"), {"id": connector_id}
        ).scalar_one()
    admin.dispose()
    return str(row)


# --- the API ---------------------------------------------------------------


def _record_one(**overrides: Any) -> uuid.UUID:
    async def _fn(session: Any) -> uuid.UUID:
        ref = await dead_letter.record(
            session,
            tenant_id=uuid.UUID(TENANT),
            connector_id=None,
            resource_type="tool_execution",
            tool_name=overrides.get("tool_name", "crm.update_account"),
            parameters={"account_ref": "acme-42"},
            error_code=overrides.get("error_code", "CONNECTOR_UNAVAILABLE"),
            ambiguous=overrides.get("ambiguous", False),
            attempts=overrides.get("attempts", 2),
        )
        return ref.item_id

    return _run(_in_app_session(TENANT, _fn))


def test_listing_requires_connector_read() -> None:
    resp = _client(TENANT, "support_viewer").get("/v1/dead-letters", headers=_headers())
    assert resp.status_code == 403, resp.text
    assert resp.json()["error"]["code"] == "POLICY_DENIED"


def test_listing_returns_pending_rows() -> None:
    _record_one()
    resp = _client(TENANT, "support_admin").get("/v1/dead-letters", headers=_headers())
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["dead_letters"]) == 1
    assert body["counts"]["pending"] == 1
    item = body["dead_letters"][0]
    assert item["operation"] == "crm.update_account"
    assert item["status"] == "pending"
    # The digest is exposed so 40 identical failures read as one operation...
    assert item["operation_digest"].startswith("crm.update_account:")
    # ...but it must not carry the payload into an operational endpoint.
    assert "acme-42" not in resp.text


def test_listing_does_not_show_another_tenants_rows() -> None:
    async def _other(session: Any) -> None:
        await dead_letter.record(
            session,
            tenant_id=uuid.UUID(TENANT_OTHER),
            connector_id=None,
            resource_type="tool_execution",
            tool_name="crm.update_account",
            parameters={},
            error_code="CONNECTOR_UNAVAILABLE",
        )

    _run(_in_app_session(TENANT_OTHER, _other))

    resp = _client(TENANT, "support_admin").get("/v1/dead-letters", headers=_headers())
    assert resp.status_code == 200
    assert resp.json()["dead_letters"] == []


def test_listing_rejects_an_unknown_status_filter() -> None:
    resp = _client(TENANT, "support_admin").get(
        "/v1/dead-letters", params={"status": "banana"}, headers=_headers()
    )
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == "VALIDATION_FAILED"


def test_resolve_requires_connector_admin() -> None:
    item_id = _record_one()
    resp = _client(TENANT, "support_admin").post(
        f"/v1/dead-letters/{item_id}/resolve", headers=_headers()
    )
    assert resp.status_code == 403, resp.text


def test_resolve_requires_an_idempotency_key() -> None:
    item_id = _record_one()
    resp = _client(TENANT, "tenant_owner").post(
        f"/v1/dead-letters/{item_id}/resolve",
        headers={"Authorization": "Bearer pt_bootstrap_test"},
    )
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"


def test_resolve_marks_the_row_and_audits_once() -> None:
    item_id = _record_one()
    client = _client(TENANT, "tenant_owner")

    first = client.post(f"/v1/dead-letters/{item_id}/resolve", headers=_headers())
    assert first.status_code == 200, first.text
    assert first.json()["changed"] is True
    assert first.json()["dead_letter"]["status"] == "resolved"
    assert first.json()["dead_letter"]["resolved_at"] is not None

    # Two operators clicking the same button is not an incident, and must not
    # write a second audit event for a transition that did not happen.
    second = client.post(f"/v1/dead-letters/{item_id}/resolve", headers=_headers())
    assert second.status_code == 200, second.text
    assert second.json()["changed"] is False

    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        n = conn.execute(
            text(
                "SELECT count(*) FROM audit_events "
                "WHERE resource_id = :id AND action = 'dead_letter.resolved'"
            ),
            {"id": str(item_id)},
        ).scalar_one()
    admin.dispose()
    assert n == 1


def test_resolve_of_another_tenants_row_is_not_found() -> None:
    async def _other(session: Any) -> uuid.UUID:
        ref = await dead_letter.record(
            session,
            tenant_id=uuid.UUID(TENANT_OTHER),
            connector_id=None,
            resource_type="tool_execution",
            tool_name="crm.update_account",
            parameters={},
            error_code="CONNECTOR_UNAVAILABLE",
        )
        return ref.item_id

    other_id = _run(_in_app_session(TENANT_OTHER, _other))

    resp = _client(TENANT, "tenant_owner").post(
        f"/v1/dead-letters/{other_id}/resolve", headers=_headers()
    )
    assert resp.status_code == 404, resp.text
    assert resp.json()["error"]["code"] == "DEAD_LETTER_NOT_FOUND"
