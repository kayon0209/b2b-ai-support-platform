"""Integration tests: the compliance export against a real database.

Three properties that only a database can demonstrate:

- **tenant isolation on the read path**, with two tenants holding comparable
  rows - an export that returned another tenant's audit events would be the
  single worst defect in the platform;
- **`truncated` is honest**, because a compliance extract that silently stops
  is worse than one that fails;
- **the export audits itself without auditing its contents**, since recording
  what was exported would make the trail a copy of the data it describes.
"""

import asyncio
import json
import os
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from platform_core.audit import service as audit_service
from platform_core.cases.service import export_cases
from platform_core.db import session_scope_with_url
from platform_core.identity.middleware import TenantContextMiddleware
from platform_core.identity.tenant_context import TenantContext, apply_rls_tenant

pytestmark = pytest.mark.integration

ADMIN_URL = os.environ.get(
    "APP_ADMIN_DATABASE_URL",
    "postgresql+psycopg://platform:platform@localhost:5435/platform",
)
APP_URL = os.environ.get(
    "APP_DATABASE_URL",
    "postgresql+psycopg://platform_app:platform_app@localhost:5435/platform",
)

TENANT = "0190d000-0000-7000-8000-000000000101"
TENANT_OTHER = "0190d000-0000-7000-8000-000000000102"

NOW = 2_000_000_000
WINDOW_START = NOW - 86_400

_CLEAN: tuple[str, ...] = (
    "DELETE FROM cases WHERE tenant_id IN (:a, :b)",
    "DELETE FROM audit_events WHERE tenant_id IN (:a, :b)",
    "DELETE FROM outbox_events WHERE tenant_id IN (:a, :b)",
)


def _clean(conn) -> None:
    for stmt in _CLEAN:
        conn.execute(text(stmt), {"a": TENANT, "b": TENANT_OTHER})


def _run(coro):
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
    return {
        "Authorization": "Bearer pt_bootstrap_test",
        "Idempotency-Key": str(uuid.uuid4()),
    }


@pytest.fixture(scope="module", autouse=True)
def seed_tenants():
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        for tid, slug in ((TENANT, "comp-t1"), (TENANT_OTHER, "comp-t2")):
            conn.execute(
                text(
                    "INSERT INTO tenants (id, slug, name, status) VALUES "
                    "(:id, :slug, :name, 'active') "
                    "ON CONFLICT (slug) DO UPDATE SET id = EXCLUDED.id, status = 'active'"
                ),
                {"id": tid, "slug": slug, "name": slug},
            )
    yield
    with admin.begin() as conn:
        _clean(conn)
        conn.execute(text("DELETE FROM tenants WHERE slug LIKE 'comp-t%'"))
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


def _seed_case(
    *,
    tenant: str = TENANT,
    subject: str = "Refund not received",
    opened_at: int = WINDOW_START + 60,
) -> str:
    admin = create_engine(ADMIN_URL)
    try:
        with admin.begin() as conn:
            case_id = conn.execute(
                text(
                    "INSERT INTO cases (id, tenant_id, subject, description, category, "
                    "priority, status, version, opened_at, last_state_changed_at, "
                    "elapsed_running_seconds, metadata) VALUES "
                    "(gen_random_uuid(), :t, :subject, 'body text', 'general', 'p2', "
                    "'new', 1, :opened, :opened, 0, '{}') RETURNING id"
                ),
                {"t": tenant, "subject": subject, "opened": opened_at},
            ).scalar_one()
    finally:
        admin.dispose()
    return str(case_id)


def _seed_audit(
    *, tenant: str = TENANT, action: str = "case.created", at: int = WINDOW_START + 60
) -> None:
    admin = create_engine(ADMIN_URL)
    try:
        with admin.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO audit_events (id, tenant_id, occurred_at, actor_type, "
                    "actor_id, action, resource_type, resource_id, decision, reason_code, "
                    "trace_id, metadata) VALUES (gen_random_uuid(), :t, :at, 'user', NULL, "
                    ":action, 'case', NULL, 'completed', 'OK', 'trace-1', '{}')"
                ),
                {"t": tenant, "at": at, "action": action},
            )
    finally:
        admin.dispose()


def _export(
    *,
    tenant: str = TENANT,
    role: str = "tenant_owner",
    since: int | None = WINDOW_START,
    until: int | None = NOW,
    sections: list[str] | None = None,
    with_idempotency: bool = True,
):
    headers = _headers()
    if not with_idempotency:
        headers.pop("Idempotency-Key")
    body: dict = {}
    if since is not None:
        body["since"] = since
    if until is not None:
        body["until"] = until
    if sections is not None:
        body["sections"] = sections
    return _client(tenant, role).post("/v1/tenant/compliance/export", headers=headers, json=body)


# --- the extract ------------------------------------------------------------


def test_an_export_returns_the_tenants_own_data() -> None:
    case_id = _seed_case()
    _seed_audit()

    resp = _export()

    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert [c["case_id"] for c in payload["sections"]["cases"]] == [case_id]
    assert len(payload["sections"]["audit"]) == 1
    manifest = payload["manifest"]
    assert manifest["since"] == WINDOW_START
    assert manifest["until"] == NOW
    assert manifest["row_counts"]["cases"] == 1
    assert manifest["truncated"] == {"audit": False, "cases": False}
    # The retention that applies is part of the answer: the recipient should not
    # have to ask separately how long the platform keeps it.
    assert manifest["retention"]["superseded_version_days"] > 0


