"""Persist idempotency receipts for operator-requested AgentRun reruns.

Revision ID: 0080_agent_run_rerun_idempotency
Revises: 0079_contact_fact_source_order
Create Date: 2026-10-08
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0080_agent_run_rerun_idempotency"
down_revision: str | None = "0079_contact_fact_source_order"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("agent_runs", sa.Column("replay_request_key_hash", sa.String(64), nullable=True))
    op.create_index(
        "uq_agent_runs_replay_request_key",
        "agent_runs",
        ["tenant_id", "replay_of_run_id", "replay_request_key_hash"],
        unique=True,
        postgresql_where=sa.text(
            "replay_of_run_id IS NOT NULL AND replay_request_key_hash IS NOT NULL"
        ),
    )


def downgrade() -> None:
    op.drop_index("uq_agent_runs_replay_request_key", table_name="agent_runs")
    op.drop_column("agent_runs", "replay_request_key_hash")
