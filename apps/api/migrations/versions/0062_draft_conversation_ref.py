"""0062: tie a knowledge draft to the conversation it answers.

The gap
-------
`knowledge_drafts` recorded a title, a body, and the gap it came from - and
nothing else. So a draft written to answer a specific customer's question could
not be read next to that question. An operator reviewing a draft had the prose
and no customer: not the wording that prompted it, not the conversation it would
have answered, and no way to check that the answer actually fits the case it was
written for.

Why the column is on the draft and not on the gap
-------------------------------------------------
A gap is *aggregated*. `knowledge_gaps` holds a `question_hash`, a
`sample_question` and a `frequency` precisely because one gap represents many
conversations. Putting a single conversation on the gap would assert that it had
one origin, which is false by the table's own design, and would make the column
wrong for every gap that recurred.

The draft is the right home for a different reason: a draft is written *now*, by
somebody, in response to something. "Which conversation was this written for" is
a question about the draft, and it has one answer.

Nullable, and deliberately so
-----------------------------
Not every draft has a conversation. One written from the gap queue has no
customer in front of the operator; a migration, or a script seeding the corpus,
has none either. NULL means "not written against a specific conversation", which
is a real state rather than missing data.

No foreign key
--------------
`conversation_refs` is referenced by id from several tables already without a
constraint, and a dangling id is a draft whose conversation was later deleted -
harmless, and better than blocking that deletion. A draft is a record of what
somebody wrote; it should outlive the conversation that prompted it.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0062_draft_conversation_ref"
down_revision: str | None = "0061_dead_letter_resource_id"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE knowledge_drafts ADD COLUMN conversation_ref_id uuid NULL")
    # The reviewer's query is "show me the drafts written for this conversation",
    # so the index is on the column alone - every such lookup is already scoped
    # to a tenant by RLS.
    op.execute(
        "CREATE INDEX ix_knowledge_drafts_conversation "
        "ON knowledge_drafts (conversation_ref_id) "
        "WHERE conversation_ref_id IS NOT NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_knowledge_drafts_conversation")
    op.execute("ALTER TABLE knowledge_drafts DROP COLUMN IF EXISTS conversation_ref_id")
