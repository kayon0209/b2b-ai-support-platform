"""Case escalation ledger: the record that makes escalation fire once.

Revision ID: 0029_case_escalations
Revises: 0028_org_structure
Create Date: 2026-09-18

`docs/development-plan.md` Phase 2 lists "SLA policy, clocks, pause,
escalation, resolution and reopen". Everything but **escalation** existed:
`cases.models` computes deadlines (`sla_deadline`) and can tell whether one has
passed (`is_breached`), and `is_breached` had **no caller**. So a breached Case
sat there breaching, and the platform's only response was that the number was
still wrong the next time someone looked.

This table is what a scanner needs and the Case row cannot provide: a durable
record that a given clock has already been escalated to a given level.

**`UNIQUE (case_id, clock, level)` is the idempotency mechanism.** Not a
`SELECT`-then-`INSERT` in the scanner: two workers polling concurrently would
both read "not yet escalated" and both escalate, which is the same
check-then-act race the ingestion claim had. With the constraint, the loser
gets an integrity error and skips - and the guarantee holds for any future
caller, including one that is not this worker.

**`breach_seconds` is recorded, not derived.** How far past the deadline the
Case was *when it was escalated* is a fact about the response, and recomputing
it later would silently change history as the clock keeps running - the same
reason `cases.sla_tier` is a snapshot rather than a lookup.

`assignee_ref` / `team_ref` are snapshots of where the escalation was routed.
They are deliberately not foreign keys to anything: routing targets are opaque
external refs (a Chatwoot agent id, a Jira team key), and the point of the
snapshot is to answer "who was told" even after that target is renamed.

Resolved and closed Cases are excluded by the scanner, not by a constraint: a
Case can be reopened, and a reopen restarts the clocks (the transition table
allows RESOLVED/CLOSED -> REOPENED). The ledger key is per clock and level, so
a reopened Case that breaches again is a genuine second breach - but the level
counter does not reset, which is why the scanner's level is derived from the
ladder rather than from the count of existing rows.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0029_case_escalations"
down_revision: str | None = "0028_org_structure"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "platform_app"

# The clocks a Case runs. Kept as a string column rather than an enum type so
# adding a clock (a vendor-response window, say) is a data change, not a type
# migration - and the ladder and scanner both live in Python where the two
# clocks are already first-class.
CLOCKS = ("first_response", "resolution")
MAX_LEVEL = 2

_RLS = """
ALTER TABLE case_escalations ENABLE ROW LEVEL SECURITY;
ALTER TABLE case_escalations FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON case_escalations
    USING (tenant_id::text = current_setting('app.tenant_id', true))
    WITH CHECK (tenant_id::text = current_setting('app.tenant_id', true));
"""


def upgrade() -> None:
    op.create_table(
        "case_escalations",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("case_id", sa.Uuid(), nullable=False),
        sa.Column("clock", sa.String(31), nullable=False),
        sa.Column("level", sa.Integer(), nullable=False),
        sa.Column("reason_code", sa.String(63), nullable=False),
        sa.Column("breach_seconds", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("assignee_ref", sa.String(255), nullable=True),
        sa.Column("team_ref", sa.String(255), nullable=True),
        sa.Column("escalated_at", sa.BigInteger(), nullable=False, server_default="0"),
        sa.CheckConstraint(
            "clock IN ('" + "','".join(CLOCKS) + "')", name="ck_case_escalation_clock"
        ),
        sa.CheckConstraint(f"level >= 1 AND level <= {MAX_LEVEL}", name="ck_case_escalation_level"),
        # The idempotency guarantee. Named so the scanner can recognise the
        # violation it expects and treat it as "already done".
        sa.UniqueConstraint("case_id", "clock", "level", name="uq_case_escalation_once"),
        # A plain FK, not the composite (tenant_id) form used by the org
        # hierarchy: this row is *about* a Case and is always written inside
        # that Case's tenant, and the Case cannot move tenant. The composite
        # form exists to stop a caller *choosing* a foreign parent, which is
        # not a choice available here - the case_id comes from the scan.
        sa.ForeignKeyConstraint(["case_id"], ["cases.id"], name="fk_case_escalation_case"),
    )
    op.create_index("ix_case_escalations_case", "case_escalations", ["case_id"])
    # The scanner's predicate: this tenant's ladders in escalation order.
    op.create_index(
        "ix_case_escalations_tenant_clock", "case_escalations", ["tenant_id", "clock", "level"]
    )

    op.execute(_RLS)
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON case_escalations TO {APP_ROLE}")


def downgrade() -> None:
    op.execute(f"REVOKE ALL ON case_escalations FROM {APP_ROLE}")
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON case_escalations")
    op.drop_index("ix_case_escalations_tenant_clock", table_name="case_escalations")
    op.drop_index("ix_case_escalations_case", table_name="case_escalations")
    op.drop_table("case_escalations")
