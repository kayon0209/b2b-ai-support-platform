"""Agent runtime tables: prompt_versions, agent_runs, citations (ticket 16).

Revision ID: 0007_agent_runtime
Revises: 0006_knowledge_acls
Create Date: 2026-09-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0007_agent_runtime"
down_revision: str | None = "0006_knowledge_acls"
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
        "prompt_versions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("template_name", sa.String(127), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("published", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.UniqueConstraint("tenant_id", "template_name", "version", name="uq_prompt_version"),
    )
    op.create_index("ix_prompt_versions_name", "prompt_versions", ["template_name"])
    op.create_index("ix_prompt_versions_tenant_id", "prompt_versions", ["tenant_id"])
    _tenant_rls("prompt_versions")

    op.create_table(
        "agent_runs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("conversation_ref_id", sa.Uuid(), nullable=False),
        sa.Column("case_id", sa.Uuid(), nullable=True),
        sa.Column("route", sa.String(31), nullable=False),
        sa.Column("status", sa.String(31), nullable=False, server_default="queued"),
        sa.Column(
            "prompt_version_id", sa.Uuid(), sa.ForeignKey("prompt_versions.id"), nullable=True
        ),
        sa.Column("model_config", JSONB(), nullable=False, server_default="{}"),
        sa.Column("retrieval_config", JSONB(), nullable=False, server_default="{}"),
        sa.Column("policy_version", sa.String(63), nullable=False, server_default="v1"),
        sa.Column("code_version", sa.String(63), nullable=False, server_default="dev"),
        sa.Column("trace_id", sa.String(63), nullable=False, server_default=""),
        sa.Column("input_hash", sa.String(127), nullable=False, server_default=""),
        sa.Column("output_hash", sa.String(127), nullable=True),
        sa.Column("token_usage", JSONB(), nullable=False, server_default="{}"),
        sa.Column("latency_ms", sa.BigInteger(), nullable=True),
        sa.Column("abstain_reason", sa.String(127), nullable=True),
    )
    op.create_index("ix_agent_runs_status", "agent_runs", ["status"])
    op.create_index("ix_agent_runs_tenant_id", "agent_runs", ["tenant_id"])
    op.create_index("ix_agent_runs_conversation", "agent_runs", ["conversation_ref_id"])
    _tenant_rls("agent_runs")

    op.create_table(
        "citations",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("agent_run_id", sa.Uuid(), sa.ForeignKey("agent_runs.id"), nullable=False),
        sa.Column("document_version_id", sa.Uuid(), nullable=False),
        sa.Column("chunk_id", sa.Uuid(), nullable=False),
        sa.Column("excerpt_hash", sa.String(127), nullable=False),
        sa.Column("source_uri", sa.Text(), nullable=False),
        sa.Column("claim_index", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("retrieval_score", sa.Float(), nullable=False, server_default="0"),
        sa.UniqueConstraint("agent_run_id", "claim_index", name="uq_citation_claim"),
    )
    op.create_index("ix_citations_run", "citations", ["agent_run_id"])
    op.create_index("ix_citations_tenant_id", "citations", ["tenant_id"])
    _tenant_rls("citations")


def downgrade() -> None:
    for table in ("citations", "agent_runs", "prompt_versions"):
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
        op.drop_table(table)
