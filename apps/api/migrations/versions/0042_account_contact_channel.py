"""Which channel a contact reached us on.

Revision ID: 0042_account_contact_channel
Revises: 0041_account_contact_bindings
Create Date: 2026-09-21

Feature list 2.1. A company is one customer who arrives through several doors -
web chat, email, WeChat, the marketplace - and each door hands us a different
contact id. `enterprise_account_contacts` already says they are the same
account; what it could not say is *which door*, so the workbench could show the
ids but not what they mean, and a reviewer had to guess whether
"contact-88231" was an email address or a marketplace handle.

Nullable, and deliberately so: a binding created by a CRM sync often has no
channel to name, and inventing one would be worse than a blank. The column
carries no default and no backfill - historical rows simply do not know.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0042_account_contact_channel"
down_revision: str | None = "0041_account_contact_bindings"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "enterprise_account_contacts"


def upgrade() -> None:
    op.add_column(TABLE, sa.Column("channel", sa.String(31), nullable=True))


def downgrade() -> None:
    op.drop_column(TABLE, "channel")
