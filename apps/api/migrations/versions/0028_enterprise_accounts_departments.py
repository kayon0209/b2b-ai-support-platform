"""EnterpriseAccount and Department, and the two dangling references they close.

Revision ID: 0028_org_structure
Revises: 0027_tenant_domains
Create Date: 2026-09-18

`docs/development-plan.md` Phase 2 lists "Tenant, EnterpriseAccount,
Department and Membership" as one epic. Two of those four did not exist:
`grep -rn "class EnterpriseAccount\\|class Department"` returned nothing, and
`cases.enterprise_account_id` was a bare `uuid` column with no FK and no target
table - so "which account is this Case for" could hold any uuid at all,
including another tenant's, and nothing would ever say so.

Four schema facts, each a decision rather than a translation of the doc.

**Composed foreign keys, not plain ones.** `(parent_id, tenant_id)` references
`(id, tenant_id)`, and the same shape is used for `cases` -> accounts and
`memberships` -> departments. A plain `parent_id REFERENCES ...(id)` would
accept a parent from another tenant: RLS hides other tenants' rows, so the
mistake would not be an error, it would be *invisible* - the row would read as
having no parent and the account would silently become a root. Including
`tenant_id` in the key makes cross-tenant reference unrepresentable, enforced
by the database for every writer including raw SQL.

That also gives the tables a `UNIQUE (id, tenant_id)` they do not otherwise
need, which is exactly what a composite FK requires as a target.

**A cycle is breakable, so it is checked.** `CHECK (parent_id <> id)` covers
the one-node cycle. Longer ones (A -> B -> A) are reachable because a
composite FK checks each row in isolation, so a trigger walks the ancestor
chain and raises `check_violation`. The walk is bounded at 32 levels: the
bound is not a business rule, it is what makes the trigger terminate if a
cycle ever exists despite the check (for example inserted by a session that
disabled triggers), and it turns an infinite loop into a clear error.

**`tier` is a closed set.** An SLA clock is derived from it, so an unrecognised
value has to mean something specific rather than silently falling through to
the default - the fallback is `standard`, but a typo should be rejected at
write time instead. `contract_status` is likewise constrained: a `churned`
account still exists and its history must remain readable, so it is a value,
not a deletion.

**`UNIQUE (tenant_id, external_crm_ref)` is partial.** CRM sync upserts by the
external ref, and two accounts sharing one ref would make the upsert target
ambiguous. Partial because the column is nullable and "no CRM ref" is not a
duplicate of "no CRM ref".

Also added here rather than in a follow-up migration, because they are the same
epic and leaving them out is what created the dangling columns:

- `memberships.department_id` - named in `docs/domain-model.md`
  (`Membership(user_id, tenant_id, department_id?, status)`) and absent from
  the schema.
- `cases.sla_tier` - a snapshot of the tier the clock was started under. The
  deadline is recomputed on a priority change, and without the snapshot
  "why did this Case have a 30-minute first-response target" would depend on
  when the question is asked: a mid-Case contract change would silently move
  an already-running deadline.

Timestamp triggers reuse `set_updated_at()` from migration 0017 unchanged -
it is a generic `NEW.updated_at := now()`. `created_at` gets the same
`IS NULL`-guarded treatment, and both columns carry a dialect-neutral
`server_default` in the model so an ORM insert that does not name them cannot
send an explicit NULL over the column default (the 0017 lesson).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0028_org_structure"
down_revision: str | None = "0027_tenant_domains"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "platform_app"

ACCOUNTS = "enterprise_accounts"
DEPARTMENTS = "departments"

TIERS = ("strategic", "enterprise", "standard", "basic")
CONTRACT_STATUSES = ("active", "pending", "suspended", "churned")

# `NEW.x IS NULL` in PL/pgSQL is true for an explicit NULL and false once a
# value is present, so the trigger fills in only what the writer left out.
# The default is `0` rather than a literal timestamp because the *model*
# declares `server_default="0"`: a real timestamp in two places would drift.
_STAMP_CREATED_AT = """
CREATE OR REPLACE FUNCTION stamp_created_at() RETURNS trigger AS $$
BEGIN
    IF NEW.created_at IS NULL OR NEW.created_at = 0 THEN
        NEW.created_at := CAST(EXTRACT(EPOCH FROM now()) AS bigint);
    END IF;
    NEW.updated_at := CAST(EXTRACT(EPOCH FROM now()) AS bigint);
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""

