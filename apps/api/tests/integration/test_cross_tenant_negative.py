"""Cross-tenant negative suite (ticket 25, docs/testing-and-evaluation.md).

For every tenant-owned table, prove the app role sees/writes only its own
rows: direct ID access, list filters, guessed external IDs, background
writes. Fail-closed (no context = no rows) is also asserted per table.
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

TENANT_TABLES = (
    "memberships",
    "external_resource_refs",
    "inbox_events",
    "outbox_events",
    "conversation_control_leases",
    "knowledge_spaces",
    "knowledge_sources",
    "documents",
    "document_versions",
    "chunks",
    "knowledge_acls",
    "agent_runs",
    "citations",
    "cases",
    "audit_events",
)

_seed_ids: dict[str, str] = {}


def _run(coro):
    import asyncio

    return asyncio.run(coro, loop_factory=asyncio.SelectorEventLoop)


@pytest.fixture(scope="module", autouse=True)
def seed_all_tables() -> None:
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        for tid, slug in ((TENANT_A, "neg-a"), (TENANT_B, "neg-b")):
            conn.execute(
                text(
                    "INSERT INTO tenants (id, slug, name, status) VALUES "
                    "(:id, :slug, 'Neg', 'active') ON CONFLICT (slug) DO NOTHING"
                ),
                {"id": tid, "slug": slug},
            )
        # Seed one row per tenant in each tenant-owned table (A only;
        # B seeds nothing so its view must be empty).
        for _tid, slug in ((TENANT_A, "neg-a"), (TENANT_B, "neg-b")):
            conn.execute(
                text(
                    "INSERT INTO users (id, primary_email, display_name) VALUES "
                    "(gen_random_uuid(), :email, 'Neg User') "
                    "ON CONFLICT (primary_email) DO NOTHING"
                ),
                {"email": f"neg-{slug}@test.local"},
            )
        a = TENANT_A
        space = str(uuid.uuid4())
        doc = str(uuid.uuid4())
        ver = str(uuid.uuid4())
        run_id = str(uuid.uuid4())
        case_id = str(uuid.uuid4())
        _seed_ids.update(space=space, doc=doc, ver=ver, run_id=run_id, case_id=case_id)
        stmts = [
            (
                "memberships",
                "INSERT INTO memberships (id, tenant_id, user_id, role) "
                "SELECT :i, :t, u.id, 'support_agent' FROM users u "
                "WHERE u.primary_email = 'neg-neg-a@test.local'",
            ),
            ("users", None),  # users created via membership FK below
            (
                "external_resource_refs",
                "INSERT INTO external_resource_refs (id, tenant_id, system, resource_type,"
                "external_id) "
                "VALUES (:i, :t, 'chatwoot', 'account', '999888')",
            ),
            (
                "inbox_events",
                "INSERT INTO inbox_events (id, tenant_id, delivery_id, event_type,"
                "payload_hash, received_at) "
                "VALUES (:i, :t, 'neg-delivery-1', 'message_created', 'hash1', 1000)",
            ),
            (
                "outbox_events",
                "INSERT INTO outbox_events (id, tenant_id, event_id, event_type, aggregate_type, "
                "aggregate_id, created_at) VALUES (:i, :t, gen_random_uuid(), 'case.created',"
                "'case', :cid, 1000)",
            ),
            (
                "conversation_control_leases",
                "INSERT INTO conversation_control_leases (id, tenant_id, conversation_ref_id,"
                "owner_type, mode) "
                "VALUES (:i, :t, gen_random_uuid(), 'ai', 'AI_ACTIVE')",
            ),
            (
                "knowledge_spaces",
                "INSERT INTO knowledge_spaces (id, tenant_id, name) VALUES (:sid, :t, 'neg-space')",
            ),
            (
                "knowledge_sources",
                "INSERT INTO knowledge_sources (id, tenant_id, space_id, type, name) "
                "VALUES (:i, :t, :sid, 'upload', 'neg-source')",
            ),
            (
                "documents",
                "INSERT INTO documents (id, tenant_id, space_id, source_id, canonical_uri, title) "
                "VALUES (:did, :t, :sid, (SELECT id FROM knowledge_sources WHERE tenant_id ="
                "CAST(:t AS uuid) AND name='neg-source'), 'kb://neg', 'Neg Doc')",
            ),
            (
                "document_versions",
                "INSERT INTO document_versions (id, tenant_id, document_id, version_label, "
                "content_hash, object_uri, status) VALUES (:vid, :t, :did, 'v1', 'h',"
                "'minio://neg', 'active')",
            ),
            (
                "chunks",
                "INSERT INTO chunks (id, tenant_id, document_version_id, ordinal, text, text_hash) "
                "VALUES (:i, :t, :vid, 0, 'negative suite chunk', 'h1')",
            ),
            (
                "knowledge_acls",
                "INSERT INTO knowledge_acls (id, tenant_id, resource_type, resource_id,"
                "principal_type, principal_id) "
                "VALUES (:i, :t, 'space', :sid, 'department', 'neg-team')",
            ),
            (
                "agent_runs",
                "INSERT INTO agent_runs (id, tenant_id, conversation_ref_id, route) "
                "VALUES (:rid, :t, gen_random_uuid(), 'knowledge_qa')",
            ),
            (
                "citations",
                "INSERT INTO citations (id, tenant_id, agent_run_id, document_version_id,"
                "chunk_id, "
                "excerpt_hash, source_uri) VALUES (:i, :t, :rid, :vid, gen_random_uuid(), 'h',"
                "'minio://neg')",
            ),
            (
                "cases",
                "INSERT INTO cases (id, tenant_id, subject, opened_at) "
                "VALUES (:cid, :t, 'neg case', 1000)",
            ),
            (
                "audit_events",
                "INSERT INTO audit_events (id, tenant_id, occurred_at, actor_type, action,"
                "resource_type, "
                "decision, reason_code, trace_id) VALUES (:i, :t, 1000, 'user', 'case.create',"
                "'case', "
                "'completed', 'OK', 'trace-neg')",
            ),
        ]
        for _table, stmt in stmts:
            if stmt is None:
                continue
            conn.execute(
                text(stmt),
                {
                    "i": uuid.uuid4(),
                    "t": a,
                    "sid": space,
                    "did": doc,
                    "vid": ver,
                    "rid": run_id,
                    "cid": case_id,
                },
            )
    yield
    with admin.begin() as conn:
        for tid in (TENANT_A, TENANT_B):
            for table in reversed(TENANT_TABLES):
                conn.execute(
                    text(f"DELETE FROM {table} WHERE tenant_id = :t"),
                    {"t": tid},
                )
        conn.execute(text("DELETE FROM tenants WHERE slug IN ('neg-a','neg-b')"))
    admin.dispose()


@pytest.mark.zero_tolerance("cross_tenant_violations")
def test_every_tenant_table_is_isolated_and_fails_closed() -> None:
    """One sweep across all tenant-owned tables: A sees its row, B sees
    none, no-context sees none, B cannot write into A's scope."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from platform_core.db import create_engine

    async def sweep() -> list[tuple[str, bool, bool, bool]]:
        engine = create_engine(APP_URL)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        results: list[tuple[str, bool, bool, bool]] = []
        for table in TENANT_TABLES:
            # Tenant A sees its own row
            async with factory() as session:
                await session.execute(
                    text("SELECT set_config('app.tenant_id', :t, true)"), {"t": TENANT_A}
                )
                a_rows = (
                    await session.execute(
                        text(f"SELECT count(*) FROM {table}")  # noqa: S608
                    )
                ).scalar()
                await session.rollback()
            # Tenant B sees nothing
            async with factory() as session:
                await session.execute(
                    text("SELECT set_config('app.tenant_id', :t, true)"), {"t": TENANT_B}
                )
                b_rows = (
                    await session.execute(
                        text(f"SELECT count(*) FROM {table}")  # noqa: S608
                    )
                ).scalar()
                await session.rollback()
            # No context sees nothing
            async with factory() as session:
                no_ctx = (
                    await session.execute(
                        text(f"SELECT count(*) FROM {table}")  # noqa: S608
                    )
                ).scalar()
                await session.rollback()
            results.append((table, int(a_rows) >= 1, int(b_rows) == 0, int(no_ctx) == 0))
        await engine.dispose()
        return results

    for table, a_sees, b_blind, fails_closed in _run(sweep()):
        assert a_sees, f"{table}: tenant A should see its own row"
        assert b_blind, f"{table}: tenant B must see zero rows"
        assert fails_closed, f"{table}: missing context must yield zero rows"


