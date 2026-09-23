"""Canned replies: reusable agent replies with a shortcut.

Every mainstream service desk has these, and it was the single most ordinary
thing this platform did not have. The gap matters more than the size suggests:
an agent who has to retype "标准交期 7 天，加急 3 天，最终以报价单为准" fifty times a
week will eventually paraphrase it, and fifty paraphrases of a commitment is
exactly how a commercial red line gets said differently every time.

Why it is not knowledge
-----------------------
It looks like knowledge - both are stored text an employee reads - but it is a
different object with different rules:

- A knowledge document is **evidence**: it is retrieved, cited, and a customer
  answer is *built* from it. A canned reply is **authored**: nothing retrieves
  it, an agent chooses it, and nothing cites it.
- Putting these in `knowledge_gaps`/`documents` would make them retrievable, so
  the retrieval path could start citing a reply template as if it were a source.
  That is the self-reinforcing loop `AGENTS.md` prohibits.

So it lives with `cases`, next to the work it supports, and is never indexed.

Two decisions worth stating:
- **`shortcut` is nullable, not a defaulted empty string.** Many replies have no
  shortcut, and UNIQUE treats NULLs as distinct - so "no shortcut" does not
  collide, while `/eta` can only mean one thing. An empty-string default would
  make the second reply without a shortcut fail.
- **`archived`, never deleted.** A reply that was sent to customers is history;
  deleting the row would leave those conversations unexplainable. Archive hides
  it from the picker and keeps the record.
"""

import uuid

from sqlalchemy import BigInteger, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from platform_core.orm_base import Base, PkMixin, TenantMixin

# Bounded so a shortcut cannot become a payload.
MAX_SHORTCUT = 63
MAX_TITLE = 191
MAX_BODY = 8192


class CannedReply(Base, PkMixin, TenantMixin):
    """One reusable reply template."""

    __tablename__ = "canned_replies"
    __table_args__ = (
        # One meaning per shortcut per tenant. NULLs are distinct, so any number
        # of replies may have no shortcut.
        UniqueConstraint("tenant_id", "shortcut", name="uq_canned_shortcut"),
        Index("ix_canned_replies_scope", "tenant_id", "business_line", "team_ref"),
    )

    title: Mapped[str] = mapped_column(String(MAX_TITLE), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    # `/eta` -> this reply. Nullable: see the module docstring.
    shortcut: Mapped[str | None] = mapped_column(String(MAX_SHORTCUT), nullable=True)
    # Scope, both optional. An empty scope is a reply anyone may use; scoping
    # narrows the picker for an agent working a PCB queue so the 200-reply list
    # does not become the reason nobody uses it.
    business_line: Mapped[str] = mapped_column(String(31), nullable=False, default="")
    team_ref: Mapped[str] = mapped_column(String(63), nullable=False, default="")
    # Language. Empty means "any", so a deployment that never localises is not
    # forced to tag every reply.
    locale: Mapped[str] = mapped_column(String(15), nullable=False, default="")
    # How often it was actually used. This is the ordering for the picker and,
    # later, the signal for which replies deserve to be automated.
    usage_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    last_used_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    archived: Mapped[bool] = mapped_column(nullable=False, default=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    created_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
    updated_at: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
