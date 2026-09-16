"""Integration tests: knowledge ACL filtering (ticket 15).

ACL semantics (fail closed per resource):
- No ACL rows on a resource -> open to all tenant principals.
- ACL rows present -> one must match the caller's principal set, else the
  resource is invisible in retrieval results.
"""

import os
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import async_sessionmaker

pytestmark = pytest.mark.integration

ADMIN_URL = os.environ.get(
    "APP_ADMIN_DATABASE_URL",
    "postgresql+psycopg://platform:platform@localhost:5435/platform",
)
APP_URL = "postgresql+psycopg://platform_app:platform_app@localhost:5435/platform"
TENANT_A = "01900000-0000-7000-8000-000000000001"

OPEN_TEXT = "Open policy: the standard warranty lasts 24 months."
RESTRICTED_TEXT = "Restricted: beta program pricing details are confidential."


def _run(coro):
    import asyncio

    return asyncio.run(coro, loop_factory=asyncio.SelectorEventLoop)


@pytest.fixture(scope="module", autouse=True)
def seed_corpus() -> None:
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO tenants (id, slug, name, status) VALUES "
                "(:id, 'acl-test', 'ACL Tenant', 'active') "
                "ON CONFLICT (slug) DO NOTHING"
            ),
            {"id": TENANT_A},
        )
        space = uuid.uuid4()
        conn.execute(
            text("INSERT INTO knowledge_spaces (id, tenant_id, name) VALUES (:i, :t, 'p')"),
            {"i": space, "t": TENANT_A},
        )
        # Open doc + restricted doc in the same space
        doc_open = uuid.uuid4()
        doc_rest = uuid.uuid4()
        for did, uri, title in (
            (doc_open, "kb://open", "Warranty"),
            (doc_rest, "kb://restricted", "Beta Pricing"),
        ):
            conn.execute(
                text(
                    "INSERT INTO documents (id, tenant_id, space_id, canonical_uri, title) "
                    "VALUES (:i, :t, :s, :u, :ti)"
                ),
                {"i": did, "t": TENANT_A, "s": space, "u": uri, "ti": title},
            )
            ver = uuid.uuid4()
            conn.execute(
                text(
                    "INSERT INTO document_versions (id, tenant_id, document_id, "
                    "version_label, content_hash, object_uri, status) VALUES "
                    "(:i, :t, :d, 'v1', 'h', 'minio://x', 'active')"
                ),
                {"i": ver, "t": TENANT_A, "d": did},
            )
            chunk_text = OPEN_TEXT if did == doc_open else RESTRICTED_TEXT
            conn.execute(
                text(
                    "INSERT INTO chunks (id, tenant_id, document_version_id, ordinal, "
                    "text, text_hash) VALUES (:i, :t, :v, 0, :x, :h)"
                ),
                {"i": uuid.uuid4(), "t": TENANT_A, "v": ver, "x": chunk_text, "h": "h1"},
            )
        # Restrict the beta pricing document to principals in group 'beta-team'
        conn.execute(
            text(
                "INSERT INTO knowledge_acls (id, tenant_id, resource_type, resource_id, "
                "principal_type, principal_id) VALUES "
                "(:i, :t, 'document', :d, 'department', 'beta-team')"
            ),
            {"i": uuid.uuid4(), "t": TENANT_A, "d": doc_rest},
        )
    yield
    with admin.begin() as conn:
        conn.execute(text("DELETE FROM knowledge_acls WHERE tenant_id = :t"), {"t": TENANT_A})
        conn.execute(text("DELETE FROM chunks WHERE tenant_id = :t"), {"t": TENANT_A})
        conn.execute(text("DELETE FROM document_versions WHERE tenant_id = :t"), {"t": TENANT_A})
        conn.execute(text("DELETE FROM documents WHERE tenant_id = :t"), {"t": TENANT_A})
        conn.execute(text("DELETE FROM knowledge_spaces WHERE tenant_id = :t"), {"t": TENANT_A})
        conn.execute(text("DELETE FROM tenants WHERE slug = 'acl-test'"))
    admin.dispose()


def _search(query: str, principal_types: list[str], principal_ids: list[str]) -> list:
    from platform_core.db import create_engine
    from platform_core.retrieval.hybrid import PrincipalScope, hybrid_search

    async def scenario() -> list:
        engine = create_engine(APP_URL)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            await session.execute(
                text("SELECT set_config('app.tenant_id', :t, true)"), {"t": TENANT_A}
            )
            scope = PrincipalScope(
                principal_types=tuple(principal_types),
                principal_ids=tuple(principal_ids),
            )
            results = await hybrid_search(
                session,
                tenant_id=uuid.UUID(TENANT_A),
                query=query,
                principal=scope,
            )
            await session.rollback()
        await engine.dispose()
        return results

    return _run(scenario())


def test_member_of_acl_group_sees_restricted_document() -> None:
    results = _search("beta program pricing", ["department"], ["beta-team"])
    assert any("Restricted" in r.excerpt for r in results)


def test_non_member_cannot_see_restricted_document() -> None:
    results = _search("beta program pricing", ["department"], ["support-team"])
    assert all("Restricted" not in r.excerpt for r in results)


def test_open_document_visible_regardless_of_acl() -> None:
    results = _search("standard warranty", ["department"], ["support-team"])
    assert any("Open policy" in r.excerpt for r in results)


def test_no_principal_scope_still_shows_open_documents() -> None:
    results = _search("standard warranty", [], [])
    assert any("Open policy" in r.excerpt for r in results)
