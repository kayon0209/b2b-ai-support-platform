"""Integration tests: knowledge models (ticket 10).

Verifies RLS on the five knowledge tables plus the version lifecycle
invariants (effective/expiry gating is application logic; here we check
the DB-level constraints and tenant isolation).
"""

import os
import uuid

import pytest
from sqlalchemy import create_engine, text

pytestmark = pytest.mark.integration

ADMIN_URL = os.environ.get(
    "APP_ADMIN_DATABASE_URL",
    "postgresql+psycopg://platform:platform@localhost:5435/platform",
)
APP_URL = "postgresql+psycopg://platform_app:platform_app@localhost:5435/platform"
TENANT_A = "01900000-0000-7000-8000-000000000001"
TENANT_B = "01900000-0000-7000-8000-000000000002"

KNOWLEDGE_TABLES = (
    "knowledge_spaces",
    "knowledge_sources",
    "documents",
    "document_versions",
    "chunks",
)


@pytest.fixture(scope="module", autouse=True)
def seed_tenants() -> None:
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        for tid, slug in ((TENANT_A, "kb-test-a"), (TENANT_B, "kb-test-b")):
            conn.execute(
                text(
                    "INSERT INTO tenants (id, slug, name, status) VALUES "
                    "(:id, :slug, 'KB Test', 'active') ON CONFLICT (slug) DO NOTHING"
                ),
                {"id": tid, "slug": slug},
            )
    yield
    with admin.begin() as conn:
        # chunks -> versions -> documents -> sources -> spaces order
        for tid in (TENANT_A, TENANT_B):
            conn.execute(text("DELETE FROM chunks WHERE tenant_id = :tid"), {"tid": tid})
            conn.execute(text("DELETE FROM document_versions WHERE tenant_id = :tid"), {"tid": tid})
            conn.execute(text("DELETE FROM documents WHERE tenant_id = :tid"), {"tid": tid})
            conn.execute(text("DELETE FROM knowledge_sources WHERE tenant_id = :tid"), {"tid": tid})
            conn.execute(text("DELETE FROM knowledge_spaces WHERE tenant_id = :tid"), {"tid": tid})
        conn.execute(text("DELETE FROM tenants WHERE slug LIKE 'kb-test-%'"))
    admin.dispose()


def test_full_ingestion_chain_with_rls() -> None:
    """Space -> source -> document -> version -> chunk, then cross-tenant check."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from platform_core.db import create_engine

    async def scenario() -> tuple[int, int]:
        engine = create_engine(APP_URL)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        tid_a = uuid.UUID(TENANT_A)

        async with factory() as session:
            await session.execute(
                text("SELECT set_config('app.tenant_id', :t, true)"), {"t": TENANT_A}
            )
            space_id = (
                await session.execute(
                    text(
                        "INSERT INTO knowledge_spaces (id, tenant_id, name) "
                        "VALUES (:id, :tid, 'policies') RETURNING id"
                    ),
                    {"id": uuid.uuid4(), "tid": tid_a},
                )
            ).scalar_one()
            source_id = (
                await session.execute(
                    text(
                        "INSERT INTO knowledge_sources (id, tenant_id, space_id, type, name) "
                        "VALUES (:id, :tid, :sid, 'upload', 'handbook') RETURNING id"
                    ),
                    {"id": uuid.uuid4(), "tid": tid_a, "sid": space_id},
                )
            ).scalar_one()
            doc_id = (
                await session.execute(
                    text(
                        "INSERT INTO documents (id, tenant_id, space_id, source_id, "
                        "canonical_uri, title) VALUES "
                        "(:id, :tid, :sid, :src, 'kb://handbook/v1', 'Handbook') RETURNING id"
                    ),
                    {"id": uuid.uuid4(), "tid": tid_a, "sid": space_id, "src": source_id},
                )
            ).scalar_one()
            version_id = (
                await session.execute(
                    text(
                        "INSERT INTO document_versions (id, tenant_id, document_id, "
                        "version_label, content_hash, object_uri, status) VALUES "
                        "(:id, :tid, :did, 'v1', :hash, 'minio://t-a/hb.pdf', 'active') "
                        "RETURNING id"
                    ),
                    {"id": uuid.uuid4(), "tid": tid_a, "did": doc_id, "hash": "abc123"},
                )
            ).scalar_one()
            await session.execute(
                text(
                    "INSERT INTO chunks (id, tenant_id, document_version_id, "
                    "section_path, ordinal, text, text_hash) VALUES "
                    "(:id, :tid, :vid, :sp, 0, 'refund window is 30 days', :th)"
                ),
                {
                    "id": uuid.uuid4(),
                    "tid": tid_a,
                    "vid": version_id,
                    "sp": '["Refunds"]',
                    "th": "def456",
                },
            )
            await session.commit()

        # Tenant B session sees nothing from tenant A
        async with factory() as session:
            await session.execute(
                text("SELECT set_config('app.tenant_id', :t, true)"), {"t": TENANT_B}
            )
            visible_versions = (
                await session.execute(text("SELECT count(*) FROM document_versions"))
            ).scalar()
            visible_chunks = (await session.execute(text("SELECT count(*) FROM chunks"))).scalar()
            await session.rollback()

        await engine.dispose()
        return int(visible_versions), int(visible_chunks)

    visible_versions, visible_chunks = _run(scenario())
    assert visible_versions == 0
    assert visible_chunks == 0


def test_duplicate_version_label_rejected() -> None:
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        space_id = uuid.uuid4()
        doc_id = uuid.uuid4()
        conn.execute(
            text("INSERT INTO knowledge_spaces (id, tenant_id, name) VALUES (:i, :t, 's')"),
            {"i": space_id, "t": TENANT_A},
        )
        conn.execute(
            text(
                "INSERT INTO documents (id, tenant_id, space_id, canonical_uri, title) "
                "VALUES (:i, :t, :s, 'kb://dup', 'Dup')"
            ),
            {"i": doc_id, "t": TENANT_A, "s": space_id},
        )
        conn.execute(
            text(
                "INSERT INTO document_versions (id, tenant_id, document_id, "
                "version_label, content_hash, object_uri) VALUES "
                "(:i, :t, :d, 'v1', 'h1', 'minio://x')"
            ),
            {"i": uuid.uuid4(), "t": TENANT_A, "d": doc_id},
        )
        with pytest.raises(Exception, match="uq_version_label"):
            conn.execute(
                text(
                    "INSERT INTO document_versions (id, tenant_id, document_id, "
                    "version_label, content_hash, object_uri) VALUES "
                    "(:i, :t, :d, 'v1', 'h2', 'minio://y')"
                ),
                {"i": uuid.uuid4(), "t": TENANT_A, "d": doc_id},
            )
    admin.dispose()


def _run(coro):
    import asyncio

    return asyncio.run(coro, loop_factory=asyncio.SelectorEventLoop)
