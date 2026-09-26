"""Give the remaining isolation policies the empty-binding guard.

Revision ID: 0055_rls_empty_binding_guard
Revises: 0054_workbench_queue_indexes
Create Date: 2026-09-24

Three tenant tables were created before migration 0046 established the guarded
form, and never picked it up:

    answer_corrections, conversation_contacts, csat_responses

    USING (tenant_id = (current_setting('app.tenant_id', true))::uuid)

while tables created after it use:

    USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)

The difference is observable, and it is the opposite of what a policy is for.
`current_setting('app.tenant_id', true)` does not return NULL when the GUC is
unbound - it returns an empty string, on a fresh connection and after `RESET`
alike (measured on PostgreSQL 16). So the bare cast evaluates `''::uuid`,
which raises:

    ERROR: invalid input syntax for type uuid: ""

A policy that raises is not a policy that denies. Every other tenant table
fails closed on an unbound connection; these three turn the same situation into
an operational error, so a query that simply forgot to bind a tenant returns a
500 instead of an empty result. The guarded form yields NULL, the predicate
becomes NULL, and no rows match - deny, which is the intent.

Same predicate, same table, three spellings across the schema is itself the
defect: the next table added from whichever example was copied last inherits
whichever behaviour that example had.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0055_rls_empty_binding_guard"
down_revision: str | None = "0054_workbench_queue_indexes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = ("answer_corrections", "conversation_contacts", "csat_responses")

GUARDED = "tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid"
BARE = "tenant_id = (current_setting('app.tenant_id', true))::uuid"


def upgrade() -> None:
    for table in TABLES:
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
        op.execute(
            f"""
            CREATE POLICY tenant_isolation ON {table}
                USING ({GUARDED})
                WITH CHECK ({GUARDED})
            """
        )


def downgrade() -> None:
    for table in TABLES:
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
        op.execute(
            f"""
            CREATE POLICY tenant_isolation ON {table}
                USING ({BARE})
                WITH CHECK ({BARE})
            """
        )
