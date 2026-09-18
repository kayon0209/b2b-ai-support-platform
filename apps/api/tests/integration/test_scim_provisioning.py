"""Integration tests: SCIM provisioning against a real database.

The protocol layer is tested without a database (`test_scim.py`). What only a
database can show is that the token **establishes** the tenant and that the
tenant boundary holds afterwards - and that a Group becomes a Department and
never a role, which is the decision that keeps an IdP administrator from being
able to grant themselves `tenant_owner`.
"""

import hashlib
import os
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

pytestmark = pytest.mark.integration

ADMIN_URL = os.environ.get(
    "APP_ADMIN_DATABASE_URL",
    "postgresql+psycopg://platform:platform@localhost:5435/platform",
)

TENANT = "0190d000-0000-7000-8000-000000000401"
TENANT_OTHER = "0190d000-0000-7000-8000-000000000402"
TOKEN = "scim-token-for-tenant-one-0123456789"
TOKEN_OTHER = "scim-token-for-tenant-two-0123456789"


def _clean(conn) -> None:
    conn.execute(
        text("DELETE FROM memberships WHERE tenant_id IN (:a, :b)"),
        {"a": TENANT, "b": TENANT_OTHER},
    )
    conn.execute(
        text("DELETE FROM departments WHERE tenant_id IN (:a, :b)"),
        {"a": TENANT, "b": TENANT_OTHER},
    )
    conn.execute(
        text("DELETE FROM audit_events WHERE tenant_id IN (:a, :b)"),
        {"a": TENANT, "b": TENANT_OTHER},
    )
    conn.execute(text("DELETE FROM users WHERE primary_email LIKE 'scim-%'"))


@pytest.fixture(scope="module", autouse=True)
def seed():
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        for tid, slug in ((TENANT, "scim-t1"), (TENANT_OTHER, "scim-t2")):
            conn.execute(
                text(
                    "INSERT INTO tenants (id, slug, name, status) VALUES "
                    "(:id, :slug, :name, 'active') "
                    "ON CONFLICT (slug) DO UPDATE SET id = EXCLUDED.id, status = 'active'"
                ),
                {"id": tid, "slug": slug, "name": slug},
            )
        # Tokens are seeded hashed, exactly as the API would store one.
        for tid, name, token in (
            (TENANT, "idp-a", TOKEN),
            (TENANT_OTHER, "idp-b", TOKEN_OTHER),
        ):
            conn.execute(
                text(
                    "INSERT INTO scim_tokens (id, tenant_id, name, token_hash) VALUES "
                    "(gen_random_uuid(), :t, :n, :h) "
                    "ON CONFLICT (tenant_id, name) DO UPDATE SET token_hash = EXCLUDED.token_hash, "
                    "revoked_at = NULL"
                ),
                {"t": tid, "n": name, "h": hashlib.sha256(token.encode()).hexdigest()},
            )
    yield
    with admin.begin() as conn:
        _clean(conn)
        conn.execute(
            text("DELETE FROM scim_tokens WHERE tenant_id IN (:a, :b)"),
            {"a": TENANT, "b": TENANT_OTHER},
        )
        conn.execute(text("DELETE FROM tenants WHERE slug LIKE 'scim-t%'"))
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


def _client() -> TestClient:
    import importlib

    main_mod = importlib.import_module("platform_core.main")
    fresh = FastAPI()
    for route in main_mod.app.router.routes:
        fresh.router.routes.append(route)
    return TestClient(fresh, raise_server_exceptions=False)


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _create_user(email: str, *, token: str = TOKEN, **extra: object):
    return _client().post(
        "/scim/v2/Users",
        headers=_auth(token),
        json={
            "schemas": ["urn:ietf:params:scim:schemas:core:2.0:User"],
            "userName": email,
            **extra,
        },
    )


# --- authentication ---------------------------------------------------------


def test_a_request_without_a_token_is_refused() -> None:
    resp = _client().get("/scim/v2/Users")
    assert resp.status_code == 401, resp.text
    assert resp.headers.get("www-authenticate") == "Bearer"


def test_an_unknown_token_is_refused() -> None:
    resp = _client().get("/scim/v2/Users", headers=_auth("not-a-real-token"))
    assert resp.status_code == 401


def test_a_revoked_token_is_refused() -> None:
    """Revocation is a timestamp, so the answer is still "invalid token" and
    not "this token used to exist"."""
    admin = create_engine(ADMIN_URL)
    try:
        with admin.begin() as conn:
            conn.execute(
                text("UPDATE scim_tokens SET revoked_at = 1 WHERE tenant_id = :t"), {"t": TENANT}
            )
        assert _client().get("/scim/v2/Users", headers=_auth(TOKEN)).status_code == 401
    finally:
        with admin.begin() as conn:
            conn.execute(
                text("UPDATE scim_tokens SET revoked_at = NULL WHERE tenant_id = :t"),
                {"t": TENANT},
            )
        admin.dispose()


# --- provisioning -----------------------------------------------------------


def test_creating_a_user_provisions_a_membership() -> None:
    resp = _create_user("scim-ada@example.com", displayName="Ada")

    assert resp.status_code == 200, resp.text
    assert resp.json()["userName"] == "scim-ada@example.com"
    assert resp.json()["active"] is True

    admin = create_engine(ADMIN_URL)
    try:
        with admin.begin() as conn:
            role = conn.execute(
                text(
                    "SELECT m.role FROM memberships m JOIN users u ON u.id = m.user_id "
                    "WHERE m.tenant_id = :t AND u.primary_email = 'scim-ada@example.com'"
                ),
                {"t": TENANT},
            ).scalar_one()
    finally:
        admin.dispose()
    # The least-privileged role, never a role taken from the payload.
    assert role == "support_viewer"


