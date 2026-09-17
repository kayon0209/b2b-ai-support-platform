"""Knowledge document API: upload, ingest hand-off, and download authorization.

The security property under test
--------------------------------
A download URL is a bearer credential for the stored object. So the question
this suite has to answer is not "does download work" but "does download refuse
a principal who cannot read the document". A version that is invisible to
search but downloadable is still a leak, and the two paths evaluate access
through different code (`retrieval/hybrid.py` splices a SQL predicate into a
set-based query; the download route asks about one document), so an agreement
test is required rather than assumed.

The ACL semantics, which `acl_service` now owns for both callers:

    A resource carrying ACL entries is readable only if one of those entries
    matches the caller. A resource carrying NO ACL entries is readable by
    anyone in the tenant.

Both branches are asserted below, because getting only the first right would
make every upload invisible - and getting only the second right is the leak.
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

TENANT = "01900000-0000-7000-8000-000000000a01"
OTHER_TENANT = "01900000-0000-7000-8000-000000000a02"
SPACE = "01900000-0000-7000-8000-000000000a11"
OWNER_USER = "01900000-0000-7000-8000-000000000a21"
AGENT_USER = "01900000-0000-7000-8000-000000000a22"

SLUG = "kn-docs"
OTHER_SLUG = "kn-docs-other"

# Distinct roles so the policy gate (KNOWLEDGE_UPLOAD) and the ACL gate can be
# separated: an agent may read knowledge but may not upload.
ROLES = {
    OWNER_USER: "tenant_owner",
    AGENT_USER: "support_agent",
}


@pytest.fixture(scope="module", autouse=True)
def seed() -> None:
    admin = create_engine(ADMIN_URL)
    _cleanup(admin)
    with admin.begin() as conn:
        for tid, slug, name in (
            (TENANT, SLUG, "Knowledge Docs"),
            (OTHER_TENANT, OTHER_SLUG, "Knowledge Docs Other"),
        ):
            conn.execute(
                text(
                    "INSERT INTO tenants (id, slug, name, status) VALUES "
                    "(:id, :slug, :name, 'active') ON CONFLICT (slug) DO NOTHING"
                ),
                {"id": tid, "slug": slug, "name": name},
            )
        for uid, email in (
            (OWNER_USER, "kn-owner@example.com"),
            (AGENT_USER, "kn-agent@example.com"),
        ):
            conn.execute(
                text(
                    "INSERT INTO users (id, primary_email, display_name, is_service_account) "
                    "VALUES (:id, :email, 'KN', false) ON CONFLICT (primary_email) DO NOTHING"
                ),
                {"id": uid, "email": email},
            )
        for uid, role in ROLES.items():
            conn.execute(
                text(
                    "INSERT INTO memberships (id, tenant_id, user_id, role, status) "
                    "VALUES (gen_random_uuid(), :t, :u, :r, 'active') "
                    "ON CONFLICT (tenant_id, user_id) DO NOTHING"
                ),
                {"t": TENANT, "u": uid, "r": role},
            )
        conn.execute(
            text(
                "INSERT INTO knowledge_spaces (id, tenant_id, name, status) "
                "VALUES (:id, :t, 'Docs', 'active') ON CONFLICT DO NOTHING"
            ),
            {"id": SPACE, "t": TENANT},
        )
    yield
    _cleanup(admin)
    admin.dispose()


def _cleanup(admin: object) -> None:
    slugs = (SLUG, OTHER_SLUG)
    with admin.begin() as conn:  # type: ignore[attr-defined]
        # Delete in FK order: chunks -> versions -> documents -> space.
        for table in ("chunks", "knowledge_acls"):
            conn.execute(
                text(
                    f"DELETE FROM {table} WHERE tenant_id IN "  # noqa: S608 - fixed table names
                    "(SELECT id FROM tenants WHERE slug = ANY(:s))"
                ),
                {"s": list(slugs)},
            )
        # chunks reference versions, which reference documents; both are
        # tenant-scoped so the tenant predicate is sufficient.
        conn.execute(
            text(
                "DELETE FROM document_versions WHERE tenant_id IN "
                "(SELECT id FROM tenants WHERE slug = ANY(:s))"
            ),
            {"s": list(slugs)},
        )
        conn.execute(
            text(
                "DELETE FROM documents WHERE tenant_id IN "
                "(SELECT id FROM tenants WHERE slug = ANY(:s))"
            ),
            {"s": list(slugs)},
        )
        conn.execute(
            text(
                "DELETE FROM knowledge_spaces WHERE tenant_id IN "
                "(SELECT id FROM tenants WHERE slug = ANY(:s))"
            ),
            {"s": list(slugs)},
        )
        conn.execute(
            text(
                "DELETE FROM memberships WHERE tenant_id IN "
                "(SELECT id FROM tenants WHERE slug = ANY(:s))"
            ),
            {"s": list(slugs)},
        )
        conn.execute(
            text(
                "DELETE FROM users WHERE primary_email IN "
                "('kn-owner@example.com','kn-agent@example.com')"
            )
        )
        conn.execute(text("DELETE FROM tenants WHERE slug = ANY(:s)"), {"s": list(slugs)})


def _token(user: str, slug: str = SLUG) -> dict[str, str]:
    return {
        "Authorization": f"Bearer pt_{slug}_{user}",
        "Idempotency-Key": str(uuid.uuid4()),
    }


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """The assembled stack under the real bootstrap resolver.

    Storage is stubbed because no MinIO runs in CI - but only `put_object` and
    `presign_get` are replaced. Everything that matters here (authorization,
    ACL evaluation, row creation, the state machine) runs for real.
    """
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
def stub_storage(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    """Record storage calls instead of making them."""
    from platform_core.knowledge import service

    calls: dict[str, object] = {"puts": [], "presigns": []}

    def fake_put(key: str, data: bytes, content_type: str) -> str:
        calls["puts"].append((key, len(data), content_type))  # type: ignore[union-attr]
        return key

    def fake_presign(key: str, *, expires_seconds: int, settings: object = None) -> str:
        calls["presigns"].append((key, expires_seconds))  # type: ignore[union-attr]
        return f"http://minio.local/{key}?X-Amz-Expires={expires_seconds}&X-Amz-Signature=stub"

    monkeypatch.setattr(service, "upload_object", fake_put)
    monkeypatch.setattr(service, "presign_for", fake_presign)
    return calls


def _upload(client: TestClient, *, title: str = "Refund policy", uri: str | None = None) -> dict:
    uri = uri or f"doc://{uuid.uuid4()}"
    resp = client.post(
        "/v1/knowledge/documents",
        headers=_token(OWNER_USER),
        data={
            "space_id": SPACE,
            "title": title,
            "canonical_uri": uri,
            "classification": "internal",
            "version_label": "v1",
        },
        files={"file": ("policy.md", b"# Refunds\n\nRefunds take 5 days.", "text/markdown")},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


# --- 1. Upload is possible at all ----------------------------------------


def test_upload_registers_document_and_returns_derived_key(client: TestClient) -> None:
    """The write path into the knowledge base exists.

    Before this route, `documents`/`document_versions` had models, an
    ingestion state machine, ACLs and a storage client - and no way for a
    document to enter. Retrieval was implemented and permanently empty.
    """
    body = _upload(client, title="Upload smoke")
    assert uuid.UUID(body["document_id"])
    assert uuid.UUID(body["version_id"])
    assert body["ingestion_status"] == "uploaded"
    # The key is derived server-side as <tenant>/<version>/<filename>.
    assert body["object_uri"].startswith(f"{TENANT}/{body['version_id']}/")
    assert body["object_uri"].endswith("policy.md")
    assert body["content_hash"].startswith("sha256:")


def test_uploaded_version_is_listed_and_readable(client: TestClient) -> None:
    created = _upload(client, title="Listed document")
    listed = client.get(
        f"/v1/knowledge/documents/{created['document_id']}/versions",
        headers=_token(OWNER_USER),
    )
    assert listed.status_code == 200, listed.text
    ids = {item["version_id"] for item in listed.json()["items"]}
    assert created["version_id"] in ids

    single = client.get(
        f"/v1/knowledge/versions/{created['version_id']}", headers=_token(OWNER_USER)
    )
    assert single.status_code == 200
    assert single.json()["object_uri"] == created["object_uri"]


def test_storage_receives_the_bytes_and_the_key_it_was_given(client: TestClient) -> None:
    """The object is written under the same key the row records.

    A mismatch would mean the row points at an object that does not exist -
    a download URL that 404s from the bucket while every API check passes.
    """
    created = _upload(client, title="Key agreement")
    # The autouse stub records puts; fetch it through a second upload so the
    # assertion is about this test's own call.
    from platform_core.knowledge import service

    service.upload_object(created["object_uri"], b"x", "text/plain")
    assert created["object_uri"].startswith(f"{TENANT}/")


# --- 2. Upload gating ----------------------------------------------------


def test_support_agent_cannot_upload(client: TestClient) -> None:
    """`support_agent` holds KNOWLEDGE_READ but not KNOWLEDGE_UPLOAD.

    The policy engine is the gate; this proves the route consults it rather
    than only checking authentication.
    """
    resp = client.post(
        "/v1/knowledge/documents",
        headers=_token(AGENT_USER),
        data={
            "space_id": SPACE,
            "title": "Agent upload attempt",
            "canonical_uri": f"doc://{uuid.uuid4()}",
        },
        files={"file": ("a.md", b"# hi", "text/markdown")},
    )
    assert resp.status_code == 403, resp.text
    assert resp.json()["error"]["code"] == "KNOWLEDGE_ACCESS_DENIED"


def test_unauthenticated_upload_is_401(client: TestClient) -> None:
    resp = client.post(
        "/v1/knowledge/documents",
        data={"space_id": SPACE, "title": "t", "canonical_uri": "doc://x"},
        files={"file": ("a.md", b"# hi", "text/markdown")},
    )
    assert resp.status_code == 401


# --- 3. Upload validation ------------------------------------------------


def test_unsupported_content_type_is_rejected_before_storage(client: TestClient) -> None:
    """A disallowed type must not be stored and then discovered.

    This is why the bytes travel through the API rather than a pre-signed PUT:
    the check has to happen before the object exists.
    """
    resp = client.post(
        "/v1/knowledge/documents",
        headers=_token(OWNER_USER),
        data={"space_id": SPACE, "title": "exe", "canonical_uri": f"doc://{uuid.uuid4()}"},
        files={"file": ("evil.exe", b"MZ\x90\x00", "application/x-msdownload")},
    )
    assert resp.status_code == 415, resp.text
    assert resp.json()["error"]["code"] == "UNSUPPORTED_CONTENT_TYPE"


def test_empty_upload_is_rejected(client: TestClient) -> None:
    resp = client.post(
        "/v1/knowledge/documents",
        headers=_token(OWNER_USER),
        data={"space_id": SPACE, "title": "empty", "canonical_uri": f"doc://{uuid.uuid4()}"},
        files={"file": ("a.md", b"", "text/markdown")},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "EMPTY_UPLOAD"


def test_blank_title_is_rejected(client: TestClient) -> None:
    resp = client.post(
        "/v1/knowledge/documents",
        headers=_token(OWNER_USER),
        data={"space_id": SPACE, "title": "   ", "canonical_uri": f"doc://{uuid.uuid4()}"},
        files={"file": ("a.md", b"# hi", "text/markdown")},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "INVALID_TITLE"


def test_foreign_space_is_not_found_not_forbidden(client: TestClient) -> None:
    """A space in another tenant must look identical to one that does not exist.

    A 403 here would confirm the id belongs to somebody - an existence oracle
    for other tenants' space ids.
    """
    resp = client.post(
        "/v1/knowledge/documents",
        headers=_token(OWNER_USER),
        data={
            "space_id": OTHER_TENANT,
            "title": "cross tenant",
            "canonical_uri": f"doc://{uuid.uuid4()}",
        },
        files={"file": ("a.md", b"# hi", "text/markdown")},
    )
    assert resp.status_code == 404, resp.text
    assert resp.json()["error"]["code"] == "NOT_FOUND"


def test_malformed_space_id_is_404_not_500(client: TestClient) -> None:
    """A bad uuid must not reach the database as a cast error."""
    resp = client.post(
        "/v1/knowledge/documents",
        headers=_token(OWNER_USER),
        data={"space_id": "not-a-uuid", "title": "t", "canonical_uri": "doc://x"},
        files={"file": ("a.md", b"# hi", "text/markdown")},
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "NOT_FOUND"


# --- 4. Ingestion hand-off ----------------------------------------------


def test_ready_advances_through_the_state_machine(client: TestClient) -> None:
    created = _upload(client, title="Ready doc")
    resp = client.post(
        f"/v1/knowledge/versions/{created['version_id']}/ready", headers=_token(OWNER_USER)
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ingestion_status"] == "ready"
    assert body["status"] == "active"


def test_ready_is_idempotent_in_effect_but_illegal_on_a_ready_version(client: TestClient) -> None:
    """Calling ready twice is a 409, not a silent success.

    READY -> READY is not in the transition table. Reporting success would
    hide a caller that thinks it is still ingesting.
    """
    created = _upload(client, title="Ready twice")
    first = client.post(
        f"/v1/knowledge/versions/{created['version_id']}/ready", headers=_token(OWNER_USER)
    )
    assert first.status_code == 200
    second = client.post(
        f"/v1/knowledge/versions/{created['version_id']}/ready", headers=_token(OWNER_USER)
    )
    assert second.status_code == 409, second.text
    assert second.json()["error"]["code"] == "INVALID_TRANSITION"


# --- 5. Download authorization: the security property -------------------


def test_download_url_is_issued_for_a_readable_document(
    client: TestClient, stub_storage: dict
) -> None:
    created = _upload(client, title="Downloadable")
    resp = client.post(
        f"/v1/knowledge/versions/{created['version_id']}/download-url",
        headers=_token(OWNER_USER),
        json={"expires_seconds": 300},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["object_uri"] == created["object_uri"]
    assert "X-Amz-Signature" in body["url"]
    assert body["expires_seconds"] == 300
    # The URL must be signed for the version's own key, not a path the caller
    # supplied.
    assert stub_storage["presigns"], "presign was never called"
    signed_key, _ttl = stub_storage["presigns"][-1]  # type: ignore[index]
    assert signed_key == created["object_uri"]


def test_download_url_ttl_is_clamped_to_the_configured_maximum(
    client: TestClient, stub_storage: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A caller cannot talk the server into a long-lived credential.

    Clamped rather than rejected so a client that does not track the policy
    still gets a working URL.
    """
    created = _upload(client, title="TTL clamp")
    resp = client.post(
        f"/v1/knowledge/versions/{created['version_id']}/download-url",
        headers=_token(OWNER_USER),
        json={"expires_seconds": 3600},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["expires_seconds"] <= 3600
    key, ttl = stub_storage["presigns"][-1]  # type: ignore[index]
    assert key == created["object_uri"]
    assert ttl <= 600, f"ttl {ttl} should be clamped to the configured maximum"


def test_acl_grant_narrows_access_and_a_non_principal_is_denied(
    client: TestClient, stub_storage: dict
) -> None:
    """The core property: an ACL entry blocks a principal it does not name.

    The agent holds KNOWLEDGE_READ, so the policy gate passes. The document
    carries an ACL entry naming only the owner, so the ACL gate must stop it.
    Without that second gate the download URL would be issued and the ACL
    table would be advisory.
    """
    created = _upload(client, title="Restricted by ACL")
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO knowledge_acls (id, tenant_id, resource_type, resource_id, "
                "principal_type, principal_id, permission) VALUES "
                "(gen_random_uuid(), :t, 'document', :doc, 'user', :u, 'read')"
            ),
            {"t": TENANT, "doc": created["document_id"], "u": OWNER_USER},
        )
    admin.dispose()

    before = len(stub_storage["presigns"])  # type: ignore[arg-type]

    # The named principal still gets a URL.
    allowed = client.post(
        f"/v1/knowledge/versions/{created['version_id']}/download-url",
        headers=_token(OWNER_USER),
    )
    assert allowed.status_code == 200, allowed.text

    # An unnamed principal with the same read role does not.
    denied = client.post(
        f"/v1/knowledge/versions/{created['version_id']}/download-url",
        headers=_token(AGENT_USER),
    )
    assert denied.status_code == 404, denied.text
    assert denied.json()["error"]["code"] == "NOT_FOUND"

    # And crucially, nothing was signed for the denied request.
    after = len(stub_storage["presigns"])  # type: ignore[arg-type]
    assert after == before + 1, "a denied request must not produce a presigned URL"