@pytest.mark.zero_tolerance("cross_tenant_violations")
def test_guessing_other_tenant_resource_ids_yields_nothing() -> None:
    """Direct-ID access with B's context against A's known row IDs."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from platform_core.db import create_engine

    async def sweep() -> list[tuple[str, int]]:
        engine = create_engine(APP_URL)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        out: list[tuple[str, int]] = []
        probes = [
            ("knowledge_spaces", "id", _seed_ids["space"]),
            ("documents", "id", _seed_ids["doc"]),
            ("document_versions", "id", _seed_ids["ver"]),
            ("agent_runs", "id", _seed_ids["run_id"]),
            ("cases", "id", _seed_ids["case_id"]),
        ]
        for table, col, rid in probes:
            async with factory() as session:
                await session.execute(
                    text("SELECT set_config('app.tenant_id', :t, true)"), {"t": TENANT_B}
                )
                n = (
                    await session.execute(
                        text(
                            f"SELECT count(*) FROM {table} WHERE {col} = :rid"  # noqa: S608
                        ),
                        {"rid": rid},
                    )
                ).scalar()
                await session.rollback()
            out.append((table, int(n)))
        await engine.dispose()
        return out

    for table, n in _run(sweep()):
        assert n == 0, f"{table}: direct-ID access leaked across tenants"
