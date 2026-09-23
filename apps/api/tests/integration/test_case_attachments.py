"""Case evidence attachments: the upload path, the key, and who may.

Storage is stubbed the way the knowledge upload tests stub it - no MinIO runs
in CI, and only the two calls that leave the process are replaced. Everything
that decides anything (the policy gate, the content-type rule, the size cap,
the key construction, the row, RLS) runs for real.

The test that matters most is the traversal one. A filename is the only part of
the object key an uploader controls, and if it can contain a path separator then
`../../<other-tenant>/…` is an object key that writes outside the prefix - which
is a cross-tenant write with no cross-tenant read to notice it.
"""

from __future__ import annotations

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

SLUG = "attach-a"
OTHER_SLUG = "attach-b"
OWNER = "01900000-0000-7000-8000-0000000000a1"
AGENT = "01900000-0000-7000-8000-0000000000a2"
VIEWER = "01900000-0000-7000-8000-0000000000a3"
OTHER_OWNER = "01900000-0000-7000-8000-0000000000a4"


def _admin():
    return create_engine(ADMIN_URL)


def _cleanup() -> None:
    admin = _admin()
    with admin.begin() as conn:
        for table in ("case_attachments", "case_conversations", "cases"):
            conn.execute(
                text(
                    f"DELETE FROM {table} WHERE tenant_id IN "  # noqa: S608 - fixed names
                    "(SELECT id FROM tenants WHERE slug = ANY(:s))"
                ),
                {"s": [SLUG, OTHER_SLUG]},
            )
        conn.execute(
            text(
                "DELETE FROM memberships WHERE tenant_id IN "
                "(SELECT id FROM tenants WHERE slug = ANY(:s))"
            ),
            {"s": [SLUG, OTHER_SLUG]},
        )
        conn.execute(text("DELETE FROM users WHERE primary_email LIKE 'attach-%@example.com'"))
        conn.execute(text("DELETE FROM tenants WHERE slug = ANY(:s)"), {"s": [SLUG, OTHER_SLUG]})
    admin.dispose()


@pytest.fixture(scope="module", autouse=True)
def seed() -> None:
    _cleanup()
    admin = _admin()
    with admin.begin() as conn:
        for tid, slug in ((OWNER, SLUG), (OTHER_OWNER, OTHER_SLUG)):
            conn.execute(
                text(
                    "INSERT INTO tenants (id, slug, name, status) VALUES "
                    "(:id, :slug, 'Attach', 'active') ON CONFLICT (slug) DO NOTHING"
                ),
                {"id": tid, "slug": slug},
            )
        for uid, email, role, tenant in (
            (AGENT, "attach-agent@example.com", "support_agent", OWNER),
            (VIEWER, "attach-viewer@example.com", "support_viewer", OWNER),
            (OTHER_OWNER, "attach-other@example.com", "support_agent", OTHER_OWNER),
        ):
            conn.execute(
                text(
                    "INSERT INTO users (id, primary_email, display_name) VALUES "
                    "(:id, :e, 'Attach Tester') ON CONFLICT (primary_email) DO NOTHING"
                ),
                {"id": uid, "e": email},
            )
            conn.execute(
                text(
                    "INSERT INTO memberships (id, tenant_id, user_id, role) VALUES "
                    "(gen_random_uuid(), :t, :u, :r) ON CONFLICT (tenant_id, user_id) "
                    "DO NOTHING"
                ),
                {"t": tenant, "u": uid, "r": role},
            )
    admin.dispose()
    yield
    _cleanup()


def _token(user: str, slug: str = SLUG, *, idem: bool = True) -> dict[str, str]:
    headers = {"Authorization": f"Bearer pt_{slug}_{user}"}
    if idem:
        headers["Idempotency-Key"] = str(uuid.uuid4())
    return headers


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("APP_ALLOW_BOOTSTRAP_TOKENS", "true")
    monkeypatch.delenv("APP_OIDC_ISSUER", raising=False)

    from platform_core.config import get_settings

    get_settings.cache_clear()

    from fastapi import FastAPI

    from platform_core.identity.middleware import TenantContextMiddleware, build_resolver
    from platform_core.main import app as main_app

    fresh = FastAPI()
    for route in main_app.router.routes:
        fresh.router.routes.append(route)
    fresh.add_middleware(TenantContextMiddleware, resolver=build_resolver())
    try:
        yield TestClient(fresh, raise_server_exceptions=False)
    finally:
        get_settings.cache_clear()