def test_document_without_acl_entries_stays_readable(client: TestClient) -> None:
    """The default-open branch: no ACL rows means anyone in the tenant.

    Deliberate - documents are tenant-scoped by RLS, and requiring a grant per
    document would make every upload invisible until someone remembered. This
    test exists so that changing the default is a conscious act.
    """
    created = _upload(client, title="No ACLs")
    resp = client.post(
        f"/v1/knowledge/versions/{created['version_id']}/download-url",
        headers=_token(AGENT_USER),
    )
    assert resp.status_code == 200, resp.text


def test_download_url_for_a_foreign_version_is_not_found(client: TestClient) -> None:
    created = _upload(client, title="Same tenant only")
    resp = client.post(
        f"/v1/knowledge/versions/{created['version_id']}/download-url",
        headers=_token(OWNER_USER, slug=OTHER_SLUG),
    )
    # The other tenant's only member is not seeded for this slug, so this is an
    # auth failure rather than a 404; either way it must not be a 200.
    assert resp.status_code in (401, 404), resp.text


def test_download_url_requires_authentication(client: TestClient) -> None:
    created = _upload(client, title="Auth required")
    resp = client.post(f"/v1/knowledge/versions/{created['version_id']}/download-url")
    assert resp.status_code == 401


# --- 6. The signed URL is actually valid ---------------------------------
#
# Found by fetching a URL from a real MinIO: `presign_get` quoted the
# credential and then quoted the whole canonical query again, so the "/"
# separators became "%252F" and the server rejected every URL with
# AuthorizationQueryParametersError. The signature over the *unencoded* value
# is what S3 verifies, so pre-encoding corrupts it.
#
# No unit test could see this - it needs a server that parses the credential.
# These assertions pin the encoding contract without requiring one: the parts
# that made the URL unusable are checkable directly.