# One function serving both hierarchies. `TG_TABLE_NAME` is interpolated with
# `%I`, which quotes it as an identifier - so the table name is data the
# database supplies, never a caller, and the format string itself is a
# constant. A second hand-written copy of this walk is how one of the two
# tables ends up with a guard the other has.
#
# **The walk stops on the value, never on `FOUND`.** The first version of this
# function terminated with `IF NOT FOUND THEN cursor_id := NULL`, which reads
# as the obvious way to stop at a row that is not there. Measured against this
# PostgreSQL, `FOUND` is **false** immediately after `EXECUTE ... INTO` even
# when the SELECT returned a row (probe: an AFTER trigger recorded
# `after_execute FOUND=false` for a query whose result was non-null). The
# guard therefore discarded the parent it had just read, and the walk stopped
# one level up. A two-node cycle (A -> B -> A) was accepted while the
# single-node case still failed - but on `CHECK (parent_id <> id)`, not on
# this function, so the guard was quietly doing nothing for any hierarchy
# deeper than one hop. Testing for null cannot go wrong this way: null is the
# correct stop both for a root row and for a parent that does not exist, and
# the composite FK rules out the latter.
#
# Deliberately NOT `SECURITY DEFINER`: the composite FK already guarantees the
# parent sits in the same tenant, so a cycle can only ever be within the
# tenant, and the tenant's own rows are exactly what the calling session's RLS
# exposes. Escalating privileges to read a table we can already read would be a
# grant made for no reason.
_GUARD_CYCLE = """
CREATE OR REPLACE FUNCTION assert_org_hierarchy_acyclic() RETURNS trigger AS $$
DECLARE
    cursor_id uuid := NEW.parent_id;
    depth int := 0;
BEGIN
    WHILE cursor_id IS NOT NULL LOOP
        IF cursor_id = NEW.id THEN
            RAISE EXCEPTION 'cycle in %.parent_id at %', TG_TABLE_NAME, NEW.id
                USING ERRCODE = 'check_violation';
        END IF;
        depth := depth + 1;
        IF depth > 32 THEN
            RAISE EXCEPTION '%.parent_id chain deeper than 32 levels', TG_TABLE_NAME
                USING ERRCODE = 'check_violation';
        END IF;
        EXECUTE format('SELECT parent_id FROM public.%I WHERE id = $1', TG_TABLE_NAME)
            INTO cursor_id USING cursor_id;
    END LOOP;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql SET search_path = pg_catalog, public;
"""

_RLS = """
ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;
ALTER TABLE {table} FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON {table}
    USING (tenant_id::text = current_setting('app.tenant_id', true))
    WITH CHECK (tenant_id::text = current_setting('app.tenant_id', true));
"""


def _hierarchy(table: str, *, extra_columns: list) -> None:
    """Create one tenant-owned hierarchy table.

    Shared because the two tables differ only in their payload columns, and a
    second hand-written copy is how one of them ends up missing a grant - the
    defect `test_schema_privileges.py` was written for.
    """
    op.create_table(
        table,
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("parent_id", sa.Uuid(), nullable=True),
        *extra_columns,
        sa.Column("created_at", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.BigInteger(), nullable=False, server_default="0"),
        # Self-loop, plus the target for the composite self-FK below.
        sa.CheckConstraint(
            "parent_id IS NULL OR parent_id <> id", name=f"ck_{table}_parent_not_self"
        ),
        sa.UniqueConstraint("id", "tenant_id", name=f"uq_{table}_id_tenant"),
    )
    op.create_foreign_key(
        f"fk_{table}_parent_same_tenant",
        table,
        table,
        ["parent_id", "tenant_id"],
        ["id", "tenant_id"],
    )
    op.create_index(f"ix_{table}_tenant", table, ["tenant_id"])

    op.execute(_RLS.format(table=table))
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {APP_ROLE}")

    op.execute(
        f"CREATE TRIGGER trg_{table}_created_at BEFORE INSERT ON {table} "
        "FOR EACH ROW EXECUTE FUNCTION stamp_created_at()"
    )
    op.execute(
        f"CREATE TRIGGER trg_{table}_updated_at BEFORE UPDATE ON {table} "
        "FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
    )