@pytest.fixture(autouse=True)
def stub_storage(monkeypatch: pytest.MonkeyPatch) -> dict[str, list]:
    """Record the two calls that would leave the process."""
    # `from platform_core.knowledge import service` rather than reaching
    # through the package: the submodule is only bound as an attribute once
    # something imports it, so the package-level path fails until then.
    from platform_core.knowledge import service as knowledge_service

    calls: dict[str, list] = {"puts": [], "presigns": []}

    class _FakeStorage:
        def put_object(self, key: str, data: bytes, content_type: str) -> str:
            calls["puts"].append((key, len(data), content_type))
            return key

    def fake_object_storage(settings: object) -> object:
        return _FakeStorage()

    def fake_presign(key: str, *, expires_seconds: int, settings: object = None) -> str:
        calls["presigns"].append((key, expires_seconds))
        return f"http://minio.local/{key}?X-Amz-Expires={expires_seconds}&X-Amz-Signature=stub"

    monkeypatch.setattr(knowledge_service, "object_storage", fake_object_storage)
    monkeypatch.setattr(knowledge_service, "presign_for", fake_presign)
    return calls


def _open_case(*, tenant: str = OWNER, subject: str = "Quality complaint") -> str:
    case_id = str(uuid.uuid4())
    admin = _admin()
    with admin.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO cases (id, tenant_id, subject, description, status, priority, "
                "category, version, opened_at, elapsed_running_seconds, "
                "last_state_changed_at) VALUES (:i, :t, :s, '', 'new', 'p2', "
                "'quality_complaint', 1, 0, 0, 0)"
            ),
            {"i": case_id, "t": tenant, "s": subject},
        )
    admin.dispose()
    return case_id


def _upload(
    client: TestClient,
    case_id: str,
    *,
    filename: str = "board.jpg",
    content: bytes = b"\xff\xd8\xff board photo",
    content_type: str = "image/jpeg",
    headers: dict[str, str] | None = None,
):
    return client.post(
        f"/v1/cases/{case_id}/attachments",
        headers=headers or _token(AGENT),
        files={"file": (filename, content, content_type)},
    )


# --- The round trip --------------------------------------------------------


def test_evidence_round_trips_and_comes_back_with_a_signed_url(
    client: TestClient, stub_storage: dict
) -> None:
    case_id = _open_case()

    uploaded = _upload(client, case_id, filename="board.jpg", content=b"photo bytes")
    assert uploaded.status_code == 200, uploaded.text
    body = uploaded.json()["attachment"]
    assert body["filename"] == "board.jpg"
    assert body["content_type"] == "image/jpeg"
    assert body["size_bytes"] == len(b"photo bytes")
    # No URL on the upload response: a link minted here would outlive the
    # request that asked for it, and it is the read path that needs one.
    assert body["url"] is None
    assert stub_storage["puts"] and stub_storage["puts"][0][1] == len(b"photo bytes")

    listed = client.get(f"/v1/cases/{case_id}/attachments", headers=_token(AGENT))
    assert listed.status_code == 200, listed.text
    items = listed.json()["items"]
    assert len(items) == 1
    assert items[0]["url"].startswith("http://minio.local/")
    assert "X-Amz-Expires=" in items[0]["url"]
    assert stub_storage["presigns"][0][1] <= 3600


def test_the_object_key_is_tenant_prefixed(client: TestClient, stub_storage: dict) -> None:
    case_id = _open_case()

    assert _upload(client, case_id).status_code == 200

    key = stub_storage["puts"][0][0]
    assert key.startswith(f"{OWNER}/cases/{case_id}/"), key


def test_a_traversing_filename_cannot_escape_the_prefix(
    client: TestClient, stub_storage: dict
) -> None:
    """The only part of the key an uploader controls is the display name.

    If a separator survives into the key, `../../<other-tenant>/x` writes an
    object outside this tenant's prefix - a cross-tenant *write* with no
    cross-tenant read to notice it.
    """
    case_id = _open_case()

    resp = _upload(
        client,
        case_id,
        filename="../../../other-tenant/secret.jpg",
        content=b"photo",
    )
    assert resp.status_code == 200, resp.text

    key = stub_storage["puts"][0][0]
    assert key.startswith(f"{OWNER}/cases/{case_id}/")
    # The property is *segment count*, not the absence of dots. The separators
    # are replaced, so the name stays one segment and nothing it contains can
    # move the path - and asserting `".." not in key` would have been asserting
    # a stronger property than safety needs, while missing the one that does
    # matter (a segment of exactly `..`, tested below).
    assert key.count("/") == 4, key
    assert key.rsplit("/", 1)[1] not in {".", ".."}, key
    # And what the operator is shown keeps the original name - sanitising the
    # key must not silently rename their file.
    assert resp.json()["attachment"]["filename"].endswith("secret.jpg")


