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

## Why the user id is random, and why it is cached

The tenant id is derived from the slug (`uuid5`), so it is the same on every
machine and the purge still finds the rows. The *user* id deliberately is not:
the bootstrap token is `pt_<slug>_<user-id>`, and a derivable user id makes the
token derivable. Anyone holding this file could compute the credential and
authenticate as `tenant_owner` against any deployment that has bootstrap tokens
enabled (`APP_ALLOW_BOOTSTRAP_TOKENS=true`, local/test only - see
`config._assert_auth_is_configured`). Deriving it with `uuid5` meant the demo
token was a published credential, so the token that used to live in the docs
and the acceptance scripts is now a dead string.

A random id would normally mean "whatever you get the first time is what you
keep, and a clone has to seed before it can authenticate". Both halves of that
are handled rather than left to the reader:

- the id is written to `.local/seed-identities.json` (gitignored), so a re-run
  reuses the same identity instead of minting a second one and orphaning the
  first user's memberships;
- the script prints the token and a ready-to-paste `VITE_API_TOKEN=...` line,
  so a fresh clone has a working credential in one command.

`--reset-identity` discards the cached id and mints a new one. The old
membership is left behind on purpose - deleting another identity's rows is not
this script's call - so run `--purge` first if you want the old one gone.
"""

import argparse
import json
import os
import re
import uuid

from sqlalchemy import create_engine, text

ADMIN_URL = os.environ.get(
    "APP_ADMIN_DATABASE_URL",
    "postgresql+psycopg://platform:platform@localhost:5435/platform",
)

SLUG = "admin-demo"
ROLE = "tenant_owner"
EMAIL = "admin-demo@example.com"

# Local, gitignored. Holds only generated identifiers - never a secret, since
# the token is recomputed from them rather than stored.
LOCAL_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".local")
IDENTITY_FILE = os.path.join(LOCAL_DIR, "seed-identities.json")


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


def load_user_id(slug: str, *, reset: bool) -> uuid.UUID:
    """Return the cached demo user id, minting one on first run.

    A malformed cache is treated as absent rather than fatal: this is
    throwaway tooling, and refusing to start because a scratch file was
    hand-edited costs more than re-seeding an identity nobody depends on.
    """
    cached: dict[str, str] = {}
    if os.path.exists(IDENTITY_FILE):
        try:
            with open(IDENTITY_FILE, encoding="utf-8") as handle:
                cached = json.load(handle)
        except (OSError, ValueError):
            print(f"warning: ignoring unreadable {IDENTITY_FILE}")

    if not reset and slug in cached:
        try:
            return uuid.UUID(cached[slug])
        except ValueError:
            print(f"warning: cached id for {slug!r} is not a uuid; minting a new one")

    user_id = uuid.uuid4()
    cached[slug] = str(user_id)
    os.makedirs(os.path.dirname(IDENTITY_FILE), exist_ok=True)
    with open(IDENTITY_FILE, "w", encoding="utf-8") as handle:
        json.dump(cached, handle, indent=2, sort_keys=True)
        handle.write("\n")
    return user_id


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
    parser = argparse.ArgumentParser(description="Seed the local admin-demo tenant.")
    parser.add_argument("--purge", action="store_true", help="delete tenant rows first")
    parser.add_argument(
        "--reset-identity",
        action="store_true",
        help="discard the cached demo user id and mint a new one",
    )
    args = parser.parse_args()

    tenant_id = uuid.uuid5(uuid.NAMESPACE_URL, f"tenant:{SLUG}")
    user_id = load_user_id(SLUG, reset=args.reset_identity)
    engine = create_engine(ADMIN_URL)

    if args.purge:
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
        # The order here is load-bearing. The user id is generated per machine,
        # so on a machine seeded earlier (with a derived id, or an older random
        # one) this email already belongs to a `users` row carrying the *old*
        # pk. Repointing it at the cached id must happen while nothing
        # references the old pk, and the previous run's membership does - so it
        # is released first and re-created below. Scoped to this tenant and
        # this exact email.
        conn.execute(
            text(
                "DELETE FROM memberships WHERE tenant_id = :tid AND user_id IN "
                "(SELECT id FROM users WHERE primary_email = :email)"
            ),
            {"tid": str(tenant_id), "email": EMAIL},
        )
        conn.execute(
            text(
                "INSERT INTO users (id, primary_email, display_name, is_service_account) "
                "VALUES (:id, :email, 'Admin Demo', false) "
                "ON CONFLICT (primary_email) DO UPDATE SET "
                "id = EXCLUDED.id, display_name = EXCLUDED.display_name"
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
        # The read path ("where is my order") needs two more rows before it can
        # run at all, and no migration creates either:
        #
        # 1. An active `business_api` connector. `ConnectorExecutorResolver`
        #    builds an executor only for a provider that has an active
        #    connector row *claiming the tool's capability*, so with no
        #    connector a read tool resolves to nothing and the run hands off
        #    with TOOL_EXECUTOR_MISSING. Measured: this table was empty, which
        #    is why the deployment had published zero `tool` turns even though
        #    the read tools are registered.
        # 2. The `agent.business_read_enabled` flag, which gates the route.
        #
        # The id is derived, like the tenant's, so re-seeding updates the same
        # row instead of accumulating connectors. `base_url` is ignored by the
        # demo adapter (it reads local sample data - see `integrations/demo_erp`)
        # and is set anyway: an empty value reads as a broken connector to
        # whoever looks next, and during a pilot that is a real cost.
        connector_id = uuid.uuid5(uuid.NAMESPACE_URL, f"connector:{SLUG}:business_api")
        conn.execute(
            text(
                "INSERT INTO connectors (id, tenant_id, provider, name, status, "
                "capabilities, configuration, credential_ref) VALUES "
                "(:id, :tid, 'business_api', 'Demo ERP', 'active', "
                "CAST(:caps AS jsonb), CAST(:cfg AS jsonb), NULL) "
                "ON CONFLICT (id) DO UPDATE SET status = 'active', "
                "capabilities = EXCLUDED.capabilities"
            ),
            {
                "id": str(connector_id),
                "tid": str(tenant_id),
                "caps": json.dumps(
                    ["orders_read", "shipments_read", "invoices_read", "inventory_read"]
                ),
                "cfg": json.dumps({"base_url": "http://demo-erp.local"}),
            },
        )
        conn.execute(
            text(
                "INSERT INTO feature_flags (id, tenant_id, key, description, enabled, "
                "rollout_percent, created_at) VALUES "
                "(gen_random_uuid(), :tid, 'agent.business_read_enabled', "
                "'read tools answer from the ERP', true, 100, 0) "
                "ON CONFLICT (tenant_id, key) DO UPDATE SET enabled = true, "
                "rollout_percent = 100"
            ),
            {"tid": str(tenant_id)},
        )

    engine.dispose()
    token = f"pt_{SLUG}_{user_id}"
    print(f"tenant_id = {tenant_id}")
    print(f"user_id   = {user_id}   (cached in .local/seed-identities.json)")
    print(f"role      = {ROLE}")
    print()
    print(f"VITE_API_TOKEN={token}")
    print()
    print("# and the API is reachable as this tenant (expect 200, not 401):")
    print(
        f'curl -s -o /dev/null -w "%{{http_code}}\\n" -H "Authorization: Bearer {token}" '
        "http://localhost:8010/v1/tenant/usage"
    )


if __name__ == "__main__":
    main()
