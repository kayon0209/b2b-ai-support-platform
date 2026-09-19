"""Seed a demo tenant/admin user so the admin UI can be exercised live.

Not part of the product: this is throwaway local tooling for manually
verifying the admin-web against a real API. It uses the `platform` (superuser)
role because it must insert across RLS boundaries before any tenant exists.

Usage:
    python scripts/seed_admin_demo.py
    python scripts/seed_admin_demo.py --purge

Prints the bootstrap token to paste into VITE_API_TOKEN.

`--purge` deletes everything owned by the demo tenant first. Repeated
acceptance runs leave their probes behind (cases named after SQL injection
strings, branding carrying an XSS payload, flags nobody created on purpose),
and a demo tenant that shows those to a reviewer looks like the product put
them there.
"""

import os
import re
import sys
import uuid

from sqlalchemy import create_engine, text

ADMIN_URL = os.environ.get(
    "APP_ADMIN_DATABASE_URL",
    "postgresql+psycopg://platform:platform@localhost:5435/platform",
)

SLUG = "admin-demo"
ROLE = "tenant_owner"
EMAIL = "admin-demo@example.com"


# Ordered children-first so a foreign key never blocks the delete. Only
# tables the demo tenant actually fills are listed; a table added later and
# not listed here is a gap in this script, not a failure of the purge.
PURGE_TABLES = (
    "billing_entries",
    "feature_flag_targets",
    "feature_flags",
    "knowledge_gap_drafts",
    "knowledge_gaps",
    "prompt_versions",
    "case_events",
    "cases",
    "memberships",
    "tenant_branding",
    "audit_events",
    "outbox_events",
)


_IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


def purge(conn: object, tenant_id: uuid.UUID) -> None:
    """Delete the demo tenant's rows, table by table.

    The table name cannot be a bind parameter, so it is interpolated. It is
    checked against a strict identifier pattern first: the values come from a
    module-level constant, but an f-string in a SQL statement should never
    rely on that alone.
    """
    for table in PURGE_TABLES:
        if not _IDENTIFIER.match(table):
            print(f"  refused suspicious table name: {table!r}")
            continue
        try:
            conn.execute(  # type: ignore[attr-defined]
                text(f"DELETE FROM {table} WHERE tenant_id = :tid"),  # noqa: S608
                {"tid": str(tenant_id)},
            )
        except Exception as exc:  # noqa: BLE001 - a missing table is not fatal
            print(f"  skipped {table}: {exc}")


def main() -> None:
    tenant_id = uuid.uuid5(uuid.NAMESPACE_URL, f"tenant:{SLUG}")
    user_id = uuid.uuid5(uuid.NAMESPACE_URL, f"user:{EMAIL}")
    engine = create_engine(ADMIN_URL)

    if "--purge" in sys.argv:
        with engine.begin() as conn:
            print(f"purging demo tenant {SLUG}…")
            purge(conn, tenant_id)

    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO tenants (id, slug, name, status) VALUES "
                "(:id, :slug, 'Admin Demo Tenant', 'active') "
                "ON CONFLICT (slug) DO UPDATE SET name = EXCLUDED.name"
            ),
            {"id": str(tenant_id), "slug": SLUG},
        )
        conn.execute(
            text(
                "INSERT INTO users (id, primary_email, display_name, is_service_account) "
                "VALUES (:id, :email, 'Admin Demo', false) "
                "ON CONFLICT (primary_email) DO UPDATE SET display_name = EXCLUDED.display_name"
            ),
            {"id": str(user_id), "email": EMAIL},
        )
        # Membership is the row the middleware resolves; without it the
        # request fails closed with no role.
        conn.execute(
            text(
                "INSERT INTO memberships (id, tenant_id, user_id, role, status) "
                "VALUES (gen_random_uuid(), :tid, :uid, :role, 'active') "
                "ON CONFLICT (tenant_id, user_id) DO UPDATE SET role = EXCLUDED.role"
            ),
            {"tid": str(tenant_id), "uid": str(user_id), "role": ROLE},
        )

    engine.dispose()
    print(f"tenant_id = {tenant_id}")
    print(f"user_id   = {user_id}")
    print(f"role      = {ROLE}")
    print(f"VITE_API_TOKEN=pt_{SLUG}_{user_id}")


if __name__ == "__main__":
    main()