@pytest.mark.parametrize("filename", ["..", "."])
def test_a_filename_that_is_only_a_dot_segment_is_replaced(
    client: TestClient, stub_storage: dict, filename: str
) -> None:
    """`..` is the one name a URL consumer normalises.

    A longer name that merely contains dots is a single literal segment and
    moves nothing, but a segment of exactly `..` resolves to the parent - which
    is why the guard is equality rather than a substring check.
    """
    case_id = _open_case()

    resp = _upload(client, case_id, filename=filename, content=b"photo")
    assert resp.status_code == 200, resp.text

    key = stub_storage["puts"][0][0]
    assert key.rsplit("/", 1)[1] == "attachment", key


# --- The refusals ----------------------------------------------------------


def test_an_unlisted_content_type_is_refused(client: TestClient) -> None:
    case_id = _open_case()

    resp = _upload(client, case_id, filename="payload.exe", content_type="application/x-msdownload")

    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == "UNSUPPORTED_ATTACHMENT_TYPE"


def test_an_empty_file_is_refused(client: TestClient) -> None:
    """A zero-byte attachment is a reference to nothing, and it would sit in
    the evidence list looking like something to review."""
    case_id = _open_case()

    resp = _upload(client, case_id, content=b"")

    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == "EMPTY_ATTACHMENT"


def test_upload_requires_an_idempotency_key(client: TestClient) -> None:
    """A retried upload is a second copy of the evidence."""
    case_id = _open_case()

    resp = _upload(client, case_id, headers=_token(AGENT, idem=False))

    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"


def test_a_role_without_case_update_cannot_attach_evidence(client: TestClient) -> None:
    """`support_viewer` reads cases and cannot change one, so it cannot add to
    the record either."""
    case_id = _open_case()

    resp = _upload(client, case_id, headers=_token(VIEWER))

    assert resp.status_code == 403, resp.text


def test_a_role_with_case_read_can_see_the_evidence(client: TestClient) -> None:
    """The gate is per action, not per endpoint: reading the evidence is a read."""
    case_id = _open_case()
    assert _upload(client, case_id).status_code == 200

    resp = client.get(f"/v1/cases/{case_id}/attachments", headers=_token(VIEWER))

    assert resp.status_code == 200, resp.text
    assert len(resp.json()["items"]) == 1


# --- Isolation -------------------------------------------------------------


def test_another_tenants_case_is_invisible(client: TestClient, stub_storage: dict) -> None:
    """RLS, not a filter: the case is not there to attach to."""
    foreign = _open_case(tenant=OTHER_OWNER, subject="Not yours")

    resp = _upload(client, foreign)

    assert resp.status_code == 404, resp.text
    assert resp.json()["error"]["code"] == "CASE_NOT_FOUND"
    # And nothing was stored: the check runs before the bytes leave.
    assert stub_storage["puts"] == []


def test_another_tenants_evidence_cannot_be_listed(client: TestClient) -> None:
    foreign = _open_case(tenant=OTHER_OWNER, subject="Not yours")

    resp = client.get(f"/v1/cases/{foreign}/attachments", headers=_token(AGENT))

    assert resp.status_code == 404, resp.text


def test_a_malformed_case_id_is_a_validation_error(client: TestClient) -> None:
    resp = client.get("/v1/cases/not-a-uuid/attachments", headers=_token(AGENT))

    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == "VALIDATION_FAILED"


def test_the_key_builder_covers_names_http_cannot_express() -> None:
    """An empty filename is handled here rather than over HTTP.

    `httpx` cannot send a multipart part with an empty filename - it drops the
    filename and the field arrives as a plain string, which FastAPI rejects as
    a 422 before the endpoint sees it. The endpoint also guards it
    (`file.filename or "attachment"`), so the pure function is the right place
    to pin the behaviour rather than pretending the HTTP path can reach it.
    """
    from platform_core.cases.attachments import attachment_object_key

    key = attachment_object_key(
        tenant_id=uuid.UUID(OWNER),
        case_id=uuid.UUID(OTHER_OWNER),
        attachment_id=uuid.UUID(VIEWER),
        filename="",
    )

    assert key == f"{OWNER}/cases/{OTHER_OWNER}/{VIEWER}/attachment"
