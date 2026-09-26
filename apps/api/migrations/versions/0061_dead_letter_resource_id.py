"""0061: make a dead letter actionable, and record what a replay was a replay of.

Two columns, one reason
-----------------------
`dead_letter_items` stored `operation_digest` and nothing that identifies a
row. That is the correct design for the only producer it had - a connector
operation, where the payload is deliberately not stored and the operator's
question is "is this the same failure repeating?", answered by matching digests
against each other.

It is the wrong design for an agent run. An operator looking at a failed run
wants to open that run and try it again, and a digest cannot be resolved back
to a row. So:

- `dead_letter_items.resource_id` points at whatever the failure is about. NULL
  for connector rows written before this, which is correct: they are identified
  by digest, and backfilling an id for them would be inventing provenance the
  original code never captured.
- `agent_runs.replay_of_run_id` records that a run exists because an operator
  asked for one. This is the difference between "replay" and "duplicate". A
  replay that leaves no trace is indistinguishable from a second customer
  message, and the audit trail is the reason the original failure is still worth
  looking at.

Why a partial unique index on the lineage
-----------------------------------------
At most one *live* replay per failed run. A completed replay must not block a
later legitimate retry - the first attempt may have failed for a reason that
has since been fixed - so the constraint covers queued and running replays only.

Enforced in the database rather than in the service alone because the service
check is a read followed by a write, and two operators clicking at the same
moment is exactly the case the constraint exists for. A service-only check
would pass the tests, which is what makes it worth putting here.

`dead_letter_items.resource_id` is deliberately not a foreign key. The table
already points at several kinds of thing by string, and a foreign key would
have to name one. A dangling id is a dead letter about something since deleted,
which is harmless and better than blocking the delete.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0061_dead_letter_resource_id"
down_revision: str | None = "0060_agent_run_version"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_LIVE_REPLAY_STATUSES = ("queued", "running")


def upgrade() -> None:
    op.execute("ALTER TABLE dead_letter_items ADD COLUMN resource_id uuid NULL")
    op.execute("ALTER TABLE agent_runs ADD COLUMN replay_of_run_id uuid NULL")

    # Partial, because a finished replay must not block the next attempt.
    predicate = " AND ".join(f"status = '{s}'" for s in _LIVE_REPLAY_STATUSES)
    op.execute(
        "CREATE UNIQUE INDEX uq_agent_runs_live_replay "
        "ON agent_runs (replay_of_run_id) "
        f"WHERE replay_of_run_id IS NOT NULL AND ({predicate})"
    )

    # For the operator's list query: "this tenant's failed runs, newest first".
    op.execute(
        "CREATE INDEX ix_dead_letter_resource "
        "ON dead_letter_items (tenant_id, resource_id) "
        "WHERE resource_id IS NOT NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_dead_letter_resource")
    op.execute("DROP INDEX IF EXISTS uq_agent_runs_live_replay")
    op.execute("ALTER TABLE agent_runs DROP COLUMN IF EXISTS replay_of_run_id")
    op.execute("ALTER TABLE dead_letter_items DROP COLUMN IF EXISTS resource_id")
