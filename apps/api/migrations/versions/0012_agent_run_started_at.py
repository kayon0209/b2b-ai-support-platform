"""Add agent_runs.started_at for the quality dashboard window (ticket 35).

Revision ID: 0012_agent_run_started_at
Revises: 0011_tool_gateway
Create Date: 2026-09-17

`aggregate_quality_metrics` windowed over `AgentRun.created_at`, a column
that never existed on the model or the table. The function had no callers and
no test, so the resulting `UndefinedColumn` never surfaced. This adds the
column the aggregator actually needs.

Expand-migrate-contract: the column is nullable and the index is built
CONCURRENTLY, so the migration is safe while the previous application version
is still serving. Rows written before this migration have no `started_at`;
the aggregator skips them rather than fabricating a timestamp, and a backfill
is a separate, optional step.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012_agent_run_started_at"
down_revision: str | None = "0011_tool_gateway"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "agent_runs",
        sa.Column("started_at", sa.BigInteger(), nullable=True),
    )
    # CONCURRENTLY keeps the write path open; it cannot run inside a
    # transaction block, so it is committed separately.
    with op.get_context().autocommit_block():
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_agent_runs_started_at "
            "ON agent_runs (started_at)"
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_agent_runs_started_at")
    op.drop_column("agent_runs", "started_at")