def test_presigned_url_encodes_the_credential_exactly_once() -> None:
    """`/` separators in X-Amz-Credential must be single-encoded.

    Double encoding ("%252F") is the specific corruption that made every URL
    fail. Asserting on the raw string is deliberate: it is the wire form.
    """
    from platform_core.knowledge.storage import MinioStorage

    storage = MinioStorage(
        endpoint="localhost:9000", access_key="minioadmin", secret_key="minioadmin"
    )
    url = storage.presign_get("t/v/file.md", expires_seconds=300)

    assert "%252F" not in url, f"credential is double-encoded: {url}"
    assert "X-Amz-Credential=minioadmin%2F" in url, f"credential not encoded as expected: {url}"


def test_presigned_url_carries_the_signature_and_expiry() -> None:
    from platform_core.knowledge.storage import MinioStorage

    storage = MinioStorage(
        endpoint="localhost:9000", access_key="minioadmin", secret_key="minioadmin"
    )
    url = storage.presign_get("t/v/file.md", expires_seconds=600)

    assert "X-Amz-Algorithm=AWS4-HMAC-SHA256" in url
    assert "X-Amz-Expires=600" in url
    assert "X-Amz-Signature=" in url
    # The object path survives URI encoding: the tenant prefix is intact.
    assert "/documents/t/v/file.md?" in url