def upgrade() -> None:
    op.execute(_STAMP_CREATED_AT)
    op.execute(_GUARD_CYCLE)

    _hierarchy(
        ACCOUNTS,
        extra_columns=[
            sa.Column("external_crm_ref", sa.String(255), nullable=True),
            sa.Column("name", sa.String(255), nullable=False),
            sa.Column("tier", sa.String(31), nullable=False, server_default="standard"),
            sa.Column("contract_status", sa.String(31), nullable=False, server_default="active"),
            sa.Column("attributes", JSONB(), nullable=False, server_default="{}"),
        ],
    )
    op.create_check_constraint(
        "ck_accounts_tier", ACCOUNTS, "tier IN ('" + "','".join(TIERS) + "')"
    )
    op.create_check_constraint(
        "ck_accounts_contract_status",
        ACCOUNTS,
        "contract_status IN ('" + "','".join(CONTRACT_STATUSES) + "')",
    )
    # Partial: "no CRM ref" is not a duplicate of "no CRM ref", and CRM sync
    # upserts by this value.
    op.create_index(
        "uq_accounts_tenant_crm_ref",
        ACCOUNTS,
        ["tenant_id", "external_crm_ref"],
        unique=True,
        postgresql_where=sa.text("external_crm_ref IS NOT NULL"),
    )
    op.execute(
        f"CREATE TRIGGER trg_{ACCOUNTS}_acyclic BEFORE INSERT OR UPDATE OF parent_id "
        f"ON {ACCOUNTS} FOR EACH ROW EXECUTE FUNCTION assert_org_hierarchy_acyclic()"
    )

    _hierarchy(
        DEPARTMENTS,
        extra_columns=[
            sa.Column("name", sa.String(255), nullable=False),
            sa.Column("slug", sa.String(63), nullable=False),
            sa.Column("external_ref", sa.String(255), nullable=True),
        ],
    )
    op.create_check_constraint("ck_departments_slug_lowercase", DEPARTMENTS, "slug = lower(slug)")
    op.create_unique_constraint("uq_departments_tenant_slug", DEPARTMENTS, ["tenant_id", "slug"])
    op.execute(
        f"CREATE TRIGGER trg_{DEPARTMENTS}_acyclic BEFORE INSERT OR UPDATE OF parent_id "
        f"ON {DEPARTMENTS} FOR EACH ROW EXECUTE FUNCTION assert_org_hierarchy_acyclic()"
    )

    # --- the two dangling references ---
    op.add_column("memberships", sa.Column("department_id", sa.Uuid(), nullable=True))
    op.create_unique_constraint("uq_memberships_id_tenant", "memberships", ["id", "tenant_id"])
    op.create_foreign_key(
        "fk_memberships_department_same_tenant",
        "memberships",
        DEPARTMENTS,
        ["department_id", "tenant_id"],
        ["id", "tenant_id"],
    )
    op.create_index("ix_memberships_department", "memberships", ["department_id"])

    op.add_column("cases", sa.Column("sla_tier", sa.String(31), nullable=True))
    op.create_unique_constraint("uq_cases_id_tenant", "cases", ["id", "tenant_id"])
    op.create_foreign_key(
        "fk_cases_account_same_tenant",
        "cases",
        ACCOUNTS,
        ["enterprise_account_id", "tenant_id"],
        ["id", "tenant_id"],
    )


def downgrade() -> None:
    op.drop_constraint("fk_cases_account_same_tenant", "cases", type_="foreignkey")
    op.drop_constraint("uq_cases_id_tenant", "cases", type_="unique")
    op.drop_column("cases", "sla_tier")

    op.drop_index("ix_memberships_department", table_name="memberships")
    op.drop_constraint("fk_memberships_department_same_tenant", "memberships", type_="foreignkey")
    op.drop_constraint("uq_memberships_id_tenant", "memberships", type_="unique")
    op.drop_column("memberships", "department_id")

    op.execute(f"DROP TRIGGER IF EXISTS trg_{DEPARTMENTS}_acyclic ON {DEPARTMENTS}")
    op.drop_constraint("uq_departments_tenant_slug", DEPARTMENTS, type_="unique")
    op.drop_constraint("ck_departments_slug_lowercase", DEPARTMENTS, type_="check")
    _drop_hierarchy(DEPARTMENTS)

    op.execute(f"DROP TRIGGER IF EXISTS trg_{ACCOUNTS}_acyclic ON {ACCOUNTS}")
    op.drop_index("uq_accounts_tenant_crm_ref", table_name=ACCOUNTS)
    op.drop_constraint("ck_accounts_contract_status", ACCOUNTS, type_="check")
    op.drop_constraint("ck_accounts_tier", ACCOUNTS, type_="check")
    _drop_hierarchy(ACCOUNTS)

    op.execute("DROP FUNCTION IF EXISTS assert_org_hierarchy_acyclic()")
    op.execute("DROP FUNCTION IF EXISTS stamp_created_at()")


def _drop_hierarchy(table: str) -> None:
    op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_updated_at ON {table}")
    op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_created_at ON {table}")
    op.execute(f"REVOKE ALL ON {table} FROM {APP_ROLE}")
    op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
    op.drop_index(f"ix_{table}_tenant", table_name=table)
    op.drop_constraint(f"fk_{table}_parent_same_tenant", table, type_="foreignkey")
    op.drop_table(table)
