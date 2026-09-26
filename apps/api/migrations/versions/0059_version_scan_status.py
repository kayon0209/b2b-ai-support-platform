"""0059: track whether a document version's bytes have been examined.

Why a column rather than a boolean on `status`
----------------------------------------------
`status` answers "where is this version in its lifecycle" and retrieval already
filters on it. Reusing it for "has a scanner read the bytes" would couple two
independent questions: a file can be `active` and unscanned, and a file can be
`superseded` and already scanned. Collapsing them makes the retrieval
predicate ambiguous and leaves nowhere to record a scan that *failed* - which
is the state a scanner outage produces and the one most worth being able to
query.

The retrieval contract
----------------------
`hybrid.py` requires `scan_status = 'clean'`, so the default is not
retrievable. That is deliberate and it cuts both ways:

- A file whose bytes were never examined is not quoted to a customer.
- A scanner that is down produces `error`, also not retrievable. Failing open
  would keep the knowledge base answering while admitting content nobody read,
  which is the exact trade this column exists to prevent.

Which means enabling the filter is a real change in behaviour: every version
that predates this column is `pending`, and becomes invisible to search until a
scanner has processed it. That is correct - an unscanned file has not been
cleared - but it should be a decision made knowingly rather than discovered as
an empty search index, so the backfill below leaves the default alone and the
uploader is what moves rows forward.

No CHECK constraint on the vocabulary. The same reasoning as
`visitor_session_revocations.ended_by`: a constraint here would need a
migration every time a scanner learns a new outcome, and the value set is
already enforced where it is written.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0059_version_scan_status"
down_revision: str | None = "0058_version_bytes_deleted_at"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE document_versions
            ADD COLUMN scan_status varchar(31) NOT NULL DEFAULT 'pending'
        """
    )
    # The scanner's work queue and the operations view of "what has been
    # examined" are the same predicate on the same column, and a partial index
    # on the non-clean values keeps both cheap as the table grows.
    op.execute(
        "CREATE INDEX ix_docversion_not_clean "
        "ON document_versions (tenant_id, scan_status) "
        "WHERE scan_status <> 'clean'"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_docversion_not_clean")
    op.execute("ALTER TABLE document_versions DROP COLUMN IF EXISTS scan_status")
