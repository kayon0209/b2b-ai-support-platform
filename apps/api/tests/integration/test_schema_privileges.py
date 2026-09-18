"""Every table must be usable by the application role - and one must not be.

Why this test exists
--------------------
A migration can create a table, configure RLS, apply cleanly, and still leave
the application unable to touch it: the grant to `platform_app` is a separate
statement, and forgetting it fails only on a *fresh* database, because an
established one may have the privilege out of band. That is exactly what
happened to `users` (fixed in migration 0021) and again to `tenant_domains`
(migration 0027, found by this suite's sibling tests failing with
`permission denied for table tenant_domains`).

The failure is also late and confusing: the migration is green, the unit tests
are green, and the first request 500s.

The second half is the mirror image and matters just as much: `audit_events`
must have **no** UPDATE or DELETE. An audit trail that can be edited is not an
audit trail, and the way that invariant would break is a migration copying the
usual four-privilege grant. Asserting the exception keeps it an intentional
property rather than an accident of who wrote which migration.
"""

import asyncio
import os

import pytest
from sqlalchemy import create_engine, text

pytestmark = pytest.mark.integration

ADMIN_URL = os.environ.get(
    "APP_ADMIN_DATABASE_URL",
    "postgresql+psycopg://platform:platform@localhost:5435/platform",
)

APP_ROLE = "platform_app"

# Tables the application role must not be able to modify, with the reason.
# A new entry here is a deliberate decision, not a way to silence the test.
READ_ONLY_BY_DESIGN = {
    "audit_events": "append-only: an editable audit trail is not an audit trail",
}

PRIVILEGES = ("SELECT", "INSERT", "UPDATE", "DELETE")


def _grants() -> dict[str, dict[str, bool]]:
    """Privileges the application role holds on every table in `public`.

    The four columns are written out rather than built from a loop: the
    privilege names are part of the SQL grammar here, and interpolating them
    produces a string no reviewer can read and a linter cannot clear.
    """
    engine = create_engine(ADMIN_URL)
    with engine.begin() as conn:
        rows = (
            conn.execute(
                text(
                    "SELECT tablename,"
                    " has_table_privilege(:role, 'public.'||tablename, 'SELECT') AS sel,"
                    " has_table_privilege(:role, 'public.'||tablename, 'INSERT') AS ins,"
                    " has_table_privilege(:role, 'public.'||tablename, 'UPDATE') AS upd,"
                    " has_table_privilege(:role, 'public.'||tablename, 'DELETE') AS del"
                    " FROM pg_catalog.pg_tables"
                    " WHERE schemaname = 'public' ORDER BY tablename"
                ),
                {"role": APP_ROLE},
            )
            .mappings()
            .all()
        )
    engine.dispose()
    return {
        row["tablename"]: {
            "select": bool(row["sel"]),
            "insert": bool(row["ins"]),
            "update": bool(row["upd"]),
            "delete": bool(row["del"]),
        }
        for row in rows
    }


def test_the_application_role_can_use_every_table() -> None:
    """A missing grant is invisible until a request hits the table."""
    grants = _grants()
    assert grants, "no tables found - is the database migrated?"

    missing: list[str] = []
    for table, privileges in grants.items():
        if table in READ_ONLY_BY_DESIGN:
            continue
        # SELECT and INSERT are required everywhere: a table the app cannot
        # read is dead weight, and one it cannot write is a write path that
        # fails at the first real request.
        for privilege in ("select", "insert", "update", "delete"):
            if not privileges[privilege]:
                missing.append(f"{table}.{privilege.upper()}")

    assert not missing, (
        "platform_app lacks privileges on: "
        + ", ".join(missing)
        + " - add GRANT ... TO platform_app in the migration that created them"
    )


def test_the_audit_trail_cannot_be_edited_or_deleted() -> None:
    """Append-only is a security property, and the way it breaks is a
    migration copying the usual four-privilege grant."""
    grants = _grants()
    assert "audit_events" in grants, "audit_events is missing entirely"

    privileges = grants["audit_events"]
    assert privileges["select"] is True
    assert privileges["insert"] is True
    assert privileges["update"] is False, READ_ONLY_BY_DESIGN["audit_events"]
    assert privileges["delete"] is False, READ_ONLY_BY_DESIGN["audit_events"]


def test_tenant_owned_tables_force_row_level_security() -> None:
    """RLS that is only ENABLED is not RLS for the table owner: PostgreSQL
    skips policies for the owner unless FORCE is set, and `platform_app` is
    not the owner - but a future migration run as the owner would silently
    bypass everything without it."""
    engine = create_engine(ADMIN_URL)
    with engine.begin() as conn:
        rows = conn.execute(
            text(
                "SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity "
                "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = 'public' AND c.relkind = 'r' "
                "AND EXISTS (SELECT 1 FROM information_schema.columns col "
                "            WHERE col.table_schema = 'public' "
                "              AND col.table_name = c.relname "
                "              AND col.column_name = 'tenant_id') "
                "ORDER BY c.relname"
            )
        ).all()
    engine.dispose()

    assert rows, "no tenant-owned tables found"
    not_forced = [name for name, enabled, forced in rows if not (enabled and forced)]
    assert not not_forced, "tenant-owned tables without FORCE ROW LEVEL SECURITY: " + ", ".join(
        not_forced
    )


def test_the_unbound_app_role_cannot_read_any_tenant_owned_table() -> None:
    """The read half of the RLS guarantee, checked across the whole schema
    rather than one table: an unbound session must see nothing anywhere."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from platform_core.db import create_engine as app_engine

    app_url = ADMIN_URL.replace("platform:platform@", f"{APP_ROLE}:{APP_ROLE}@")

    engine = create_engine(ADMIN_URL)
    with engine.begin() as conn:
        tables = [
            row[0]
            for row in conn.execute(
                text(
                    "SELECT c.relname FROM pg_class c "
                    "JOIN pg_namespace n ON n.oid = c.relnamespace "
                    "WHERE n.nspname = 'public' AND c.relkind = 'r' "
                    "AND EXISTS (SELECT 1 FROM information_schema.columns col "
                    "            WHERE col.table_schema = 'public' "
                    "              AND col.table_name = c.relname "
                    "              AND col.column_name = 'tenant_id') "
                    "ORDER BY c.relname"
                )
            ).all()
        ]
    engine.dispose()

    async def _count_unbound() -> dict[str, int]:
        engine = app_engine(app_url)
        try:
            factory = async_sessionmaker(engine, expire_on_commit=False)
            async with factory() as session:
                counts: dict[str, int] = {}
                for table in tables:
                    # Table names come from pg_class, not from input.
                    # noqa on the line ruff reports: for an implicitly
                    # concatenated string that is the first part, and the
                    # table name comes from pg_class rather than from input.
                    stmt = text(f"SELECT count(*) FROM {table}")  # noqa: S608
                    counts[table] = int((await session.execute(stmt)).scalar_one())
                return counts
        finally:
            await engine.dispose()

    counts = asyncio.run(_count_unbound(), loop_factory=asyncio.SelectorEventLoop)
    visible = {table: n for table, n in counts.items() if n}
    assert not visible, f"unbound app role can read rows in: {visible}"
