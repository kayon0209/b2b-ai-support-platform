"""Index the high-volume workbench queue reads.

Revision ID: 0054_workbench_queue_indexes
Revises: 0053_brand_display_name_cleanup
Create Date: 2026-09-24

The workbench polls exact queue and per-agent counts alongside an ordered page.
Partial indexes bound these queries to the ownership state they read. They are
created concurrently so deploying the migration does not hold a write lock on
the lease table while the index is built.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0054_workbench_queue_indexes"
down_revision: str | None = "0053_brand_display_name_cleanup"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.create_index(
            "ix_workbench_queue_order",
            "conversation_control_leases",
            ["tenant_id", "updated_at", "id"],
            postgresql_where=sa.text("owner_type = 'queue'"),
            postgresql_concurrently=True,
        )
        op.create_index(
            "ix_workbench_human_owner_order",
            "conversation_control_leases",
            ["tenant_id", "owner_ref", "updated_at", "id"],
            postgresql_where=sa.text("owner_type = 'human'"),
            postgresql_concurrently=True,
        )
        op.create_index(
            "ix_workbench_human_waiting_count",
            "conversation_control_leases",
            ["tenant_id", "owner_ref"],
            postgresql_where=sa.text("owner_type = 'human' AND mode = 'HUMAN_WAITING_CUSTOMER'"),
            postgresql_concurrently=True,
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.drop_index(
            "ix_workbench_human_waiting_count",
            table_name="conversation_control_leases",
            postgresql_concurrently=True,
        )
        op.drop_index(
            "ix_workbench_human_owner_order",
            table_name="conversation_control_leases",
            postgresql_concurrently=True,
        )
        op.drop_index(
            "ix_workbench_queue_order",
            table_name="conversation_control_leases",
            postgresql_concurrently=True,
        )
