"""Integration tests: EnterpriseAccount, Department, and the SLA they decide.

These are integration tests rather than unit tests because every property worth
pinning lives in the database, not in Python:

- a composite FK, so a Case or a membership cannot reference another tenant's
  row (RLS would *hide* the wrong row rather than reject the write, so the
  failure would read as "no account" instead of as an error);
- an acyclicity trigger, because `parent_id <> id` stops one node and the FK
  stops a foreign parent, but A -> B -> A satisfies both;
- a table grant for the application role, whose absence fails only on a fresh
  database and only on the first request.

The API tests carry the tier -> SLA behaviour, because that is where a silent
mistake is cheapest to make: a Case attached to a `strategic` account that gets
the standard window looks like a working system.
"""

import asyncio
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

TENANT = "0190d000-0000-7000-8000-0000000000e1"
TENANT_OTHER = "0190d000-0000-7000-8000-0000000000e2"

# Cleanup order matters: Cases reference accounts, memberships reference
# departments, and both reference the tenant.
_CLEAN: tuple[str, ...] = (
    "DELETE FROM cases WHERE tenant_id IN (:a, :b)",
    "DELETE FROM memberships WHERE tenant_id IN (:a, :b)",
    "DELETE FROM users WHERE primary_email LIKE 'orgstest-%'",
    "DELETE FROM departments WHERE tenant_id IN (:a, :b)",
    "DELETE FROM enterprise_accounts WHERE tenant_id IN (:a, :b)",
    "DELETE FROM audit_events WHERE tenant_id IN (:a, :b)",
    "DELETE FROM outbox_events WHERE tenant_id IN (:a, :b)",
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


def _client(tenant_id: str, role: str = "tenant_owner") -> TestClient:
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
        for tid, slug in ((TENANT, "org-t1"), (TENANT_OTHER, "org-t2")):
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
        conn.execute(text("DELETE FROM tenants WHERE slug LIKE 'org-t%'"))
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


# --- enterprise accounts ----------------------------------------------------


def _create_account(
    *,
    name: str = "Acme",
    tier: str = "standard",
    contract_status: str = "active",
    tenant: str = TENANT,
    role: str = "tenant_owner",
    **extra: object,
) -> dict:
    body = {"name": name, "tier": tier, "contract_status": contract_status, **extra}
    resp = _client(tenant, role).post("/v1/identity/accounts", headers=_headers(), json=body)
    return {"status": resp.status_code, "body": resp.json() if resp.content else {}}


def test_an_account_can_be_created_and_read_back() -> None:
    created = _create_account(tier="strategic", external_crm_ref="crm-42")
    assert created["status"] == 200, created
    account = created["body"]["account"]
    assert account["tier"] == "strategic"
    assert account["contract_status"] == "active"
    assert account["external_crm_ref"] == "crm-42"
    # The database owns created_at; the ORM would otherwise send an explicit
    # NULL over the column default and SQLAlchemy would report it as an
    # integrity error on a field nobody asked for.
    assert account["created_at"] > 0

    listed = _client(TENANT).get("/v1/identity/accounts", headers=_headers())
    assert listed.status_code == 200
    assert [a["account_id"] for a in listed.json()["accounts"]] == [account["account_id"]]


def test_a_support_agent_can_read_accounts_but_not_edit_them() -> None:
    """Reading is deliberate: an agent opening a Case has to pick the account
    it is about. Writing is not: the tier decides an SLA window, which is a
    commercial decision."""
    assert _create_account()["status"] == 200

    read = _client(TENANT, "support_agent").get("/v1/identity/accounts", headers=_headers())
    assert read.status_code == 200

    write = _create_account(name="Second", role="support_agent")
    assert write["status"] == 403, write


def test_another_tenant_cannot_see_the_account() -> None:
    account_id = _create_account()["body"]["account"]["account_id"]

    listed = _client(TENANT_OTHER).get("/v1/identity/accounts", headers=_headers())
    assert listed.json()["accounts"] == []

    direct = _client(TENANT_OTHER).get(f"/v1/identity/accounts/{account_id}", headers=_headers())
    assert direct.status_code == 404


def test_a_cross_tenant_parent_is_refused() -> None:
    """Rendered as 409 not 404: the caller owns the request, the parent simply
    is not one they can attach to. Either way the response must not confirm
    that the other tenant's row exists."""
    foreign = _create_account(tenant=TENANT_OTHER)["body"]["account"]["account_id"]

    result = _create_account(name="Child", parent_id=foreign)
    assert result["status"] == 409, result
    assert result["body"]["error"]["code"] == "ORG_PARENT_NOT_FOUND"


def test_a_two_node_cycle_is_refused() -> None:
    """A -> B -> A. `parent_id <> id` catches the one-node case and the FK
    catches a foreign parent, so a cycle of length two is the first case that
    actually exercises the ancestor walk - and it is the one a
    single-lookup guard would let through."""
    a = _create_account(name="A")["body"]["account"]["account_id"]
    b = _create_account(name="B", parent_id=a)["body"]["account"]["account_id"]

    resp = _client(TENANT).patch(
        f"/v1/identity/accounts/{a}",
        headers=_headers(),
        json={"parent_id": b},
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "ORG_CYCLE"


def test_a_deeper_cycle_is_refused() -> None:
    a = _create_account(name="A")["body"]["account"]["account_id"]
    b = _create_account(name="B", parent_id=a)["body"]["account"]["account_id"]
    c = _create_account(name="C", parent_id=b)["body"]["account"]["account_id"]

    resp = _client(TENANT).patch(
        f"/v1/identity/accounts/{a}", headers=_headers(), json={"parent_id": c}
    )
    assert resp.status_code == 409, resp.text


def test_omitting_parent_leaves_it_alone_and_null_detaches() -> None:
    """The distinction the PATCH body has to make: a rename must not silently
    detach a child from its parent."""
    parent = _create_account(name="Parent")["body"]["account"]["account_id"]
    child = _create_account(name="Child", parent_id=parent)["body"]["account"]["account_id"]

    renamed = _client(TENANT).patch(
        f"/v1/identity/accounts/{child}", headers=_headers(), json={"name": "Renamed"}
    )
    assert renamed.status_code == 200
    assert renamed.json()["account"]["parent_id"] == parent

    detached = _client(TENANT).patch(
        f"/v1/identity/accounts/{child}", headers=_headers(), json={"parent_id": None}
    )
    assert detached.status_code == 200
    assert detached.json()["account"]["parent_id"] is None


def test_an_invalid_tier_is_rejected_before_the_database_sees_it() -> None:
    result = _create_account(tier="platinum")
    assert result["status"] == 400, result
    assert result["body"]["error"]["code"] == "TIER_INVALID"


# --- departments ------------------------------------------------------------


def test_a_department_slug_is_case_normalised_and_unique() -> None:
    created = _client(TENANT).post(
        "/v1/identity/departments",
        headers=_headers(),
        json={"name": "Support", "slug": "Support-EMEA"},
    )
    assert created.status_code == 200, created.text
    assert created.json()["department"]["slug"] == "support-emea"

    # Same department, different casing: a 409 rather than a second row, which
    # is what `slug = lower(slug)` plus UNIQUE is for.
    duplicate = _client(TENANT).post(
        "/v1/identity/departments",
        headers=_headers(),
        json={"name": "Support again", "slug": "SUPPORT-EMEA"},
    )
    assert duplicate.status_code == 409, duplicate.text
    assert duplicate.json()["error"]["code"] == "SLUG_TAKEN"


def test_a_member_can_be_filed_under_a_department_of_their_own_tenant() -> None:
    dept = (
        _client(TENANT)
        .post(
            "/v1/identity/departments",
            headers=_headers(),
            json={"name": "Support", "slug": "support"},
        )
        .json()["department"]["department_id"]
    )

    membership_id = _seed_membership(TENANT, "orgstest-member@example.com")
    resp = _client(TENANT).post(
        f"/v1/identity/members/{membership_id}",
        headers=_headers(),
        json={"role": "support_agent", "department_id": dept},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["department_id"] == dept


def test_a_member_cannot_be_filed_under_another_tenants_department() -> None:
    foreign_dept = (
        _client(TENANT_OTHER)
        .post(
            "/v1/identity/departments",
            headers=_headers(),
            json={"name": "Foreign", "slug": "foreign"},
        )
        .json()["department"]["department_id"]
    )

    membership_id = _seed_membership(TENANT, "orgstest-member@example.com")
    resp = _client(TENANT).post(
        f"/v1/identity/members/{membership_id}",
        headers=_headers(),
        json={"role": "support_agent", "department_id": foreign_dept},
    )
    assert resp.status_code == 404, resp.text
    assert resp.json()["error"]["code"] == "DEPARTMENT_NOT_FOUND"


def test_a_role_change_without_a_department_leaves_the_department_alone() -> None:
    dept = (
        _client(TENANT)
        .post(
            "/v1/identity/departments",
            headers=_headers(),
            json={"name": "Support", "slug": "support"},
        )
        .json()["department"]["department_id"]
    )
    membership_id = _seed_membership(TENANT, "orgstest-member@example.com")

    _client(TENANT).post(
        f"/v1/identity/members/{membership_id}",
        headers=_headers(),
        json={"role": "support_agent", "department_id": dept},
    )
    again = _client(TENANT).post(
        f"/v1/identity/members/{membership_id}",
        headers=_headers(),
        json={"role": "support_admin"},
    )
    assert again.status_code == 200, again.text
    assert again.json()["department_id"] == dept


# --- the tier actually decides the SLA window -------------------------------


def _create_case(*, account_id: str | None = None, priority: str = "p2") -> dict:
    body: dict = {"subject": "Refund not received", "priority": priority}
    if account_id is not None:
        body["enterprise_account_id"] = account_id
    resp = _client(TENANT, "support_agent").post("/v1/cases", headers=_headers(), json=body)
    return {"status": resp.status_code, "body": resp.json() if resp.content else {}}


def _window(case: dict) -> int:
    return case["first_response_due_at"] - case["opened_at"]


def test_a_case_without_an_account_keeps_the_default_window() -> None:
    case = _create_case()
    assert case["status"] == 200, case
    assert case["body"]["case"]["sla_tier"] is None
    assert _window(case["body"]["case"]) == 60 * 60


def test_a_case_inherits_the_account_tier_window() -> None:
    """The reason EnterpriseAccount exists: a contract fact with an effect."""
    account = _create_account(tier="strategic")["body"]["account"]["account_id"]

    case = _create_case(account_id=account)
    assert case["status"] == 200, case
    payload = case["body"]["case"]
    assert payload["sla_tier"] == "strategic"
    assert payload["enterprise_account_id"] == account
    # DEFAULT_SLA.first_response_minutes (60) * 0.25, p2 multiplier 1.0.
    assert _window(payload) == 15 * 60


def test_a_churned_account_does_not_get_the_tier_window() -> None:
    account = _create_account(tier="strategic", contract_status="churned")["body"]["account"][
        "account_id"
    ]

    case = _create_case(account_id=account)
    assert case["status"] == 200, case
    assert _window(case["body"]["case"]) == 60 * 60


def test_a_case_cannot_reference_another_tenants_account() -> None:
    foreign = _create_account(tenant=TENANT_OTHER)["body"]["account"]["account_id"]

    case = _create_case(account_id=foreign)
    assert case["status"] == 404, case
    assert case["body"]["error"]["code"] == "ACCOUNT_NOT_FOUND"


def test_the_tier_is_snapshotted_so_a_contract_change_does_not_move_a_running_clock() -> None:
    """Otherwise "why was the first-response target 15 minutes" would depend on
    when the question is asked, and a customer could shorten a window that
    started before they upgraded."""
    account = _create_account(tier="strategic")["body"]["account"]["account_id"]
    case = _create_case(account_id=account)["body"]["case"]
    original = case["first_response_due_at"]

    patched = _client(TENANT).patch(
        f"/v1/identity/accounts/{account}",
        headers=_headers(),
        json={"contract_status": "churned"},
    )
    assert patched.status_code == 200, patched.text

    after = (
        _client(TENANT, "support_agent")
        .get(f"/v1/cases/{case['case_id']}", headers=_headers())
        .json()["case"]
    )
    assert after["first_response_due_at"] == original
    assert after["sla_tier"] == "strategic"


def test_a_priority_change_recomputes_from_the_snapshotted_tier() -> None:
    """The deadline is recomputed here, so this is the path where re-reading
    the account would quietly apply a tier the Case never opened under."""
    account = _create_account(tier="strategic")["body"]["account"]["account_id"]
    case = _create_case(account_id=account)["body"]["case"]

    # The account moves to `basic` after the Case opened.
    _client(TENANT).patch(
        f"/v1/identity/accounts/{account}",
        headers=_headers(),
        json={"tier": "basic"},
    )

    resp = _client(TENANT, "support_agent").post(
        f"/v1/cases/{case['case_id']}/commands",
        headers=_headers(),
        json={"command": "change_priority", "parameters": {"priority": "p1"}},
    )
    assert resp.status_code == 200, resp.text
    updated = resp.json()["case"]
    # The tier scales *both* targets: strategic resolution is
    # 480 min * 0.25 = 120 min, and p1 halves it to 3600s. Using the account's
    # current tier instead would give basic (480 * 2.0 = 960 min), i.e. 28800s,
    # so the two answers are unmistakable.
    assert updated["resolution_due_at"] - updated["opened_at"] == 3600


def _seed_membership(tenant: str, email: str) -> str:
    """Insert a User + Membership directly.

    Inviting and accepting would exercise the invitation flow, which is already
    covered elsewhere; this test is about where a member may be filed.
    """
    admin = create_engine(ADMIN_URL)
    try:
        with admin.begin() as conn:
            user_id = conn.execute(
                text(
                    "INSERT INTO users (id, primary_email, display_name, is_service_account) "
                    "VALUES (gen_random_uuid(), :email, 'Org Member', false) "
                    "ON CONFLICT (primary_email) DO UPDATE "
                    "SET display_name = EXCLUDED.display_name "
                    "RETURNING id"
                ),
                {"email": email},
            ).scalar_one()
            membership_id = conn.execute(
                text(
                    "INSERT INTO memberships (id, tenant_id, user_id, role, status) "
                    "VALUES (gen_random_uuid(), :t, :u, 'support_agent', 'active') "
                    "RETURNING id"
                ),
                {"t": tenant, "u": user_id},
            ).scalar_one()
    finally:
        admin.dispose()
    return str(membership_id)
