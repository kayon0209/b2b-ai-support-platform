"""Integration tests: hybrid retrieval (ticket 14).

Seeds chunks with deterministic embeddings, then verifies:
- FTS path returns lexical matches
- vector path returns semantic-ish matches (deterministic embedder)
- tenant pre-filter: tenant B never sees tenant A chunks (RLS + SQL)
- expired versions are excluded
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
APP_URL = os.environ.get(
    "APP_TEST_DATABASE_URL",
    "postgresql+psycopg://platform_app:platform_app@localhost:5435/platform",
)
TENANT_A = "01900000-0000-7000-8000-000000000001"
TENANT_B = "01900000-0000-7000-8000-000000000002"

REFUND_TEXT = "The refund window is 30 days after purchase for annual plans."
SHIPPING_TEXT = "Standard shipping takes 5 to 7 business days within the region."


def _run(coro):
    import asyncio

    return asyncio.run(coro, loop_factory=asyncio.SelectorEventLoop)


@pytest.fixture(scope="module", autouse=True)
def seed_corpus() -> None:
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        for tid, slug in ((TENANT_A, "ret-a"), (TENANT_B, "ret-b")):
            conn.execute(
                text(
                    "INSERT INTO tenants (id, slug, name, status) VALUES "
                    "(:id, :slug, 'Ret', 'active') ON CONFLICT (slug) DO NOTHING"
                ),
                {"id": tid, "slug": slug},
            )
        # Tenant A corpus
        space = uuid.uuid4()
        conn.execute(
            text("INSERT INTO knowledge_spaces (id, tenant_id, name) VALUES (:i, :t, 'p')"),
            {"i": space, "t": TENANT_A},
        )
        doc = uuid.uuid4()
        conn.execute(
            text(
                "INSERT INTO documents (id, tenant_id, space_id, canonical_uri, title) "
                "VALUES (:i, :t, :s, 'kb://ret', 'Support Policy')"
            ),
            {"i": doc, "t": TENANT_A, "s": space},
        )
        # active version with two chunks
        ver = uuid.uuid4()
        conn.execute(
            text(
                "INSERT INTO document_versions (id, tenant_id, document_id, "
                "version_label, content_hash, object_uri, status) VALUES "
                "(:i, :t, :d, 'v1', 'h', 'minio://a', 'active')"
            ),
            {"i": ver, "t": TENANT_A, "d": doc},
        )
        conn.execute(
            text(
                "INSERT INTO chunks (id, tenant_id, document_version_id, ordinal, "
                "text, text_hash) VALUES (:i, :t, :v, 0, :x, :h)"
            ),
            {"i": uuid.uuid4(), "t": TENANT_A, "v": ver, "x": REFUND_TEXT, "h": "r1"},
        )
        conn.execute(
            text(
                "INSERT INTO chunks (id, tenant_id, document_version_id, ordinal, "
                "text, text_hash) VALUES (:i, :t, :v, 1, :x, :h)"
            ),
            {"i": uuid.uuid4(), "t": TENANT_A, "v": ver, "x": SHIPPING_TEXT, "h": "s1"},
        )
        # expired version with a matching chunk
        ver_x = uuid.uuid4()
        conn.execute(
            text(
                "INSERT INTO document_versions (id, tenant_id, document_id, "
                "version_label, content_hash, object_uri, status, expires_at) VALUES "
                "(:i, :t, :d, 'v0', 'h0', 'minio://a0', 'active', 1000)"
            ),
            {"i": ver_x, "t": TENANT_A, "d": doc},
        )
        conn.execute(
            text(
                "INSERT INTO chunks (id, tenant_id, document_version_id, ordinal, "
                "text, text_hash) VALUES (:i, :t, :v, 0, :x, :h)"
            ),
            {
                "i": uuid.uuid4(),
                "t": TENANT_A,
                "v": ver_x,
                "x": "Old refund window was 14 days.",
                "h": "old",
            },
        )
        # Tenant B corpus with its own refund chunk
        space_b = uuid.uuid4()
        conn.execute(
            text("INSERT INTO knowledge_spaces (id, tenant_id, name) VALUES (:i, :t, 'p')"),
            {"i": space_b, "t": TENANT_B},
        )
        doc_b = uuid.uuid4()
        conn.execute(
            text(
                "INSERT INTO documents (id, tenant_id, space_id, canonical_uri, title) "
                "VALUES (:i, :t, :s, 'kb://ret-b', 'B Policy')"
            ),
            {"i": doc_b, "t": TENANT_B, "s": space_b},
        )
        ver_b = uuid.uuid4()
        conn.execute(
            text(
                "INSERT INTO document_versions (id, tenant_id, document_id, "
                "version_label, content_hash, object_uri, status) VALUES "
                "(:i, :t, :d, 'v1', 'h', 'minio://b', 'active')"
            ),
            {"i": ver_b, "t": TENANT_B, "d": doc_b},
        )
        conn.execute(
            text(
                "INSERT INTO chunks (id, tenant_id, document_version_id, ordinal, "
                "text, text_hash) VALUES (:i, :t, :v, 0, :x, :h)"
            ),
            {
                "i": uuid.uuid4(),
                "t": TENANT_B,
                "v": ver_b,
                "x": "Tenant B refund rules apply here.",
                "h": "b1",
            },
        )
    yield
    with admin.begin() as conn:
        for tid in (TENANT_A, TENANT_B):
            conn.execute(text("DELETE FROM chunks WHERE tenant_id = :t"), {"t": tid})
            conn.execute(text("DELETE FROM document_versions WHERE tenant_id = :t"), {"t": tid})
            conn.execute(text("DELETE FROM documents WHERE tenant_id = :t"), {"t": tid})
            conn.execute(text("DELETE FROM knowledge_spaces WHERE tenant_id = :t"), {"t": tid})
            conn.execute(text("DELETE FROM tenants WHERE slug IN ('ret-a','ret-b')"))
    admin.dispose()


def _set_embeddings() -> None:
    """Fill embeddings with the deterministic embedder via admin connection."""
    from platform_core.retrieval.hybrid import _vector_literal, embed_deterministic

    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        rows = conn.execute(
            text(
                "SELECT c.id, c.text FROM chunks c WHERE c.embedding IS NULL "
                "AND c.tenant_id::text IN (:a, :b)"
            ),
            {"a": TENANT_A, "b": TENANT_B},
        ).all()
        for cid, chunk_text in rows:
            vec = embed_deterministic(chunk_text)
            conn.execute(
                text("UPDATE chunks SET embedding = CAST(:v AS vector) WHERE id = :i"),
                {"v": _vector_literal(vec), "i": cid},
            )
    admin.dispose()


def test_fts_and_vector_paths_with_tenant_isolation() -> None:
    from platform_core.db import create_engine
    from platform_core.retrieval.hybrid import hybrid_search

    _set_embeddings()

    async def scenario() -> tuple[list, list]:
        engine = create_engine(APP_URL)
        factory = async_sessionmaker(engine, expire_on_commit=False)

        async with factory() as session:
            await session.execute(
                text("SELECT set_config('app.tenant_id', :t, true)"), {"t": TENANT_A}
            )
            results_a = await hybrid_search(
                session, tenant_id=uuid.UUID(TENANT_A), query="refund window"
            )
            await session.rollback()

        async with factory() as session:
            await session.execute(
                text("SELECT set_config('app.tenant_id', :t, true)"), {"t": TENANT_B}
            )
            results_b = await hybrid_search(
                session, tenant_id=uuid.UUID(TENANT_B), query="refund window"
            )
            await session.rollback()

        await engine.dispose()
        return results_a, results_b

    results_a, results_b = _run(scenario())

    # Tenant A: refund chunk found, expired 14-day version excluded
    assert results_a, "tenant A should get results"
    top = results_a[0]
    assert "refund" in top.excerpt.lower()
    assert all("14 days" not in r.excerpt for r in results_a)
    assert top.title == "Support Policy"
    # ranking diagnostics present; never presented as probabilities
    assert "lexical" in top.ranking and "vector" in top.ranking

    # Tenant B sees only its own corpus
    assert results_b
    assert all("Tenant B" in r.excerpt for r in results_b)
    assert all(r.title == "B Policy" for r in results_b)


def test_no_cross_tenant_leak_without_results() -> None:
    from platform_core.db import create_engine
    from platform_core.retrieval.hybrid import hybrid_search

    async def scenario() -> list:
        engine = create_engine(APP_URL)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            await session.execute(
                text("SELECT set_config('app.tenant_id', :t, true)"), {"t": TENANT_B}
            )
            results = await hybrid_search(
                session, tenant_id=uuid.UUID(TENANT_B), query="Standard shipping"
            )
            await session.rollback()
        await engine.dispose()
        return results

    # Shipping text belongs to tenant A only. Tenant B may legitimately
    # surface its OWN chunks via the vector path (the deterministic
    # embedder has no semantics), so the invariant to assert is: no
    # tenant-A rows appear in tenant-B results.
    results = _run(scenario())
    assert all("Standard shipping" not in r.excerpt for r in results)
    assert all("refund window is 30 days" not in r.excerpt.lower() for r in results)
    assert all(r.title == "B Policy" for r in results)
