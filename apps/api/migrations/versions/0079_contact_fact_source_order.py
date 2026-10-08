"""Order and expire durable contact memory by its source turn.

Revision ID: 0079_contact_fact_source_order
Revises: 0078_quality_reviews
Create Date: 2026-10-08

The database trigger preserves the ordering rule during a rolling deployment:
older application workers still issue unconditional ON CONFLICT updates, so
the table itself must reject stale customer-turn provenance.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0079_contact_fact_source_order"
down_revision: str | None = "0078_quality_reviews"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "contact_facts"
TURN_RETENTION_SECONDS = 90 * 86400


def upgrade() -> None:
    op.add_column(
        TABLE,
        sa.Column("source_ts", sa.BigInteger(), nullable=False, server_default="0"),
    )
    op.add_column(
        TABLE,
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
    )
    op.execute(
        """
        UPDATE contact_facts AS cf
        SET source_ts = COALESCE(
            (
                SELECT ct.ts
                FROM conversation_turns AS ct
                WHERE ct.tenant_id = cf.tenant_id
                  AND ct.id = cf.source_turn_id
            ),
            cf.updated_at
        )
        """
    )
    op.execute(
        sa.text(
            "UPDATE contact_facts SET expires_at = source_ts + :ttl WHERE expires_at IS NULL"
        ).bindparams(ttl=TURN_RETENTION_SECONDS)
    )
    op.create_index(
        "ix_contact_facts_tenant_expiry",
        TABLE,
        ["tenant_id", "expires_at"],
        postgresql_where=sa.text("expires_at IS NOT NULL"),
    )
    op.create_check_constraint(
        "ck_contact_facts_key_allowlist",
        TABLE,
        "key IN ('plan', 'product', 'region', 'case_ref', 'order_ref', 'response_language')",
    )
    op.execute(
        """
        CREATE FUNCTION enforce_contact_fact_source_order()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            turn_timestamp bigint;
            turn_role text;
        BEGIN
            SELECT ts, role
              INTO turn_timestamp, turn_role
              FROM conversation_turns
             WHERE tenant_id = NEW.tenant_id
               AND id = NEW.source_turn_id;

            IF NOT FOUND OR turn_role <> 'customer' THEN
                RETURN NULL;
            END IF;

            NEW.source_ts := turn_timestamp;
            NEW.expires_at := turn_timestamp + 7776000;

            IF TG_OP = 'INSERT' THEN
                NEW.revision := GREATEST(COALESCE(NEW.revision, 1), 1);
                RETURN NEW;
            END IF;

            IF turn_timestamp < OLD.source_ts
               OR (
                   turn_timestamp = OLD.source_ts
                   AND OLD.source_turn_id IS NOT NULL
                   AND NEW.source_turn_id <= OLD.source_turn_id
               ) THEN
                RETURN NULL;
            END IF;

            NEW.revision := OLD.revision + 1;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER contact_fact_source_order
        BEFORE INSERT OR UPDATE ON contact_facts
        FOR EACH ROW EXECUTE FUNCTION enforce_contact_fact_source_order()
        """
    )
    op.execute(
        "COMMENT ON COLUMN contact_facts.source_ts IS 'Server-recorded source customer "
        "turn time; independent of worker arrival time.'"
    )
    op.execute(
        "COMMENT ON COLUMN contact_facts.revision IS 'Monotonic accepted source revision "
        "for this tenant/contact/key.'"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS contact_fact_source_order ON contact_facts")
    op.execute("DROP FUNCTION IF EXISTS enforce_contact_fact_source_order()")
    op.drop_constraint("ck_contact_facts_key_allowlist", TABLE, type_="check")
    op.drop_index("ix_contact_facts_tenant_expiry", table_name=TABLE)
    op.drop_column(TABLE, "revision")
    op.drop_column(TABLE, "source_ts")
