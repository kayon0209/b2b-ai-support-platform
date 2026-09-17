"""Ingestion claim bookkeeping: `document_versions.updated_at` + claim index.

Revision ID: 0017_ingestion_claim
Revises: 0016_oidc_subject_bootstrap
Create Date: 2026-09-17

Why this migration exists
-------------------------
The ingestion worker claims versions with `FOR UPDATE SKIP LOCKED` and must
recover a claim abandoned by a dead worker. Recovery needs to know *when* a
row entered its current state, and `document_versions` had no such column:
`orm_base.TimestampMixin` provides `created_at`, but `DocumentVersion` does
not use that mixin, and `created_at` would be the wrong signal anyway - a
document uploaded yesterday and claimed a second ago is not stale.

So there are two additions, each fixing a measured problem rather than a
hypothetical one:

1. **`updated_at bigint` + a trigger.** Bigint epoch seconds, matching every
   other clock column in this schema (`effective_at`, `expires_at`,
   `cases.opened_at`). A `timestamp with time zone` column would compare
   against `EXTRACT(EPOCH FROM now())` differently from its neighbours and
   invite exactly the type-mismatch bug migration 0008 hit.

   A trigger rather than application-side writes: the ingestion state machine
   is advanced from two places (`service.mark_ready` on the API path, the
   worker's `_advance`), and a third caller added later would silently forget
   to stamp the row - at which point stale-claim recovery starts reclaiming
   rows that are being actively processed, and two workers ingest the same
   document concurrently. The database owns the invariant instead.

2. **A partial index on the claim predicate.** The worker's claim query is
   `WHERE ingestion_status IN ('uploaded','queued_for_retry') ORDER BY
   created_at LIMIT n`. Without an index that is a sequential scan of every
   version in every tenant on every poll - once per second per worker, on the
   bulk queue that is supposed to be the *lowest* priority consumer. Partial
   because the rows eligible for claim are a tiny, transient fraction of the
   table: indexing only them keeps the index small enough to stay in cache
   and makes the scan O(claimable) rather than O(all versions).

Rollback note: the trigger and function are dropped first, then the index,
then the column. Dropping the column while the trigger still references it
would fail on the next UPDATE against the table.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0017_ingestion_claim"
down_revision = "0016_oidc_subject_bootstrap"
branch_labels = None
depends_on = None

# Names chosen to be greppable: one prefix for everything this migration owns.
COLUMN = "updated_at"
FUNCTION = "set_updated_at"
TRIGGER = "trg_document_versions_updated_at"
INDEX = "ix_docversion_claimable"
INSERT_FUNCTION = "set_document_version_created_at"
INSERT_TRIGGER = "trg_document_versions_created_at"
STAMP = "CAST(EXTRACT(EPOCH FROM now()) AS bigint)"

# The UPDATE trigger. `CREATE OR REPLACE` and `DROP ... IF EXISTS` so
# re-running a partially-applied migration converges instead of erroring.
_CREATE_FUNCTION = f"""
CREATE OR REPLACE FUNCTION {FUNCTION}() RETURNS trigger AS $$
BEGIN
    NEW.{COLUMN} := CAST(EXTRACT(EPOCH FROM now()) AS bigint);
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""

# The INSERT trigger stamps `created_at` **and** `updated_at`. Both columns
# are NOT NULL and the database, not the application, owns their values, so
# the trigger is the single writer for the INSERT path.
#
# The `IS NULL` guard on each leaves an explicitly supplied value alone, which
# is what backfills and tests need. It is not a substitute for the column
# defaults: a caller that sends an explicit NULL - which the ORM does for any
# column it does not know to omit - is still caught here before the INSERT
# reaches the constraint check.
#
# Measured, not assumed: before this function stamped `updated_at`, the upload
# endpoint returned HTTP 503 `IntegrityError` with
# `null value in column "updated_at" of relation "document_versions"`, because
# SQLAlchemy emitted `updated_at = NULL` explicitly and an explicit NULL
# overrides the column DEFAULT.
_CREATE_INSERT_FUNCTION = f"""
CREATE OR REPLACE FUNCTION {INSERT_FUNCTION}() RETURNS trigger AS $$
BEGIN
    IF NEW.created_at IS NULL THEN
        NEW.created_at := {STAMP};
    END IF;
    IF NEW.{COLUMN} IS NULL THEN
        NEW.{COLUMN} := {STAMP};
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""


def upgrade() -> None:
    # `created_at` first: the claim query orders by it (FIFO across uploads)
    # and it did not exist on this table either, even though the ORM's
    # `TimestampMixin` provides it for other models. Discovered by running
    # this migration, not by reading the model file - the model declares
    # neither timestamp, so introspection agreed with the wrong schema.
    #
    # Added here rather than in a separate revision because the index below
    # depends on it: one migration, one atomic change to this table's
    # queueing behaviour.
    op.add_column(
        "document_versions",
        sa.Column(
            "created_at",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("CAST(EXTRACT(EPOCH FROM now()) AS bigint)"),
        ),
    )

    # `server_default` backfills existing rows in one statement, which is why
    # the column can be NOT NULL without a separate data migration. The value
    # is "now" for pre-existing rows: a version uploaded before this migration
    # has no claim in flight, so treating it as freshly-touched is the safe
    # direction - it delays a spurious reclaim rather than causing one.
    op.add_column(
        "document_versions",
        sa.Column(
            COLUMN,
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("CAST(EXTRACT(EPOCH FROM now()) AS bigint)"),
        ),
    )

    op.execute(_CREATE_FUNCTION)
    op.execute(_CREATE_INSERT_FUNCTION)
    op.execute(
        f"""
        CREATE TRIGGER {TRIGGER}
        BEFORE UPDATE ON document_versions
        FOR EACH ROW
        EXECUTE FUNCTION {FUNCTION}();
        """
    )
    op.execute(
        f"""
        CREATE TRIGGER {INSERT_TRIGGER}
        BEFORE INSERT ON document_versions
        FOR EACH ROW
        EXECUTE FUNCTION {INSERT_FUNCTION}();
        """
    )

    # Partial index over exactly the claim predicate. Partial because the
    # rows eligible for claim are a tiny, transient fraction of the table:
    # indexing only them keeps the index small enough to stay in cache and
    # makes the scan O(claimable) rather than O(all versions), on a query the
    # bulk worker runs every second.
    op.execute(
        f"""
        CREATE INDEX {INDEX} ON document_versions (created_at)
        WHERE ingestion_status IN ('uploaded', 'queued_for_retry')
        """
    )


def downgrade() -> None:
    op.execute(f"DROP INDEX IF EXISTS {INDEX}")
    op.execute(f"DROP TRIGGER IF EXISTS {TRIGGER} ON document_versions")
    op.execute(f"DROP TRIGGER IF EXISTS {INSERT_TRIGGER} ON document_versions")
    op.execute(f"DROP FUNCTION IF EXISTS {FUNCTION}()")
    op.execute(f"DROP FUNCTION IF EXISTS {INSERT_FUNCTION}()")
    op.drop_column("document_versions", COLUMN)
    op.drop_column("document_versions", "created_at")
