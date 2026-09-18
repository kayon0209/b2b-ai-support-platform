"""Integration tests: connector sync and the resumable cursor.

`SyncCursor` was declared and referenced by nothing, and
`ConnectorAdapter.fetch` had no callers - so there was no way to walk a
resource and nothing that needed to remember where a walk stopped. These
tests pin the behaviour that makes the position meaningful, against real
Postgres and the real routers.

The two that matter most are
`test_a_failed_page_does_not_move_the_cursor` (a failed page that advanced the
position would skip that page forever, while looking healthy) and
`test_the_second_sync_resumes_from_the_stored_token` (the whole point of
storing a position).
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

pytestmark = pytest.mark.integration

ADMIN_URL = os.environ.get(
    "APP_ADMIN_DATABASE_URL",
    "postgresql+psycopg://platform:platform@localhost:5435/platform",
)

TENANT = "0190d000-0000-7000-8000-0000000000b1"
TENANT_OTHER = "0190d000-0000-7000-8000-0000000000b2"

_CLEAN: tuple[str, ...] = (
    "DELETE FROM sync_cursors WHERE tenant_id IN (:a, :b)",
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
        for tid, slug in ((TENANT, "sync-t1"), (TENANT_OTHER, "sync-t2")):
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
        conn.execute(text("DELETE FROM tenants WHERE slug LIKE 'sync-t%'"))
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


def _insert_connector(*, tenant: str = TENANT, provider: str = "jira") -> str:
    cid = str(uuid.uuid4())
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO connectors (id, tenant_id, provider, name, status, "
                "capabilities, configuration, credential_ref) VALUES "
                "(:id, :tid, :provider, :name, 'active', CAST('[]' AS jsonb), "
                "CAST(:cfg AS jsonb), NULL)"
            ),
            {
                "id": cid,
                "tid": tenant,
                "provider": provider,
                "name": f"{provider}-primary",
                "cfg": '{"base_url": "http://jira.test", "project_key": "SUP"}',
            },
        )
    admin.dispose()
    return cid


def _cursor_row(connector_id: str) -> tuple[str | None, int | None] | None:
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        row = conn.execute(
            text("SELECT cursor, watermark FROM sync_cursors WHERE connector_id = :id"),
            {"id": connector_id},
        ).fetchone()
    admin.dispose()
    return (row[0], row[1]) if row else None


class _FakeAdapter:
    """An adapter whose `fetch` returns a scripted page.

    Records the cursor it was handed, because "did the second call resume?"
    is only answerable from what the adapter received.
    """

    def __init__(
        self,
        *,
        records: list[Any] | None = None,
        next_cursor: str | None = None,
        raise_exc: BaseException | None = None,
    ) -> None:
        self._records = records if records is not None else []
        self._next_cursor = next_cursor
        self._raise = raise_exc
        self.seen_cursors: list[str | None] = []
        self.seen_resources: list[str] = []

    async def fetch(self, resource: str, cursor: str | None = None) -> tuple[list[Any], str | None]:
        self.seen_cursors.append(cursor)
        self.seen_resources.append(resource)
        if self._raise is not None:
            raise self._raise
        return self._records, self._next_cursor


@pytest.fixture
def install_adapter(monkeypatch: pytest.MonkeyPatch):
    """Replace adapter construction with a scripted one.

    Patched at the router's import site rather than in the registry: the unit
    under test is this endpoint's contract with the sync service, and building
    a real Jira adapter would make these tests depend on credential resolution
    and on httpx.
    """

    def _install(adapter: Any) -> Any:
        monkeypatch.setattr(
            "platform_core.integrations.router.build_adapter", lambda *a, **k: adapter
        )
        return adapter

    return _install


class _Record:
    """Stands in for an adapter's canonical projection (a frozen dataclass)."""

    def __init__(self, key: str) -> None:
        self.key = key


# --- authorization ---------------------------------------------------------


def test_sync_requires_connector_admin() -> None:
    cid = _insert_connector()
    resp = _client(TENANT, "support_admin").post(
        f"/v1/connectors/{cid}/sync", headers=_headers(), json={}
    )
    assert resp.status_code == 403, resp.text
    assert resp.json()["error"]["code"] == "POLICY_DENIED"


def test_sync_requires_an_idempotency_key() -> None:
    cid = _insert_connector()
    resp = _client(TENANT, "tenant_owner").post(
        f"/v1/connectors/{cid}/sync",
        headers={"Authorization": "Bearer pt_bootstrap_test"},
        json={},
    )
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"


def test_sync_of_another_tenants_connector_is_not_found() -> None:
    cid = _insert_connector(tenant=TENANT_OTHER)
    resp = _client(TENANT, "tenant_owner").post(
        f"/v1/connectors/{cid}/sync", headers=_headers(), json={}
    )
    assert resp.status_code == 404, resp.text
    assert _cursor_row(cid) is None


# --- capability gates ------------------------------------------------------