def test_repeating_a_provisioning_request_is_idempotent() -> None:
    first = _create_user("scim-ada@example.com")
    second = _create_user("scim-ada@example.com")

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["id"] == second.json()["id"]


def test_a_filter_narrows_the_list() -> None:
    _create_user("scim-ada@example.com")
    _create_user("scim-grace@example.com")

    resp = _client().get(
        '/scim/v2/Users?filter=userName eq "scim-ada@example.com"', headers=_auth(TOKEN)
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["totalResults"] == 1
    assert body["Resources"][0]["userName"] == "scim-ada@example.com"


def test_an_unsupported_filter_is_a_400_with_the_scim_type() -> None:
    resp = _client().get('/scim/v2/Users?filter=userName co "scim"', headers=_auth(TOKEN))
    assert resp.status_code == 400, resp.text
    assert resp.json()["scimType"] == "invalidFilter"


def test_deactivating_a_user_suspends_rather_than_deletes() -> None:
    """SCIM's DELETE means "deactivate". A row that disappears breaks the audit
    trail that references it and the Cases it worked on."""
    user_id = _create_user("scim-ada@example.com").json()["id"]

    deleted = _client().delete(f"/scim/v2/Users/{user_id}", headers=_auth(TOKEN))
    assert deleted.status_code == 204, deleted.text

    admin = create_engine(ADMIN_URL)
    try:
        with admin.begin() as conn:
            status = conn.execute(
                text(
                    "SELECT m.status FROM memberships m JOIN users u ON u.id = m.user_id "
                    "WHERE m.tenant_id = :t AND u.primary_email = 'scim-ada@example.com'"
                ),
                {"t": TENANT},
            ).scalar_one()
            still_there = conn.execute(
                text("SELECT count(*) FROM users WHERE primary_email = 'scim-ada@example.com'")
            ).scalar_one()
    finally:
        admin.dispose()
    assert status == "suspended"
    assert still_there == 1


def test_a_patch_can_reactivate() -> None:
    user_id = _create_user("scim-ada@example.com").json()["id"]
    _client().delete(f"/scim/v2/Users/{user_id}", headers=_auth(TOKEN))

    resp = _client().patch(
        f"/scim/v2/Users/{user_id}",
        headers=_auth(TOKEN),
        json={
            "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
            "Operations": [{"op": "replace", "path": "active", "value": True}],
        },
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["active"] is True


def test_a_patch_cannot_change_an_immutable_attribute() -> None:
    user_id = _create_user("scim-ada@example.com").json()["id"]
    resp = _client().patch(
        f"/scim/v2/Users/{user_id}",
        headers=_auth(TOKEN),
        json={"Operations": [{"op": "replace", "path": "id", "value": str(uuid.uuid4())}]},
    )
    assert resp.status_code == 400, resp.text
    assert resp.json()["scimType"] == "mutability"


# --- tenant isolation -------------------------------------------------------


def test_another_tenants_token_cannot_read_this_tenants_user() -> None:
    user_id = _create_user("scim-ada@example.com").json()["id"]

    direct = _client().get(f"/scim/v2/Users/{user_id}", headers=_auth(TOKEN_OTHER))
    listed = _client().get("/scim/v2/Users", headers=_auth(TOKEN_OTHER))

    assert direct.status_code == 404, direct.text
    assert listed.json()["totalResults"] == 0


def test_a_group_becomes_a_department_and_never_a_role() -> None:
    """The decision that keeps an IdP administrator from granting themselves
    `tenant_owner`: a Group is org structure, and roles stay a tenant action."""
    resp = _client().post(
        "/scim/v2/Groups",
        headers=_auth(TOKEN),
        json={"displayName": "Support EMEA", "externalId": "support-emea"},
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["externalId"] == "support-emea"

    admin = create_engine(ADMIN_URL)
    try:
        with admin.begin() as conn:
            assert (
                conn.execute(
                    text("SELECT count(*) FROM departments WHERE tenant_id = :t"),
                    {"t": TENANT},
                ).scalar_one()
                == 1
            )
    finally:
        admin.dispose()


def test_repeating_a_group_request_converges() -> None:
    first = _client().post(
        "/scim/v2/Groups",
        headers=_auth(TOKEN),
        json={"displayName": "Support EMEA", "externalId": "support-emea"},
    )
    second = _client().post(
        "/scim/v2/Groups",
        headers=_auth(TOKEN),
        json={"displayName": "Support EMEA", "externalId": "support-emea"},
    )

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["id"] == second.json()["id"]


def test_a_group_patch_assigns_members_to_the_department() -> None:
    user_id = _create_user("scim-ada@example.com").json()["id"]
    group_id = (
        _client()
        .post(
            "/scim/v2/Groups",
            headers=_auth(TOKEN),
            json={"displayName": "Support EMEA", "externalId": "support-emea"},
        )
        .json()["id"]
    )

    resp = _client().patch(
        f"/scim/v2/Groups/{group_id}",
        headers=_auth(TOKEN),
        json={"Operations": [{"op": "replace", "path": "members", "value": [{"value": user_id}]}]},
    )

    assert resp.status_code == 200, resp.text
    admin = create_engine(ADMIN_URL)
    try:
        with admin.begin() as conn:
            department_id = conn.execute(
                text(
                    "SELECT m.department_id FROM memberships m JOIN users u ON u.id = m.user_id "
                    "WHERE m.tenant_id = :t AND u.primary_email = 'scim-ada@example.com'"
                ),
                {"t": TENANT},
            ).scalar_one()
    finally:
        admin.dispose()
    assert str(department_id) == group_id
