"""membership invitations for tenant self-service (Phase 5).

Revision ID: 0020
Revises: 0019
Create Date: 2026-09-17

Adds a membership_invitations table to support tenant administrators
inviting new members. The invite token is an opaque, single-use UUIDv7
that expires and can only be consumed within the tenant that created it.
Rows carry tenant_id and are RLS-protected like all tenant-owned tables.
"""

from collections import abc

import sqlalchemy as sa
from alembic import op

revision: str = "0020_membership_invitations"
down_revision: str = "0019_ingestion_reclaim_fn"
branch_labels: abc.Sequence[str] | None = None
depends_on: abc.Sequence[str] | None = None

APP_ROLE = "platform_app"


def upgrade() -> None:
    op.create_table(
        "membership_invitations",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("email", sa.String(255), nullable=False),
        # The role the invitee will receive once they accept. Stored as a
        # plain string to match membership.role; validated at the application
        # layer against MembershipRole.
        sa.Column("role", sa.String(63), nullable=False),
        # Opaque single-use token. Unhashed in the table because it must be
        # returned to the inviter at creation time (for email delivery); in
        # production this would be sent via a side channel (email/SMTP) and
        # only the hash stored. For the local pilot, the inviter receives it
        # directly from the API.
        sa.Column("token", sa.Uuid(), nullable=False, unique=True),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("expires_at", sa.BigInteger(), nullable=False),
        sa.Column("accepted_at", sa.BigInteger(), nullable=True),
        sa.Column("status", sa.String(31), nullable=False, server_default="pending"),
        sa.UniqueConstraint("tenant_id", "email", name="uq_invite_per_tenant_email"),
        sa.UniqueConstraint("tenant_id", "token", name="uq_invite_per_tenant_token"),
    )
    op.create_index("ix_invitations_tenant_id", "membership_invitations", ["tenant_id"])
    op.create_index("ix_invitations_token", "membership_invitations", ["token"], unique=True)
    op.create_index(
        "ix_invitations_status_expires",
        "membership_invitations",
        ["status", "expires_at"],
    )

    # RLS: invitations are tenant-owned. A token is only usable inside the
    # tenant that created it, so an unbound read returns zero rows.
    op.execute("ALTER TABLE membership_invitations ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE membership_invitations FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY tenant_isolation ON membership_invitations
        USING (tenant_id::text = current_setting('app.tenant_id', true))
        WITH CHECK (tenant_id::text = current_setting('app.tenant_id', true))
        """
    )
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON membership_invitations TO {APP_ROLE}")

    # Timestamps owned by the database.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION stamp_membership_invitation_timestamps()
        RETURNS TRIGGER AS $$
        BEGIN
            IF TG_OP = 'INSERT' THEN
                IF NEW.created_at = 0 THEN
                    NEW.created_at := EXTRACT(EPOCH FROM now())::bigint;
                END IF;
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_membership_invitation_timestamps
        BEFORE INSERT OR UPDATE ON membership_invitations
        FOR EACH ROW EXECUTE FUNCTION stamp_membership_invitation_timestamps();
        """
    )


def downgrade() -> None:
    op.drop_table("membership_invitations")
