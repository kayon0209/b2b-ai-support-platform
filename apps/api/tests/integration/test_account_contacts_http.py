"""HTTP layer for the contact bindings: permissions, idempotency, conflicts.

The service-level tests in `test_account_contact_routing.py` prove the data
path; this file proves the endpoints around it, which is where three things
live that the service cannot tell you:

- **the permission is enforced**, and a denial is a real 403 rather than a 200
  with an error body (this repo's most repeated defect);
- **the write requires an idempotency key**;
- **a duplicate bind is a 409**, not a 500 carrying a driver message - which is
  what it was until `uq_account_contact_external` was added to `org.py`'s
  conflict map.
"""

from __future__ import annotations

import os
import uuid

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

TENANT = "01900000-0000-7000-8000-0000000000c7"
OTHER_TENANT = "01900000-0000-7000-8000-0000000000c8"
SLUG = "contacts-http"
OTHER_SLUG = "contacts-http-other"

ACCOUNT = "01900000-0000-7000-8000-0000000000d1"
CONTACT = "chatwoot-contact-7001"


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


def _seed() -> None:
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        for tid, slug in ((TENANT, SLUG), (OTHER_TENANT, OTHER_SLUG)):
            conn.execute(
                text(
                    "INSERT INTO tenants (id, slug, name, status) VALUES "
                    "(:id, :slug, :slug, 'active') ON CONFLICT (slug) DO NOTHING"
                ),
                {"id": tid, "slug": slug},
            )
        conn.execute(
            text(
                "INSERT INTO enterprise_accounts (id, tenant_id, name, tier, "
                "contract_status, attributes, created_at, updated_at) VALUES "
                "(:i, :t, 'Key Account', 'strategic', 'active', '{}'::jsonb, 0, 0) "
                "ON CONFLICT DO NOTHING"
            ),
            {"i": ACCOUNT, "t": TENANT},
        )
    admin.dispose()


def _clear() -> None:
    statements = (
        "DELETE FROM enterprise_account_contacts WHERE tenant_id = :t",
        "DELETE FROM enterprise_accounts WHERE tenant_id = :t",
    )
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        for tenant in (TENANT, OTHER_TENANT):
            for statement in statements:
                conn.execute(text(statement), {"t": tenant})
        for slug in (SLUG, OTHER_SLUG):
            conn.execute(text("DELETE FROM tenants WHERE slug = :slug"), {"slug": slug})
    admin.dispose()


@pytest.fixture(autouse=True)
def clean() -> None:
    _clear()
    _seed()
    yield
    _clear()


def _url() -> str:
    return f"/v1/identity/accounts/{ACCOUNT}/contacts"


def test_an_owner_can_bind_list_and_unbind() -> None:
    owner = _client(TENANT, "tenant_owner")

    bound = owner.post(_url(), json={"external_contact_id": CONTACT}, headers=_headers())
    assert bound.status_code == 200, bound.text[:300]

    listed = owner.get(_url(), headers=_headers())
    assert listed.status_code == 200
    assert listed.json()["contacts"] == [CONTACT]

    removed = owner.delete(f"{_url()}/{CONTACT}", headers=_headers())
    assert removed.status_code == 200
    assert owner.get(_url(), headers=_headers()).json()["contacts"] == []


def test_a_role_without_tenant_admin_is_refused() -> None:
    """A denial must be a real 403, not a 200 carrying an error body."""
    agent = _client(TENANT, "support_agent")

    denied = agent.post(_url(), json={"external_contact_id": CONTACT}, headers=_headers())

    assert denied.status_code == 403, f"got {denied.status_code}: {denied.text[:200]}"
    assert "error" in denied.json()


def test_a_write_without_an_idempotency_key_is_refused() -> None:
    owner = _client(TENANT, "tenant_owner")

    missing = owner.post(
        _url(),
        json={"external_contact_id": CONTACT},
        headers={"Authorization": "Bearer pt_bootstrap_test"},
    )

    assert missing.status_code == 400, missing.text[:200]


def test_binding_the_same_contact_twice_is_a_conflict_not_a_crash() -> None:
    """409, not 500.

    `uq_account_contact_external` is what makes the second bind impossible;
    before it was in `org.py`'s conflict map the driver's IntegrityError
    escaped as a 500 carrying a constraint name.
    """
    owner = _client(TENANT, "tenant_owner")
    assert (
        owner.post(_url(), json={"external_contact_id": CONTACT}, headers=_headers()).status_code
        == 200
    )

    again = owner.post(_url(), json={"external_contact_id": CONTACT}, headers=_headers())

    assert again.status_code == 409, f"got {again.status_code}: {again.text[:300]}"
    assert again.json()["error"]["code"] == "CONTACT_ALREADY_BOUND"


def test_binding_to_an_account_that_is_not_this_tenants_is_not_found() -> None:
    """404, and deliberately indistinguishable from "no such account"."""
    other = _client(OTHER_TENANT, "tenant_owner")

    resp = other.post(_url(), json={"external_contact_id": CONTACT}, headers=_headers())

    assert resp.status_code == 404, resp.text[:200]


def test_one_tenant_cannot_read_anothers_bindings() -> None:
    """RLS at the endpoint, not just in the service."""
    owner = _client(TENANT, "tenant_owner")
    assert (
        owner.post(_url(), json={"external_contact_id": CONTACT}, headers=_headers()).status_code
        == 200
    )

    other = _client(OTHER_TENANT, "tenant_owner")
    listed = other.get(f"/v1/identity/accounts/{ACCOUNT}/contacts", headers=_headers())

    assert listed.status_code == 200
    assert listed.json()["contacts"] == []
