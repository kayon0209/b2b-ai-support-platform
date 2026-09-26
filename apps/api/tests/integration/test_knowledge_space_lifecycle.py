"""Knowledge space creation: the path that does not exist yet.

Why this suite exists
---------------------
`POST /v1/knowledge/documents` has been implemented for a long time, and so has
`GET /v1/knowledge/spaces`. What is missing is the one write that makes either
of them reachable from an empty tenant: creating the space a document belongs
to. `POST /v1/knowledge/spaces` answers 405, and no other code path in the
repository constructs a `KnowledgeSpace` outside the model definition.

The consequence is not a missing convenience. On a freshly provisioned tenant
`GET /v1/knowledge/spaces` returns `{"items": [], "total": 0}`, every upload
fails with "no such knowledge space for this tenant", and the knowledge base -
the thing the product's grounded-answer promise rests on - cannot be populated
at all without a seed script or direct database surgery. Measured on a live
stack: 150 of 167 customer questions were answered by abstaining, because
there was nowhere to put the evidence.

So the property under test is the full bootstrap chain, not the single route:
a tenant with no spaces can create one, see it listed, and upload into it.
"""

import os
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

pytestmark = pytest.mark.integration

ADMIN_URL = os.environ.get(
    "APP_ADMIN_DATABASE_URL",
    "postgresql+psycopg://platform:platform@localhost:5435/platform",
)

TENANT = "01900000-0000-7000-8000-000000000b01"
OWNER_USER = "01900000-0000-7000-8000-000000000b21"
AGENT_USER = "01900000-0000-7000-8000-000000000b22"
SLUG = "kn-space-create"

OWNER_TOKEN = f"pt_{SLUG}_{OWNER_USER}"
AGENT_TOKEN = f"pt_{SLUG}_{AGENT_USER}"


@pytest.fixture(scope="module", autouse=True)
def seed() -> None:
    admin = create_engine(ADMIN_URL)
    _cleanup(admin)
    with admin.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO tenants (id, slug, name, status) "
                "VALUES (:id, :slug, 'Knowledge Space Create', 'active') "
                "ON CONFLICT (slug) DO NOTHING"
            ),
            {"id": TENANT, "slug": SLUG},
        )
        for uid, email in (
            (OWNER_USER, "kn-space-owner@example.com"),
            (AGENT_USER, "kn-space-agent@example.com"),
        ):
            conn.execute(
                text(
                    "INSERT INTO users (id, primary_email, display_name, is_service_account) "
                    "VALUES (:id, :email, 'KS', false) ON CONFLICT (primary_email) DO NOTHING"
                ),
                {"id": uid, "email": email},
            )
        # Distinct roles: the owner may create a space, the agent may not.
        # `support_agent` is deliberately the one role that has neither
        # KNOWLEDGE_UPLOAD nor KNOWLEDGE_PUBLISH, so it is the cheapest proof
        # that the new route is gated rather than merely reachable.
        for uid, role in (
            (OWNER_USER, "tenant_owner"),
            (AGENT_USER, "support_agent"),
        ):
            conn.execute(
                text(
                    "INSERT INTO memberships (id, tenant_id, user_id, role, status) "
                    "VALUES (gen_random_uuid(), :t, :u, :r, 'active') "
                    "ON CONFLICT (tenant_id, user_id) DO NOTHING"
                ),
                {"t": TENANT, "u": uid, "r": role},
            )
    yield
    _cleanup(admin)
    admin.dispose()


def _cleanup(admin: object) -> None:
    with admin.begin() as conn:  # type: ignore[attr-defined]
        for table in ("chunks", "knowledge_acls", "document_versions", "documents"):
            conn.execute(
                text(
                    f"DELETE FROM {table} WHERE tenant_id = "  # noqa: S608 - fixed table names
                    "(SELECT id FROM tenants WHERE slug = :s)"
                ),
                {"s": SLUG},
            )
        conn.execute(
            text(
                "DELETE FROM knowledge_spaces WHERE tenant_id = "
                "(SELECT id FROM tenants WHERE slug = :s)"
            ),
            {"s": SLUG},
        )
        conn.execute(
            text(
                "DELETE FROM memberships WHERE tenant_id = (SELECT id FROM tenants WHERE slug = :s)"
            ),
            {"s": SLUG},
        )
        conn.execute(
            text(
                "DELETE FROM users WHERE primary_email IN "
                "('kn-space-owner@example.com','kn-space-agent@example.com')"
            )
        )
        conn.execute(text("DELETE FROM tenants WHERE slug = :s"), {"s": SLUG})


