"""0058: record when a document version's bytes were erased.

Why a column and not a status
-----------------------------
`status = 'expired'` is written by the retention sweep at the moment the policy
decides the version is past its window. That is a *decision*, not an outcome:
the sweep can mark a row expired, then fail before reaching storage, and the
two are indistinguishable afterwards - every expired row looks like it has been
erased. Operators and auditors both need to ask "did the bytes actually go?",
and `bytes_deleted_at` is what answers it.

It also drives erasure itself. The erasure pass selects rows with
`bytes_deleted_at IS NULL`, so a row whose delete failed is retried on the next
run and a row already erased is not touched twice. Without the stamp the pass
would have to re-attempt the whole expired set on every cycle, forever.

Null semantics
--------------
NULL means one of two things, deliberately conflated because the correct action
is the same for both: either the bytes are still there, or we have not yet
established that they are gone. Either way, try to erase. Non-null means the
endpoint reported the object absent.

Nothing in this migration deletes anything. It widens the table; backfilling
the rows that predate it would mean asserting an erasure that never happened.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0058_version_bytes_deleted_at"
down_revision: str | None = "0057_visitor_session_revocation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE document_versions
            ADD COLUMN bytes_deleted_at bigint NULL
        """
    )
    # Partial index, because the erasure pass scans exactly this set: rows whose
    # bytes are not yet confirmed gone. Indexing every row would carry the ones
    # already erased, which are never selected again.
    op.execute(
        "CREATE INDEX ix_docversion_pending_erasure "
        "ON document_versions (tenant_id) "
        "WHERE bytes_deleted_at IS NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_docversion_pending_erasure")
    op.execute("ALTER TABLE document_versions DROP COLUMN IF EXISTS bytes_deleted_at")
