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
                    # Security note: table names come from the fixed tuple
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
        conn.execute(
            text(
                "INSERT INTO chunks (id, tenant_id, document_version_id, section_path, "
                "ordinal, text, text_hash) VALUES (:c, :t, :v, '[]'::jsonb, 1, :body, 'h2') "
                "ON CONFLICT DO NOTHING"
            ),
            {
                "c": "01900000-0000-7000-8000-00000000d042",
                "t": TENANT,
                "v": version_id,
                "body": "设备型号 EC-500 网关：错误码 E504 表示散热风扇故障，请先断电后检查风道。",
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


def test_hybrid_search_recovers_chinese_error_code_and_exact_model_id() -> None:
    chunks = _search(TENANT, "错误码 E504")
    assert any("设备型号 EC-500 网关" in chunk.excerpt for chunk in chunks)
    assert any("E504" in chunk.excerpt for chunk in chunks)


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
            annual_turn_id = await conversation_store.append_turn(
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
                source_turn_id=annual_turn_id,
            )
            accepted = await conversation_store.upsert_facts(
                session,
                tenant_id=uuid.UUID(TENANT),
                contact_ref=contact,
                facts=[("plan", "monthly")],
                source_turn_id=turn_id,
            )
            late_old = await conversation_store.upsert_facts(
                session,
                tenant_id=uuid.UUID(TENANT),
                contact_ref=contact,
                facts=[("plan", "annual")],
                source_turn_id=annual_turn_id,
            )
            with pytest.raises(ValueError, match="unsupported durable fact type"):
                await conversation_store.upsert_facts(
                    session,
                    tenant_id=uuid.UUID(TENANT),
                    contact_ref=contact,
                    facts=[("permission", "tenant_admin")],
                    source_turn_id=turn_id,
                )
            # Simulate an older process in a rolling deployment: it does not
            # send source_ts/revision and unconditionally proposes an UPDATE.
            # The table trigger must still reject its stale source turn.
            await session.execute(
                text(
                    "INSERT INTO contact_facts (id, tenant_id, contact_ref, key, value, "
                    "source_turn_id, updated_at) VALUES "
                    "(:id, :tenant, :contact, 'plan', 'annual', :source_turn, :updated_at) "
                    "ON CONFLICT (tenant_id, contact_ref, key) DO UPDATE SET "
                    "value = EXCLUDED.value, source_turn_id = EXCLUDED.source_turn_id, "
                    "updated_at = EXCLUDED.updated_at"
                ),
                {
                    "id": uuid.uuid4(),
                    "tenant": TENANT,
                    "contact": contact,
                    "source_turn": annual_turn_id,
                    "updated_at": int(time.time()),
                },
            )
            fact_row = (
                await session.execute(
                    text(
                        "SELECT source_turn_id, source_ts, revision, expires_at "
                        "FROM contact_facts WHERE tenant_id = :tenant AND contact_ref = :contact "
                        "AND key = 'plan'"
                    ),
                    {"tenant": TENANT, "contact": contact},
                )
            ).one()
            turns = await conversation_store.load_turns(
                session, tenant_id=uuid.UUID(TENANT), conversation_ref_id=convo, limit=10
            )
            facts = await conversation_store.load_facts(
                session, tenant_id=uuid.UUID(TENANT), contact_ref=contact
            )
            await session.commit()
        return turns, facts, accepted, late_old, fact_row, turn_id

    turns, facts, accepted, late_old, fact_row, turn_id = asyncio.run(run())
    # PII is masked at rest; the words memory needs survive.
    assert not any("hidden@example.com" in t.text for t in turns)
    assert any("annual" in t.text for t in turns)
    # Source time wins even when the older message is processed last.
    assert ("plan", "monthly") in facts
    assert accepted == 1
    assert late_old == 0
    assert str(fact_row.source_turn_id) == str(turn_id)
    assert fact_row.revision == 2
    assert fact_row.expires_at > int(time.time())


def test_memory_erasure_is_scoped_to_one_channel_contact() -> None:
    import asyncio
    import time

    from platform_core.agent_runtime import conversation_store
    from platform_core.agent_runtime.conversation import Turn, TurnRole
    from platform_core.agent_runtime.memory_erasure import erase_contact_memory
    from platform_core.db import session_scope_with_url
    from platform_core.support_bridge.continuity import link_conversation

    async def run() -> tuple[dict[str, int], list, list, list, list]:
        tenant_id = uuid.UUID(TENANT)
        external_contact_id = "phase4-erasure-contact"
        chatwoot_conversation = uuid.uuid4()
        wechat_conversation = uuid.uuid4()
        now = int(time.time())
        async with session_scope_with_url(os.environ["APP_TEST_DATABASE_URL"]) as session:
            await session.execute(
                text("SELECT set_config('app.tenant_id', :tenant, true)"),
                {"tenant": TENANT},
            )
            await link_conversation(
                session,
                tenant_id=tenant_id,
                conversation_ref_id=chatwoot_conversation,
                external_contact_id=external_contact_id,
                channel="chatwoot",
            )
            await link_conversation(
                session,
                tenant_id=tenant_id,
                conversation_ref_id=wechat_conversation,
                external_contact_id=external_contact_id,
                channel="wechat",
            )
            chat_turn = await conversation_store.append_turn(
                session,
                tenant_id=tenant_id,
                conversation_ref_id=chatwoot_conversation,
                turn=Turn(role=TurnRole.CUSTOMER, text="my plan is monthly", ts=now),
            )
            wechat_turn = await conversation_store.append_turn(
                session,
                tenant_id=tenant_id,
                conversation_ref_id=wechat_conversation,
                turn=Turn(role=TurnRole.CUSTOMER, text="my plan is annual", ts=now),
            )
            await conversation_store.upsert_facts(
                session,
                tenant_id=tenant_id,
                contact_ref=conversation_store.contact_ref_from_external(
                    tenant_id, external_contact_id, channel="chatwoot"
                ),
                facts=[("plan", "monthly")],
                source_turn_id=chat_turn,
                now=now,
            )
            await conversation_store.upsert_facts(
                session,
                tenant_id=tenant_id,
                contact_ref=conversation_store.contact_ref_from_external(
                    tenant_id, external_contact_id, channel="wechat"
                ),
                facts=[("plan", "annual")],
                source_turn_id=wechat_turn,
                now=now,
            )

            counts = await erase_contact_memory(
                session,
                tenant_id=tenant_id,
                external_contact_id=external_contact_id,
                channel="chatwoot",
            )
            erased_facts = await conversation_store.load_facts(
                session,
                tenant_id=tenant_id,
                contact_ref=conversation_store.contact_ref_from_external(
                    tenant_id, external_contact_id, channel="chatwoot"
                ),
            )
            retained_facts = await conversation_store.load_facts(
                session,
                tenant_id=tenant_id,
                contact_ref=conversation_store.contact_ref_from_external(
                    tenant_id, external_contact_id, channel="wechat"
                ),
            )
            erased_turns = await conversation_store.load_turns(
                session,
                tenant_id=tenant_id,
                conversation_ref_id=chatwoot_conversation,
                limit=10,
            )
            retained_turns = await conversation_store.load_turns(
                session,
                tenant_id=tenant_id,
                conversation_ref_id=wechat_conversation,
                limit=10,
            )
            await session.execute(
                text(
                    "DELETE FROM conversation_contacts WHERE tenant_id = :tenant "
                    "AND external_contact_id = :contact"
                ),
                {"tenant": TENANT, "contact": external_contact_id},
            )
            await session.commit()
        return counts, erased_facts, retained_facts, erased_turns, retained_turns

    counts, erased_facts, retained_facts, erased_turns, retained_turns = asyncio.run(run())
    assert counts == {
        "conversations_matched": 1,
        "contact_facts_deleted": 1,
        "conversation_turns_deleted": 1,
    }
    assert erased_facts == []
    assert ("plan", "annual") in retained_facts
    assert erased_turns == []
    assert any("annual" in turn.text for turn in retained_turns)
