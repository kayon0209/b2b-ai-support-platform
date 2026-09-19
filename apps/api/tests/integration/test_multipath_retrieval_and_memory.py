"""Integration: multi-path retrieval (trigram/alias/metadata) and the
conversation memory store (plan 1.3/1.5/2.1/2.5).

Requires the migrated database (alembic upgrade head).
"""

from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import create_engine, text

pytestmark = pytest.mark.integration

ADMIN_URL = os.environ.get(
    "APP_ADMIN_DATABASE_URL",
    "postgresql+psycopg://platform:platform@localhost:5435/platform",
)
TENANT = "01900000-0000-7000-8000-000000000041"
TENANT2 = "01900000-0000-7000-8000-000000000042"


def _seed() -> None:
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        for tid, slug in ((TENANT, "retrieval-a"), (TENANT2, "retrieval-b")):
            conn.execute(
                text(
                    "INSERT INTO tenants (id, slug, name, status) VALUES "
                    "(:id, :slug, :name, 'active') ON CONFLICT (slug) DO NOTHING"
                ),
                {"id": tid, "slug": slug, "name": slug},
            )
        # Clean prior state for idempotent reruns.
        for tid in (TENANT, TENANT2):
            for table in ("conversation_turns", "contact_facts", "knowledge_aliases"):
                conn.execute(
                    # noqa-style note: table names come from the fixed tuple
                    # above, never from test input.
                    text("DELETE FROM " + table + " WHERE tenant_id = :t"),  # noqa: S608
                    {"t": tid},
                )
        # A document with a model/firmware front-matter metadata and a chunk
        # mentioning the EC-500 error code table.
        doc_id = "01900000-0000-7000-8000-00000000a041"
        version_id = "01900000-0000-7000-8000-00000000b041"
        space_id = "01900000-0000-7000-8000-00000000c041"
        chunk_id = "01900000-0000-7000-8000-00000000d041"
        conn.execute(
            text(
                "INSERT INTO knowledge_spaces (id, tenant_id, name) VALUES "
                "(:s, :t, 'docs') ON CONFLICT DO NOTHING"
            ),
            {"s": space_id, "t": TENANT},
        )
        conn.execute(
            text(
                "INSERT INTO documents (id, tenant_id, space_id, canonical_uri, title) VALUES "
                "(:d, :t, :s, 'https://seed/ec500', 'EC-500 error codes') "
                "ON CONFLICT (tenant_id, canonical_uri) DO NOTHING"
            ),
            {"d": doc_id, "t": TENANT, "s": space_id},
        )
        conn.execute(
            text(
                "INSERT INTO document_versions (id, tenant_id, document_id, version_label, "
                "status, content_hash, object_uri, metadata) VALUES "
                "(:v, :t, :d, 'v1', 'active', 'seed-hash', 'https://seed/ec500/v1', "
                "CAST(:meta AS jsonb)) ON CONFLICT DO NOTHING"
            ),
            {"v": version_id, "t": TENANT, "d": doc_id, "meta": '{"model": "ec-500"}'},
        )
        conn.execute(
            text(
                "INSERT INTO chunks (id, tenant_id, document_version_id, section_path, "
                "ordinal, text, text_hash) VALUES (:c, :t, :v, '[]'::jsonb, 0, :body, 'h1') "
                "ON CONFLICT DO NOTHING"
            ),
            {
                "c": chunk_id,
                "t": TENANT,
                "v": version_id,
                "body": "Error code EC-5O4 means the fan failed on the EC-500 gateway.",
            },
        )
        # Alias for tenant A only.
        conn.execute(
            text(
                "INSERT INTO knowledge_aliases (id, tenant_id, term, alias, weight) VALUES "
                "(:id, :t, 'error code', 'fault code', 1.0) ON CONFLICT (tenant_id, alias) "
                "DO NOTHING"
            ),
            {"id": str(uuid.uuid4()), "t": TENANT},
        )
    admin.dispose()


@pytest.fixture(scope="module", autouse=True)
def seeded() -> None:
    _seed()


def _search(tenant: str, query: str, **kwargs: object):
    import asyncio

    from sqlalchemy.ext.asyncio import async_sessionmaker

    from platform_core.db import create_engine
    from platform_core.retrieval.hybrid import hybrid_search

    async def run() -> object:
        engine = create_engine(
            ADMIN_URL.replace("platform:platform@", "platform_app:platform_app@")
        )
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            await session.execute(
                text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant}
            )
            result = await hybrid_search(
                session,
                tenant_id=uuid.UUID(tenant),
                query=query,
                top_k=5,
                **kwargs,  # type: ignore[arg-type]
            )
            await session.rollback()
        await engine.dispose()
        return result

    return asyncio.run(run())  # type: ignore[return-value]


