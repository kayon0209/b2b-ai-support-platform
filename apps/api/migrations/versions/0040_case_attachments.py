"""Case evidence attachments, held as object-store references.

Revision ID: 0040_case_attachments
Revises: 0039_claim_by_version_ids
Create Date: 2026-09-20

The research report's stage 3 pairs a quality complaint with its evidence
("`case.create` + 证据附件（MinIO 预签名 URL）→ 人工裁定"). The complaint half
landed in `case.create`; this is the evidence half.

**The bytes do not live here.** A board photograph or a Gerber archive does not
belong in a row, and the platform already has an object store with the
immutability rule the knowledge originals use. The row holds the reference, the
display name, the accepted content type and the size.

**`UNIQUE (tenant_id, object_key)`, not `UNIQUE (object_key)`.** The key is
built from the tenant, the case and a generated attachment id, so it is unique
by construction; making it unique per tenant means a collision *between*
tenants is also unrepresentable rather than merely unlikely. Two guards on one
property - the same shape as `uq_inbox_delivery`, and for the same reason: a
constraint holds a race that a check-then-insert loses.

**RLS, like every tenant-owned table.** `FORCE` as well as `ENABLE`, so the
table owner is subject to the policy too and a maintenance query cannot read
across tenants by accident. The policy is the same `tenant_isolation` shape the
other tenant tables use; `platform_app` is the only role granted.

`size_bytes` is recorded rather than read back from the store: listing evidence
must not cost a HEAD per row, and the size is part of what the upload was
accepted against.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0040_case_attachments"
down_revision: str | None = "0039_claim_by_version_ids"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "case_attachments"
APP_ROLE = "platform_app"


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("case_id", sa.Uuid(), nullable=False),
        sa.Column("object_key", sa.String(1024), nullable=False),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("content_type", sa.String(127), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("uploaded_by", sa.String(255), nullable=True),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.ForeignKeyConstraint(["case_id"], ["cases.id"], name="fk_case_attachment_case"),
        sa.UniqueConstraint("tenant_id", "object_key", name="uq_case_attachment_key"),
    )
    op.create_index("ix_case_attachments_case", TABLE, ["case_id"])

    op.execute(f"ALTER TABLE {TABLE} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {TABLE} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY tenant_isolation ON {TABLE} "
        "USING (tenant_id::text = current_setting('app.tenant_id', true)) "
        "WITH CHECK (tenant_id::text = current_setting('app.tenant_id', true))"
    )
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {TABLE} TO {APP_ROLE}")


def downgrade() -> None:
    op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {TABLE}")
    op.execute("DROP INDEX IF EXISTS ix_case_attachments_case")
    op.drop_table(TABLE)
