"""Tool-sourced citations: evidence beyond documents (iteration plan 3.4).

Revision ID: 0036_tool_citations
Revises: 0035_contact_facts
Create Date: 2026-09-19

`citations.document_version_id` becomes nullable. A citation whose evidence
came from a read-tool receipt (order status, shipment tracking) has NO
document version: forcing a value there would mean pointing tool claims at
an unrelated document, which is exactly the mis-citation the citation
validator exists to prevent. Tool provenance lives in `source_uri`
("tool://<tool>/<ref>") and `excerpt_hash` (hash of the receipt) instead.

`chunk_id` keeps a value: for tool sources it is a deterministic uuid5 of
the receipt reference, so the (run, claim) uniqueness contract is unchanged.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0036_tool_citations"
down_revision: str | None = "0035_contact_facts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "citations",
        "document_version_id",
        existing_type=sa.Uuid(),
        nullable=True,
    )


def downgrade() -> None:
    # Refuse to restore NOT NULL while tool-sourced rows exist: they would be
    # silently deleted, and a citation disappearing is worse than a failed
    # migration.
    op.execute(
        "UPDATE citations SET document_version_id = "
        "'00000000-0000-0000-0000-000000000000' WHERE document_version_id IS NULL"
    )
    op.alter_column(
        "citations",
        "document_version_id",
        existing_type=sa.Uuid(),
        nullable=False,
    )