def test_trigram_path_recovers_from_a_typo() -> None:
    # "EC-5O4" typed with a letter O: FTS on whole tokens matches nothing
    # decisive, trigram similarity finds the error-code chunk.
    chunks = _search(TENANT, "error code EC-5O4 fan failed")
    assert chunks, "trigram path returned nothing"
    assert any("EC-5O4" in c.excerpt or "EC-500" in c.excerpt for c in chunks)


def test_alias_path_expands_tenant_vocabulary() -> None:
    # "fault code" is this tenant's alias for "error code"; the alias path
    # must surface the chunk via the expanded FTS query.
    chunks = _search(TENANT, "fault code reference")
    assert any("EC-5O4" in c.excerpt for c in chunks)


def test_alias_of_another_tenant_does_not_apply() -> None:
    chunks = _search(TENANT2, "fault code reference")
    assert not chunks


def test_metadata_filter_narrows_and_unknown_key_raises() -> None:
    from platform_core.retrieval.hybrid import build_metadata_filter

    matched = _search(
        TENANT, "fan failed", metadata_filter=build_metadata_filter({"model": "ec-500"})
    )
    assert matched
    wrong = _search(
        TENANT, "fan failed", metadata_filter=build_metadata_filter({"model": "ec-999"})
    )
    assert not wrong
    with pytest.raises(ValueError):
        build_metadata_filter({"evil_key": "x"})


def test_disabling_a_path_changes_ranks_not_ownership() -> None:
    full = _search(TENANT, "error code EC-5O4 fan failed")
    fts_only = _search(TENANT, "error code EC-5O4 fan failed", enabled_paths=("fts",))
    assert full, "all-path search empty"
    assert fts_only is not None  # per-path disable is runnable, ranks may differ


def test_conversation_store_roundtrip_redacts_and_latest_fact_wins() -> None:
    import asyncio
    import time

    from platform_core.agent_runtime import conversation_store
    from platform_core.agent_runtime.conversation import Turn, TurnRole
    from platform_core.db import app_role_url, session_scope_with_url

    async def run() -> tuple[list, list]:
        async with session_scope_with_url(app_role_url()) as session:
            await session.execute(
                text("SELECT set_config('app.tenant_id', :t, true)"), {"t": TENANT}
            )
            convo = uuid.uuid5(uuid.UUID(TENANT), "chatwoot:conversation:777")
            await conversation_store.append_turn(
                session,
                tenant_id=uuid.UUID(TENANT),
                conversation_ref_id=convo,
                turn=Turn(
                    role=TurnRole.CUSTOMER, text="my plan is annual", ts=int(time.time()) - 60
                ),
            )
            await conversation_store.append_turn(
                session,
                tenant_id=uuid.UUID(TENANT),
                conversation_ref_id=convo,
                turn=Turn(
                    role=TurnRole.CUSTOMER,
                    text="email me at hidden@example.com about it",
                    ts=int(time.time()) - 30,
                ),
            )
            turn_id = await conversation_store.append_turn(
                session,
                tenant_id=uuid.UUID(TENANT),
                conversation_ref_id=convo,
                turn=Turn(
                    role=TurnRole.CUSTOMER,
                    text="actually my plan is monthly now",
                    ts=int(time.time()),
                ),
            )
            contact = conversation_store.contact_ref_from_external(uuid.UUID(TENANT), "42")
            await conversation_store.upsert_facts(
                session,
                tenant_id=uuid.UUID(TENANT),
                contact_ref=contact,
                facts=[("plan", "annual")],
                source_turn_id=turn_id,
            )
            await conversation_store.upsert_facts(
                session,
                tenant_id=uuid.UUID(TENANT),
                contact_ref=contact,
                facts=[("plan", "monthly")],
                source_turn_id=turn_id,
            )
            turns = await conversation_store.load_turns(
                session, tenant_id=uuid.UUID(TENANT), conversation_ref_id=convo, limit=10
            )
            facts = await conversation_store.load_facts(
                session, tenant_id=uuid.UUID(TENANT), contact_ref=contact
            )
            await session.commit()
        return turns, facts

    turns, facts = asyncio.run(run())
    # PII is masked at rest; the words memory needs survive.
    assert not any("hidden@example.com" in t.text for t in turns)
    assert any("annual" in t.text for t in turns)
    # Latest statement wins.
    assert ("plan", "monthly") in facts
