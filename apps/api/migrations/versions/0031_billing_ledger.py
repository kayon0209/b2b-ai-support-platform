"""Billing ledger: the consumer `usage.recorded` never had.

Revision ID: 0031_billing_ledger
Revises: 0030_saml_scim
Create Date: 2026-09-18

`docs/development-plan.md` Phase 5 lists "usage quotas and billing events".
Half of it existed: `identity/usage.py` counts runs and enforces a quota, and
`orchestrator.py` enqueues a `usage.recorded` event when a run reaches a
terminal state. Nothing consumed those events - `build_default_relay`
registered only `case.created` and `case.updated`, so every usage event
travelled the outbox and was logged as "unhandled". A billing event that is
emitted but never aggregated is a log line, not a billing system.

The table is the aggregation target.

**`event_id` UNIQUE is the whole idempotency story.** The outbox is
at-least-once by design (a crash after publish re-sends), so the consumer can
see the same `usage.recorded` twice. Deriving the ledger key from the event's
own id - rather than from `(run_id)` alone - keeps the two concerns separate:
two legitimate events about one run (a completion and a later correction)
would collide on run_id, while a redelivery of one event collides on event_id,
which is exactly the intended behaviour.

**Token counts are copied, not referenced.** `agent_runs.token_usage` is the
live record and may be written again; the ledger is what a monthly invoice is
computed from, and an invoice must not change because a run row was touched.
The same snapshot reasoning as `cases.sla_tier`.

**No UPDATE or DELETE grant for the app role.** A ledger that can be edited in
place is not a ledger. Corrections are new rows with a negative or adjusted
count (`entry_kind`), so the history stays readable.

`period_start` is stored because invoices are per calendar month and
recomputing it from `recorded_at` at read time would make a row's period
depend on the tenant's current timezone configuration - a row written in
March must belong to March forever.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0031_billing_ledger"
down_revision: str | None = "0030_saml_scim"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "platform_app"

# 'usage' is the normal terminal outcome. 'adjustment' exists so a correction
# is an append rather than an edit.
ENTRY_KINDS = ("usage", "adjustment")

_RLS = """
ALTER TABLE billing_entries ENABLE ROW LEVEL SECURITY;
ALTER TABLE billing_entries FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON billing_entries
    USING (tenant_id::text = current_setting('app.tenant_id', true))
    WITH CHECK (tenant_id::text = current_setting('app.tenant_id', true));
"""


def upgrade() -> None:
    op.create_table(
        "billing_entries",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        # The outbox event that produced this row. Unique, so a redelivered
        # event cannot bill twice.
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("entry_kind", sa.String(31), nullable=False, server_default="usage"),
        sa.Column("route", sa.String(31), nullable=False, server_default=""),
        sa.Column("run_status", sa.String(31), nullable=False, server_default=""),
        sa.Column("prompt_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("completion_tokens", sa.Integer(), nullable=False, server_default="0"),
        # Calendar month the entry belongs to, as UTC epoch seconds of its
        # first instant. Stored, not derived - see the module docstring.
        sa.Column("period_start", sa.BigInteger(), nullable=False),
        sa.Column("recorded_at", sa.BigInteger(), nullable=False),
        sa.CheckConstraint(
            "entry_kind IN ('" + "','".join(ENTRY_KINDS) + "')", name="ck_billing_entry_kind"
        ),
        sa.CheckConstraint("prompt_tokens >= 0", name="ck_billing_prompt_tokens_non_negative"),
        sa.CheckConstraint(
            "completion_tokens >= 0", name="ck_billing_completion_tokens_non_negative"
        ),
        sa.UniqueConstraint("event_id", name="uq_billing_entry_event"),
    )
    # The monthly rollup: this tenant's entries for one period.
    op.create_index(
        "ix_billing_entries_tenant_period", "billing_entries", ["tenant_id", "period_start"]
    )
    op.create_index("ix_billing_entries_run", "billing_entries", ["run_id"])

    op.execute(_RLS)
    # No UPDATE, no DELETE: append-only by grant. ROLLBACK rather than a
    # silent failure, so an attempt to rewrite history is loud.
    op.execute(f"GRANT SELECT, INSERT ON billing_entries TO {APP_ROLE}")


def downgrade() -> None:
    op.execute(f"REVOKE ALL ON billing_entries FROM {APP_ROLE}")
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON billing_entries")
    op.drop_index("ix_billing_entries_run", table_name="billing_entries")
    op.drop_index("ix_billing_entries_tenant_period", table_name="billing_entries")
    op.drop_table("billing_entries")