def test_a_provider_without_a_sync_surface_is_refused() -> None:
    """The CRM pilot is lookup-based. Reporting an empty page would look like
    "no records" and hide a capability gap."""
    cid = _insert_connector(provider="crm")
    resp = _client(TENANT, "tenant_owner").post(
        f"/v1/connectors/{cid}/sync", headers=_headers(), json={}
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == "CONNECTOR_SYNC_UNSUPPORTED"


def test_an_unknown_resource_type_is_refused() -> None:
    cid = _insert_connector()
    resp = _client(TENANT, "tenant_owner").post(
        f"/v1/connectors/{cid}/sync", headers=_headers(), json={"resource_type": "epics"}
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == "CONNECTOR_SYNC_UNSUPPORTED"


# --- the cursor ------------------------------------------------------------


def test_a_successful_sync_stores_the_position_and_audits(install_adapter) -> None:
    cid = _insert_connector()
    install_adapter(_FakeAdapter(records=[_Record("SUP-1")], next_cursor="page-2"))

    resp = _client(TENANT, "tenant_owner").post(
        f"/v1/connectors/{cid}/sync", headers=_headers(), json={}
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["fetched"] == 1
    assert body["complete"] is False

    stored = _cursor_row(cid)
    assert stored is not None
    assert stored[0] == "page-2"
    assert stored[1] is not None

    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        n = conn.execute(
            text(
                "SELECT count(*) FROM audit_events WHERE resource_id = :id "
                "AND action = 'connector.sync_completed'"
            ),
            {"id": cid},
        ).scalar_one()
    admin.dispose()
    assert n == 1


def test_the_second_sync_resumes_from_the_stored_token(install_adapter) -> None:
    """The whole point of storing a position.

    Without this, every sync restarts the walk and a large resource is never
    fully read.
    """
    cid = _insert_connector()
    client = _client(TENANT, "tenant_owner")

    first = install_adapter(_FakeAdapter(records=[], next_cursor="page-2"))
    client.post(f"/v1/connectors/{cid}/sync", headers=_headers(), json={})
    assert first.seen_cursors == [None]

    second = install_adapter(_FakeAdapter(records=[], next_cursor="page-3"))
    client.post(f"/v1/connectors/{cid}/sync", headers=_headers(), json={})
    assert second.seen_cursors == ["page-2"]

    assert _cursor_row(cid)[0] == "page-3"


def test_a_failed_page_does_not_move_the_cursor(install_adapter) -> None:
    """A failed page that advanced the position would skip that page forever,
    while the sync kept reporting success - the worst of both outcomes."""
    cid = _insert_connector()
    client = _client(TENANT, "tenant_owner")

    install_adapter(_FakeAdapter(records=[], next_cursor="page-2"))
    client.post(f"/v1/connectors/{cid}/sync", headers=_headers(), json={})
    assert _cursor_row(cid)[0] == "page-2"

    install_adapter(_FakeAdapter(raise_exc=RuntimeError("provider exploded")))
    resp = client.post(f"/v1/connectors/{cid}/sync", headers=_headers(), json={})

    assert resp.status_code == 502, resp.text
    assert resp.json()["error"]["code"] == "CONNECTOR_SYNC_FAILED"
    # The position is exactly where the last successful page left it.
    assert _cursor_row(cid)[0] == "page-2"


def test_a_provider_without_sync_surface_writes_no_cursor(install_adapter) -> None:
    """The adapter raising NotImplementedError is a capability gap, not a
    partial sync: nothing should be recorded."""
    cid = _insert_connector()
    install_adapter(_FakeAdapter(raise_exc=NotImplementedError("lookup-based")))

    resp = _client(TENANT, "tenant_owner").post(
        f"/v1/connectors/{cid}/sync", headers=_headers(), json={}
    )

    assert resp.status_code == 502, resp.text
    assert resp.json()["error"]["code"] == "CONNECTOR_SYNC_UNSUPPORTED"
    assert _cursor_row(cid) is None


def test_an_exhausted_walk_is_reported_as_complete(install_adapter) -> None:
    """No token means the end of the result set. Storing None is meaningful:
    the next sync starts over rather than resuming from a token that no longer
    addresses anything."""
    cid = _insert_connector()
    install_adapter(_FakeAdapter(records=[], next_cursor=None))

    resp = _client(TENANT, "tenant_owner").post(
        f"/v1/connectors/{cid}/sync", headers=_headers(), json={}
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["complete"] is True
    assert _cursor_row(cid)[0] is None


def test_reset_forgets_the_position(install_adapter) -> None:
    """Needed because a cursor is only valid for the query that produced it:
    after an operator changes the connector's `jql`, resuming from the old
    token would skip rows without any error."""
    cid = _insert_connector()
    client = _client(TENANT, "tenant_owner")

    install_adapter(_FakeAdapter(records=[], next_cursor="page-2"))
    client.post(f"/v1/connectors/{cid}/sync", headers=_headers(), json={})

    after_reset = install_adapter(_FakeAdapter(records=[], next_cursor="page-2"))
    resp = client.post(f"/v1/connectors/{cid}/sync", headers=_headers(), json={"reset": True})

    assert resp.status_code == 200, resp.text
    # The walk started from the beginning again.
    assert after_reset.seen_cursors == [None]


def test_the_page_size_is_bounded(install_adapter) -> None:
    cid = _insert_connector()
    resp = _client(TENANT, "tenant_owner").post(
        f"/v1/connectors/{cid}/sync", headers=_headers(), json={"page_size": 5000}
    )
    assert resp.status_code == 422, resp.text


def test_synced_records_are_returned_as_canonical_projections(install_adapter) -> None:
    cid = _insert_connector()
    install_adapter(_FakeAdapter(records=[{"key": "SUP-9", "status": "Open"}]))

    resp = _client(TENANT, "tenant_owner").post(
        f"/v1/connectors/{cid}/sync", headers=_headers(), json={}
    )

    assert resp.status_code == 200, resp.text
    records = resp.json()["records"]
    assert records == [{"key": "SUP-9", "status": "Open"}]
