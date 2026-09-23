"""How an agent's reply was composed.

Revision ID: 0051_turn_origin
Revises: 0050_conversation_channel_key
Create Date: 2026-09-23

"AI 建议话术采纳率" is the number that says whether the copilot is worth
anything, and it was unmeasurable: a reply arrived as a turn with no record of
whether the agent typed it, inserted a template, or sent the model's suggestion
as-is. Three very different outcomes with identical rows.

Two columns rather than one because they answer different questions:

- `origin` - where the text came from. `canned`, `ai_suggestion`, or empty for
  something the agent wrote. Empty is the default and means *unknown*, not
  *free-typed*: rows written before this column existed are genuinely unknown,
  and counting them as free-typed would understate adoption while counting them
  as adopted would overstate it. Neither is honest, so neither is chosen.
- `canned_reply_id` - which template, when one was used. `canned_replies.
  usage_count` already counts *insertions* (the picker increments it when the
  client asks for the body); this records what was actually **sent**. An agent
  who inserts a template and then edits it produces one insertion and a different
  message, and only this column can tell the difference.

The client supplies `origin`, and that is deliberate: only the client knows what
the agent did. It is telemetry, not authorization - it changes no permission and
gates no action - so an unverifiable value is acceptable here in a way it would
not be for `tenant_id` or an actor.

No FK on `origin` (free text by design, so a new composition mode does not need
a migration), and a nullable FK on `canned_reply_id` because templates are
archived rather than deleted, so the reference stays valid.
"""

import sqlalchemy as sa
from alembic import op

revision = "0051_turn_origin"
down_revision = "0050_conversation_channel_key"
branch_labels = None
depends_on = None

TABLE = "conversation_turns"


def upgrade() -> None:
    op.add_column(
        TABLE,
        sa.Column("origin", sa.String(31), nullable=False, server_default=""),
    )
    op.add_column(
        TABLE,
        sa.Column(
            "canned_reply_id",
            sa.Uuid(),
            sa.ForeignKey("canned_replies.id", name="fk_turn_canned_reply"),
            nullable=True,
        ),
    )
    op.add_column(
        TABLE,
        sa.Column("author_ref", sa.String(255), nullable=True),
    )
    # Adoption is read as "of the replies in this window, how many came from the
    # suggestion" - a filter on origin within a tenant and time range. The
    # author is on the row for the same reason: the lease holds *current*
    # ownership, so attributing past replies to whoever holds it now would make
    # every per-agent number change when a conversation is reassigned.
    op.create_index("ix_conversation_turns_origin", TABLE, ["tenant_id", "origin"])


def downgrade() -> None:
    op.drop_index("ix_conversation_turns_origin", table_name=TABLE)
    op.drop_column(TABLE, "author_ref")
    op.drop_column(TABLE, "canned_reply_id")
    op.drop_column(TABLE, "origin")