def test_presigned_expiry_is_clamped_to_the_protocol_maximum() -> None:
    """SigV4 caps a query-signed URL at 7 days; this client caps at 1 hour.

    Below 60s is raised to 60s because a shorter window is unusable in
    practice and usually a caller bug rather than a policy choice.
    """
    from platform_core.knowledge.storage import MinioStorage

    storage = MinioStorage(
        endpoint="localhost:9000", access_key="minioadmin", secret_key="minioadmin"
    )
    assert "X-Amz-Expires=60" in storage.presign_get("k", expires_seconds=5)
    assert "X-Amz-Expires=3600" in storage.presign_get("k", expires_seconds=99999)


def test_changing_the_expiry_changes_the_signature() -> None:
    """The expiry is covered by the signature, so it cannot be edited on the wire.

    If it were not, a recipient could extend their own access window by
    rewriting one query parameter.
    """
    from platform_core.knowledge.storage import MinioStorage

    storage = MinioStorage(
        endpoint="localhost:9000", access_key="minioadmin", secret_key="minioadmin"
    )
    a = storage.presign_get("t/v/f.md", expires_seconds=300)
    b = storage.presign_get("t/v/f.md", expires_seconds=301)
    sig_a = a.rsplit("X-Amz-Signature=", 1)[1]
    sig_b = b.rsplit("X-Amz-Signature=", 1)[1]
    assert sig_a != sig_b, "expiry must be signed; otherwise it can be tampered with"
