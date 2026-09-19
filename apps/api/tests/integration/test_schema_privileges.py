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

`billing_entries` is withheld the same two privileges for the same reason.
This file caught it as an apparent regression the moment the ledger landed,
which is the allowlist doing its job: the blanket rule could not tell
"append-only by design" from "grant forgotten", so the decision is written
down here instead of the test being loosened.
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
# Table -> the privileges the application role is *supposed* to lack, with the
# reason. A map rather than a set of tables to skip: "this table is special" is
# not a specification, and the first version of this file skipped a table
# entirely rather than saying which privilege was withheld and why.
#
# Found by running `alembic downgrade base && alembic upgrade head`: the test had
# been passing against a development database where `DELETE ON tenants` had been
# granted out of band, and a database built from the migrations alone does not
# have it. The grant in migration 0001 withholds it, and `grep` for
# `delete(Tenant)` / `DELETE FROM tenants` across `apps/` finds nothing - tenant
# deletion is not an application operation at all. The lifecycle answer is
# suspend, and a cascade across every table is not something a request handler
# should be able to trigger.
RESTRICTED_BY_DESIGN: dict[str, tuple[frozenset[str], str]] = {
    "audit_events": (
        frozenset({"UPDATE", "DELETE"}),
        "append-only: an editable audit trail is not an audit trail",
    ),
    "tenants": (
        frozenset({"DELETE"}),
        "tenant deletion is not an application operation; the lifecycle answer is suspend",
    ),
    "billing_entries": (
        frozenset({"UPDATE", "DELETE"}),
        "append-only ledger: a row that can be rewritten is not a record of what "
        "was consumed. Corrections are new rows (`record_adjustment`), and the "
        "rollup applies the sign, so no UPDATE is ever needed",
    ),
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


def test_the_withheld_privileges_are_the_documented_ones() -> None:
    """The listed exceptions must match the database exactly.

    A table that quietly gains a privilege the platform decided to withhold is
    as much a defect as one that loses a privilege it needs, and only this
    direction catches a `GRANT ALL` pasted into a migration.
    """
    grants = _grants()
    for table, (withheld, reason) in RESTRICTED_BY_DESIGN.items():
        assert table in grants, table
        for privilege in withheld:
            assert grants[table][privilege.lower()] is False, f"{table} has {privilege}: {reason}"


def test_the_application_role_can_use_every_table() -> None:
    """A missing grant is invisible until a request hits the table."""
    grants = _grants()
    assert grants, "no tables found - is the database migrated?"

    missing: list[str] = []
    for table, privileges in grants.items():
        withheld, _reason = RESTRICTED_BY_DESIGN.get(table, (frozenset(), ""))
        # SELECT and INSERT are required everywhere: a table the app cannot
        # read is dead weight, and one it cannot write is a write path that
        # fails at the first real request.
        for privilege in ("select", "insert", "update", "delete"):
            if privilege.upper() in withheld:
                continue
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
    assert privileges["update"] is False, RESTRICTED_BY_DESIGN["audit_events"][1]
    assert privileges["delete"] is False, RESTRICTED_BY_DESIGN["audit_events"][1]


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
    rather than one table: an unbound session must see nothing anywhere.

    Scoped to rows that actually belong to a tenant. The isolation policy is
    `tenant_id IS NULL OR tenant_id = app.tenant_id()`, so a row with no
    tenant is global reference data by that policy's own definition - which
    AGENTS.md permits ("unless it is explicitly global reference data").
    Counting those rows too made this test assert "no global data exists"
    rather than "no tenant's data leaks", and it then failed on rows no
    production path can create: `ensure_tool_definitions` is the only insert
    into `tool_definitions` and always sets `tenant_id`, and no migration
    seeds one. Those rows were fixtures that did not clean up after
    themselves, so a hygiene problem was being reported as an isolation
    breach - the least useful shape a failing security test can take.
    """
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
                    stmt = text(
                        f"SELECT count(*) FROM {table}"  # noqa: S608
                        " WHERE tenant_id IS NOT NULL"
                    )
                    counts[table] = int((await session.execute(stmt)).scalar_one())
                return counts
        finally:
            await engine.dispose()

    counts = asyncio.run(_count_unbound(), loop_factory=asyncio.SelectorEventLoop)
    visible = {table: n for table, n in counts.items() if n}
    assert not visible, f"unbound app role can read tenant-owned rows in: {visible}"


def test_every_table_in_the_database_is_known_to_the_orm() -> None:
    """`Base.metadata` must be complete wherever a session can be opened.

    A mapped module that no entry point imports leaves the metadata partial,
    and the symptom is not a missing table - it is a foreign key that cannot
    resolve, raised only in whichever process happens to map the child table
    first. That is exactly how `cases.enterprise_account_id` broke a worker
    test while the API tests passed, which is why `models_registry` exists and
    why it is checked here rather than trusted.

    `alembic_version` is excluded: it is the migration tool's own bookkeeping,
    not a platform table.
    """
    from platform_core.db import create_engine as _create_engine  # noqa: F401
    from platform_core.orm_base import Base

    engine = create_engine(ADMIN_URL)
    with engine.begin() as conn:
        db_tables = {
            str(row[0])
            for row in conn.execute(
                text("SELECT tablename FROM pg_catalog.pg_tables WHERE schemaname = 'public'")
            ).all()
        }
    engine.dispose()

    unknown = sorted(db_tables - set(Base.metadata.tables) - {"alembic_version"})
    assert unknown == [], f"tables exist in the database but no model maps them: {unknown}"
