"""0060: a version counter on agent runs, so a terminal write can be claimed.

What this is for
----------------
A run was finalized by mutating the ORM object and flushing, which is a blind
write: two workers on the same run both write a terminal state and the second
overwrites the first with no trace. The row ends up healthy-looking, holding one
answer's `output_hash` with nothing to say another answer existed.

`version` is the receipt for a compare-and-set. `claim_terminal` bumps it inside
the same `UPDATE` that checks the status, so exactly one concurrent caller can
win and the losers get `None` rather than silence.

Why the column defaults to 0 and not 1
---------------------------------------
Rows that exist today have had no claims and no versioned writes. Starting them
at 0 says "nothing has claimed this yet", which is true, and keeps the first
claim's bump to 1 - which is also what a fresh row does. A default of 1 would
make a never-claimed row indistinguishable from a claimed one.

Not a UNIQUE or a constraint
---------------------------
The value's meaning is "number of claims taken", enforced only by
`claim_terminal`. A CHECK would encode a rule that only holds while every
writer goes through that function, and would turn a future bulk repair into a
migration.

No backfill beyond the default
------------------------------
Existing runs are not retroactively claimable - they are already terminal or
already held. Backfilling a version that suggests a claim happened would be a
fabricated audit trail.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0060_agent_run_version"
down_revision: str | None = "0059_version_scan_status"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE agent_runs
            ADD COLUMN version integer NOT NULL DEFAULT 0
        """
    )


def downgrade() -> None:
    op.execute("ALTER TABLE agent_runs DROP COLUMN IF EXISTS version")
