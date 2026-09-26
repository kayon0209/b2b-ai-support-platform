"""The outbound addressing key for a conversation.

Revision ID: 0050_conversation_channel_key
Revises: 0049_sla_policies
Create Date: 2026-09-23

Found while building the agent reply path, and it is two defects at once.

**1. `conversation_contacts` had no production writer.** `link_conversation`
(and its reader `contact_for_conversation`) existed and were tested, and nothing
outside the tests ever called them - the recurring shape in this repository: a
capability with no consumer. So feature 1.5's cross-device continuity, which
this table *is*, did not actually work: a customer who asked on WeChat and then
wrote an email was two unrelated conversations, because no row ever recorded who
they were. The channel adapter is wired up to call it now.

**2. A reply could not join its own thread.** `channels/outbound.py` says why
this column has to exist: for email, `conversation_key` is the thread root and
is written into `In-Reply-To`/`References`. Omit it and every answer starts a
**new** thread, so the conversation never accumulates. The key is the channel's
own conversation identifier (`message.conversation_key` on the way in) and it
was not stored anywhere the outbound path could reach - `conversation_ref_for`
derives a uuid5 from it, which is one-way.

Nullable on purpose. A platform-surface conversation (`/support`) has no channel
key, and a row that predates this migration cannot be given one. NULL means
"there is no thread to join", which is true and is not the same as `""` - an
empty string would be written into `In-Reply-To` as a header with no value.
"""

import sqlalchemy as sa
from alembic import op

revision = "0050_conversation_channel_key"
down_revision = "0049_sla_policies"
branch_labels = None
depends_on = None

TABLE = "conversation_contacts"


def upgrade() -> None:
    op.add_column(
        TABLE,
        sa.Column("external_conversation_key", sa.String(255), nullable=True),
    )


def downgrade() -> None:
    op.drop_column(TABLE, "external_conversation_key")