def test_the_case_window_is_by_when_the_case_arose() -> None:
    """ "Show me Q3" means Cases that arose in Q3. A window on the last update
    would hide one opened earlier and resolved inside the window - exactly the
    Case an auditor is looking for."""
    inside = _seed_case(subject="Inside", opened_at=WINDOW_START + 60)
    _seed_case(subject="Before", opened_at=WINDOW_START - 60)

    payload = _export().json()

    assert [c["case_id"] for c in payload["sections"]["cases"]] == [inside]


def test_another_tenants_data_is_not_in_the_extract() -> None:
    """The worst possible defect in this endpoint."""
    mine = _seed_case(subject="Mine")
    _seed_case(tenant=TENANT_OTHER, subject="Theirs")
    _seed_audit()
    _seed_audit(tenant=TENANT_OTHER)

    payload = _export().json()

    assert [c["case_id"] for c in payload["sections"]["cases"]] == [mine]
    assert len(payload["sections"]["audit"]) == 1
    assert "Theirs" not in json.dumps(payload)
    assert TENANT_OTHER not in json.dumps(payload)


def test_the_export_audits_itself_without_the_content() -> None:
    _seed_case(subject="A very particular subject line")

    assert _export().status_code == 200

    admin = create_engine(ADMIN_URL)
    try:
        with admin.begin() as conn:
            row = conn.execute(
                text(
                    "SELECT action, reason_code, metadata::text FROM audit_events "
                    "WHERE tenant_id = :t AND action = 'compliance.export.created'"
                ),
                {"t": TENANT},
            ).one()
    finally:
        admin.dispose()

    assert row[0] == "compliance.export.created"
    # The window and the counts, which is what an investigator needs...
    assert "row_counts" in row[2]
    # ...and not the content, because a trail that carries the payload is a
    # second copy of the data it describes.
    assert "particular subject line" not in row[2]


# --- bounds and refusals ----------------------------------------------------


def test_a_window_that_omits_the_start_is_refused() -> None:
    resp = _export(since=None)
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == "EXPORT_SINCE_REQUIRED"


def test_a_window_wider_than_the_limit_is_refused() -> None:
    resp = _export(since=NOW - (400 * 86_400))
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == "EXPORT_WINDOW_TOO_WIDE"


def test_an_unknown_section_is_refused() -> None:
    resp = _export(sections=["audit", "everything"])
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == "EXPORT_UNKNOWN_SECTION"


def test_the_idempotency_key_is_required() -> None:
    resp = _export(with_idempotency=False)
    assert resp.status_code == 400, resp.text


def test_a_support_agent_cannot_export() -> None:
    """No AUDIT_READ, so no COMPLIANCE_EXPORT: the extract returns audit events,
    and a role that cannot read the audit log cannot take a copy of it."""
    resp = _export(role="support_agent")
    assert resp.status_code == 403, resp.text


def test_an_auditor_can_export() -> None:
    _seed_case()
    resp = _export(role="auditor")
    assert resp.status_code == 200, resp.text


def test_a_narrower_section_selection_is_honoured() -> None:
    _seed_case()
    _seed_audit()

    payload = _export(sections=["audit"]).json()

    assert "cases" not in payload["sections"]
    assert len(payload["sections"]["audit"]) == 1


# --- `truncated` is honest --------------------------------------------------


def test_a_section_that_hits_the_row_ceiling_says_so() -> None:
    """Asserted against the section function with a limit of one, because
    creating 5,000 rows to test the real ceiling would be a slow test of the
    same branch. The property under test is that the flag is computed from an
    extra row rather than guessed from `len(rows) == limit` - so a page that
    ends exactly at the bound does not claim there is more."""
    _seed_case(subject="One", opened_at=WINDOW_START + 60)
    _seed_case(subject="Two", opened_at=WINDOW_START + 61)
    ctx = TenantContext(tenant_id=uuid.UUID(TENANT), actor_id=None, actor_kind="service")

    async def _drive(limit: int):
        async with session_scope_with_url(APP_URL) as session:
            await apply_rls_tenant(session, ctx)
            return await export_cases(
                session,
                tenant_id=uuid.UUID(TENANT),
                since=WINDOW_START,
                until=NOW,
                limit=limit,
            )

    cases_one, _, truncated_one = _run(_drive(1))
    cases_two, _, truncated_two = _run(_drive(2))

    assert len(cases_one) == 1 and truncated_one is True
    assert len(cases_two) == 2 and truncated_two is False


def test_the_audit_extract_carries_hashes_and_not_payloads() -> None:
    """The audit trail records that a value changed and what it hashed to. An
    extract that carried the payloads would be a copy of every secret anybody
    ever edited."""
    ctx = TenantContext(tenant_id=uuid.UUID(TENANT), actor_id=None, actor_kind="service")

    async def _seed_and_export():
        async with session_scope_with_url(APP_URL) as session:
            await apply_rls_tenant(session, ctx)
            await audit_service.record(
                session,
                ctx=ctx,
                action="connector.credential_rotated",
                resource_type="connector",
                resource_id=uuid.uuid4(),
                decision="completed",
                reason_code="OK",
                before={"credential_ref": "vault://kv/old"},
                after={"credential_ref": "vault://kv/new"},
                trace_id="trace-x",
            )
            await session.commit()
        async with session_scope_with_url(APP_URL) as session:
            await apply_rls_tenant(session, ctx)
            return await audit_service.export_events(
                session,
                tenant_id=uuid.UUID(TENANT),
                since=0,
                until=NOW + 10_000,
                limit=10,
            )

    events, truncated = _run(_seed_and_export())

    assert truncated is False
    rotated = [e for e in events if e["action"] == "connector.credential_rotated"]
    assert rotated, [e["action"] for e in events]
    event = rotated[0]
    assert event["before_hash"] and event["after_hash"]
    # The reference itself is not in the row: only its hash.
    assert "vault://kv/old" not in json.dumps(event)
