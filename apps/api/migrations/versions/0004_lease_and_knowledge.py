"""conversation_control_leases (ticket 9) + knowledge tables (ticket 10).

Revision ID: 0004_lease_and_knowledge
Revises: 0003_outbox
Create Date: 2026-09-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0004_lease_and_knowledge"
down_revision: str | None = "0003_outbox"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "platform_app"


def _tenant_rls(table: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"""
        CREATE POLICY tenant_isolation ON {table}
        USING (tenant_id::text = current_setting('app.tenant_id', true))
        WITH CHECK (tenant_id::text = current_setting('app.tenant_id', true))
        """
    )
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {APP_ROLE}")


def upgrade() -> None:
    op.create_table(
        "conversation_control_leases",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("conversation_ref_id", sa.Uuid(), nullable=False),
        sa.Column("owner_type", sa.String(15), nullable=False),
        sa.Column("owner_ref", sa.String(255), nullable=True),
        sa.Column("mode", sa.String(63), nullable=False, server_default="AI_ACTIVE"),
        sa.Column("lease_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("expires_at", sa.BigInteger(), nullable=True),
        sa.Column("changed_reason", sa.String(255), nullable=False, server_default="created"),
        sa.Column("updated_at", sa.BigInteger(), nullable=False, server_default="0"),
        sa.UniqueConstraint("tenant_id", "conversation_ref_id", name="uq_lease_per_conversation"),
    )
    op.create_index("ix_lease_owner", "conversation_control_leases", ["owner_type"])
    op.create_index("ix_lease_tenant_id", "conversation_control_leases", ["tenant_id"])
    _tenant_rls("conversation_control_leases")

    op.create_table(
        "knowledge_spaces",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("status", sa.String(31), nullable=False, server_default="active"),
        sa.Column("default_policy_id", sa.Uuid(), nullable=True),
    )
    op.create_index("ix_kspaces_tenant_id", "knowledge_spaces", ["tenant_id"])
    _tenant_rls("knowledge_spaces")

    op.create_table(
        "knowledge_sources",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("space_id", sa.Uuid(), sa.ForeignKey("knowledge_spaces.id"), nullable=False),
        sa.Column("type", sa.String(31), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("config_ref", sa.String(255), nullable=True),
        sa.Column("sync_cursor", sa.Text(), nullable=True),
        sa.Column("status", sa.String(31), nullable=False, server_default="active"),
        sa.UniqueConstraint("tenant_id", "space_id", "name", name="uq_source_per_space"),
    )
    op.create_index("ix_ksources_tenant_id", "knowledge_sources", ["tenant_id"])
    op.create_index("ix_ksources_space_id", "knowledge_sources", ["space_id"])
    _tenant_rls("knowledge_sources")

    op.create_table(
        "documents",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("space_id", sa.Uuid(), sa.ForeignKey("knowledge_spaces.id"), nullable=False),
        sa.Column("source_id", sa.Uuid(), sa.ForeignKey("knowledge_sources.id"), nullable=True),
        sa.Column("canonical_uri", sa.Text(), nullable=False),
        sa.Column("title", sa.String(512), nullable=False),
        sa.Column("owner_ref", sa.String(255), nullable=True),
        sa.Column("classification", sa.String(31), nullable=False, server_default="internal"),
        sa.UniqueConstraint("tenant_id", "canonical_uri", name="uq_document_uri"),
    )
    op.create_index("ix_documents_tenant_id", "documents", ["tenant_id"])
    op.create_index("ix_documents_space_id", "documents", ["space_id"])
    _tenant_rls("documents")

    op.create_table(
        "document_versions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), sa.ForeignKey("documents.id"), nullable=False),
        sa.Column("version_label", sa.String(63), nullable=False),
        sa.Column("content_hash", sa.String(127), nullable=False),
        sa.Column("effective_at", sa.BigInteger(), nullable=True),
        sa.Column("expires_at", sa.BigInteger(), nullable=True),
        sa.Column("status", sa.String(31), nullable=False, server_default="draft"),
        sa.Column("object_uri", sa.Text(), nullable=False),
        sa.Column("parser_version", sa.String(63), nullable=False, server_default="v1"),
        sa.Column("ingestion_status", sa.String(31), nullable=False, server_default="uploaded"),
        sa.Column("metadata", JSONB(), nullable=False, server_default="{}"),
        sa.UniqueConstraint("document_id", "version_label", name="uq_version_label"),
    )
    op.create_index(
        "ix_docversion_status_dates", "document_versions", ["status", "effective_at", "expires_at"]
    )
    op.create_index("ix_docversion_tenant_id", "document_versions", ["tenant_id"])
    _tenant_rls("document_versions")

    op.create_table(
        "chunks",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column(
            "document_version_id",
            sa.Uuid(),
            sa.ForeignKey("document_versions.id"),
            nullable=False,
        ),
        sa.Column("section_path", JSONB(), nullable=False, server_default="[]"),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("text_hash", sa.String(127), nullable=False),
        sa.Column("metadata", JSONB(), nullable=False, server_default="{}"),
        sa.UniqueConstraint("document_version_id", "ordinal", name="uq_chunk_ordinal"),
    )
    op.create_index("ix_chunks_tenant_id", "chunks", ["tenant_id"])
    op.create_index("ix_chunks_docversion_id", "chunks", ["document_version_id"])
    _tenant_rls("chunks")


def downgrade() -> None:
    for table in (
        "chunks",
        "document_versions",
        "documents",
        "knowledge_sources",
        "knowledge_spaces",
        "conversation_control_leases",
    ):
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
        op.drop_table(table)
