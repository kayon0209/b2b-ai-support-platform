"""Persist copilot provenance on human replies.

Revision ID: 0065_copilot_reply_provenance
Revises: 0064_outbox_claim_started
Create Date: 2026-09-26

Reply provenance is resolved from the copilot job server-side; a caller never
supplies source turn references directly.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0065_copilot_reply_provenance"
down_revision: str | None = "0064_outbox_claim_started"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "conversation_turns",
        sa.Column("copilot_job_id", sa.Uuid(), nullable=True),
    )
    op.add_column(
        "conversation_turns",
        sa.Column(
            "source_refs",
            postgresql.JSONB(),
            nullable=False,
            server_default="[]",
        ),
    )


def downgrade() -> None:
    op.drop_column("conversation_turns", "source_refs")
    op.drop_column("conversation_turns", "copilot_job_id")
