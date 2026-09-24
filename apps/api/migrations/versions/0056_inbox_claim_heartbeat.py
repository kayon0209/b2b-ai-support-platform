"""Record when an inbox row was claimed, and when it was last alive.

Revision ID: 0056_inbox_claim_heartbeat
Revises: 0055_rls_empty_binding_guard
Create Date: 2026-09-24

`reclaim_stale_processing` decided whether a PROCESSING row belonged to a dead
worker by comparing `received_at` against the timeout. Those are different
questions. `received_at` answers "how long has this waited", which says
nothing about who is working on it now.

The consequence only appears under a backlog, which is exactly when recovery
matters. An event waits longer than the ten-minute threshold, a worker finally
claims it, and the very next poll sees a PROCESSING row whose `received_at` is
old - and hands it straight back to the queue while the first worker is still
running it. Two workers, one customer message: the duplicate reply is
suppressed downstream by the outbound command id, so nothing looks wrong from
the outside, but the model runs twice, the tools execute twice and the tenant
is billed twice. Reproduced against the live stack before this migration: a row
enqueued 11 minutes earlier and claimed a moment ago came back as RECEIVED on
the next poll.

Three columns, all nullable, all additive:

- `claimed_at`  - when this worker took the row. The basis for the predicate.
- `heartbeat_at` - when the owning worker last confirmed it was still working.
                 A long run keeps moving this, so a slow worker stays
                 distinguishable from a dead one.
- `worker_id`   - which process holds it, so a stuck row can be attributed.

Nullable rather than NOT NULL so the change is purely additive and a rolling
deploy never sees a row the new code cannot read. The reclaim predicate
coalesces down to `received_at`, which is what rows written by the previous
version have; after this ships, every claimed row carries a claim timestamp.

The index is partial on `status = 'processing'` because that is the only state
the reclaim scans, and the table is the busiest in the schema.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0056_inbox_claim_heartbeat"
down_revision: str | None = "0055_rls_empty_binding_guard"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "inbox_events"


def upgrade() -> None:
    op.add_column(TABLE, sa.Column("claimed_at", sa.BigInteger(), nullable=True))
    op.add_column(TABLE, sa.Column("heartbeat_at", sa.BigInteger(), nullable=True))
    op.add_column(TABLE, sa.Column("worker_id", sa.String(length=64), nullable=True))
    with op.get_context().autocommit_block():
        op.create_index(
            "ix_inbox_events_stale_claim",
            TABLE,
            ["heartbeat_at", "claimed_at", "received_at"],
            postgresql_where=sa.text("status = 'processing'"),
            postgresql_concurrently=True,
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.drop_index("ix_inbox_events_stale_claim", table_name=TABLE)
    op.drop_column(TABLE, "worker_id")
    op.drop_column(TABLE, "heartbeat_at")
    op.drop_column(TABLE, "claimed_at")