def _headers(token: str = OWNER_TOKEN) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "Idempotency-Key": str(uuid.uuid4())}


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("APP_ALLOW_BOOTSTRAP_TOKENS", "true")
    monkeypatch.delenv("APP_OIDC_ISSUER", raising=False)

    from platform_core.config import get_settings
    from platform_core.identity.middleware import build_resolver
    from platform_core.main import app as main_app

    get_settings.cache_clear()

    from fastapi import FastAPI

    from platform_core.identity.middleware import TenantContextMiddleware

    fresh = FastAPI()
    for route in main_app.router.routes:
        fresh.router.routes.append(route)
    fresh.add_middleware(TenantContextMiddleware, resolver=build_resolver())
    try:
        yield TestClient(fresh, raise_server_exceptions=False)
    finally:
        get_settings.cache_clear()


@pytest.fixture(autouse=True)
def stub_storage(monkeypatch: pytest.MonkeyPatch) -> None:
    from platform_core.knowledge import service

    monkeypatch.setattr(service, "upload_object", lambda key, data, content_type: key)
    monkeypatch.setattr(
        service,
        "presign_for",
        lambda key, *, expires_seconds, settings=None: f"http://minio.local/{key}",
    )


# --- 1. The route exists and is usable ------------------------------------


def test_empty_tenant_has_no_spaces(client: TestClient) -> None:
    """The precondition, asserted so the rest of the suite cannot pass by
    inheriting a space somebody else left behind."""
    resp = client.get("/v1/knowledge/spaces", headers=_headers())
    assert resp.status_code == 200, resp.text
    assert resp.json()["items"] == [], resp.text


def test_create_space_returns_an_id_and_name(client: TestClient) -> None:
    resp = client.post(
        "/v1/knowledge/spaces",
        headers=_headers(),
        json={"name": "Product FAQ"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert uuid.UUID(body["id"])
    assert body["name"] == "Product FAQ"


def test_created_space_is_listed(client: TestClient) -> None:
    created = client.post(
        "/v1/knowledge/spaces", headers=_headers(), json={"name": "Listed Space"}
    ).json()
    listing = client.get("/v1/knowledge/spaces", headers=_headers()).json()
    assert created["id"] in [item["id"] for item in listing["items"]]


def test_document_upload_lands_in_a_created_space(client: TestClient) -> None:
    """The whole reason this route exists: the bootstrap chain end to end."""
    space_id = client.post(
        "/v1/knowledge/spaces", headers=_headers(), json={"name": "Upload Target"}
    ).json()["id"]

    resp = client.post(
        "/v1/knowledge/documents",
        headers=_headers(),
        data={
            "space_id": space_id,
            "title": "Refund policy",
            "canonical_uri": f"doc://{uuid.uuid4()}",
            "classification": "internal",
            "version_label": "v1",
        },
        files={"file": ("policy.md", b"# Refunds\n\nRefunds take 5 days.", "text/markdown")},
    )
    assert resp.status_code == 200, resp.text
    assert uuid.UUID(resp.json()["version_id"])


# --- 2. It is gated, not merely reachable ----------------------------------


def test_agent_without_upload_rights_is_refused(client: TestClient) -> None:
    resp = client.post("/v1/knowledge/spaces", headers=_headers(AGENT_TOKEN), json={"name": "Nope"})
    assert resp.status_code == 403, resp.text


def test_anonymous_caller_is_refused(client: TestClient) -> None:
    resp = client.post(
        "/v1/knowledge/spaces",
        headers={"Idempotency-Key": str(uuid.uuid4())},
        json={"name": "Nope"},
    )
    assert resp.status_code == 401, resp.text


def test_write_requires_an_idempotency_key(client: TestClient) -> None:
    """Every write command in this platform carries one; a new route that
    skipped the rule would be the only way to create two spaces from one
    double-click."""
    resp = client.post(
        "/v1/knowledge/spaces",
        headers={"Authorization": f"Bearer {OWNER_TOKEN}"},
        json={"name": "No Key"},
    )
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"


def test_blank_name_is_refused(client: TestClient) -> None:
    resp = client.post("/v1/knowledge/spaces", headers=_headers(), json={"name": ""})
    assert resp.status_code == 422, resp.text
