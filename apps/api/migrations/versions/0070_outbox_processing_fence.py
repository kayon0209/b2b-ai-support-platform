"""Fence dedicated outbox consumers after stale-claim recovery.

Revision ID: 0070_outbox_processing_fence
Revises: 0069_signed_release_evidence
Create Date: 2026-09-28
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0070_outbox_processing_fence"
down_revision: str | None = "0069_signed_release_evidence"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "outbox_events",
        sa.Column("processing_token", postgresql.UUID(as_uuid=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("outbox_events", "processing_token")
