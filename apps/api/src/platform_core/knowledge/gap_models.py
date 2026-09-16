"""Knowledge gap queue and reviewed draft workflow
(ticket 39, docs/development-plan.md Phase 4).

    "Knowledge gap queue and reviewed knowledge-draft workflow."

When the agent abstains because retrieval found nothing, that question is
*evidence about the knowledge base*, not just a failed answer. The same
question asked repeatedly is the strongest available signal of what to
document next. Today that signal is thrown away - the customer gets a
handoff and the gap disappears.

Two hard rules shape the design:

1. **Nothing publishes automatically.** A gap produces a *draft* that a
   human reviews. The AI can propose "this should be documented" but cannot
   create live knowledge, because an unreviewed auto-ingested answer is a
   self-reinforcing loop: the agent's own guess becomes the source it cites
   next time. `AGENTS.md` prohibits "automatically learning from
   unreviewed conversations".

2. **A gap is aggregated, not spammed.** The same question from 50
   customers is one gap with a frequency of 50, so the queue reflects
   demand rather than traffic. Questions are normalised before matching.

Status flow:

    open -> acknowledged -> drafted -> resolved
                          \\-> dismissed

`resolved` means knowledge was published for it. `dismissed` means a human
decided it should not be documented (out of scope, one-off, or the answer
exists and retrieval simply failed - a different bug).
"""

import enum
import hashlib
import re
import uuid

from sqlalchemy import BigInteger, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from platform_core.orm_base import Base, PkMixin, TenantMixin


class GapStatus(enum.StrEnum):
    OPEN = "open"
    ACKNOWLEDGED = "acknowledged"
    DRAFTED = "drafted"
    RESOLVED = "resolved"
    DISMISSED = "dismissed"


class DraftStatus(enum.StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class KnowledgeGap(Base, PkMixin, TenantMixin):
    """One documented knowledge gap, aggregated across occurrences.

    `question_hash` is a normalised fingerprint used for deduplication: two
    customers asking "How do I reset my password?" and "how do i reset my
    password" must land on the same row. The raw question is kept once, as
    `sample_question`, and is subject to PII minimisation like any other
    customer content.
    """

    __tablename__ = "knowledge_gaps"
    __table_args__ = (
        # One open record per normalised question per tenant.
        UniqueConstraint("tenant_id", "question_hash", name="uq_gap_question"),
        Index("ix_gaps_status_frequency", "status", "frequency"),
    )

    question_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    # A representative occurrence. Never the full conversation transcript.
    sample_question: Mapped[str] = mapped_column(Text, nullable=False)
    # Why the agent could not answer: NO_AUTHORIZED_EVIDENCE,
    # EVIDENCE_BELOW_THRESHOLD, CONFLICTING_SOURCES, ...
    reason_code: Mapped[str] = mapped_column(String(63), nullable=False, default="")
    status: Mapped[str] = mapped_column(String(31), nullable=False, default=GapStatus.OPEN.value)
    # How many times this gap was observed. The queue is ordered by this.
    frequency: Mapped[int] = mapped_column(nullable=False, default=1)
    first_seen_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
    last_seen_at: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    # Set when a human marks it as being worked on.
    acknowledged_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # The space a resolution should land in, chosen by the reviewer.
    target_space_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("knowledge_spaces.id"), nullable=True
    )


class KnowledgeDraft(Base, PkMixin, TenantMixin):
    """A proposed answer awaiting human review. Never live knowledge.

    Publishing creates a real `Document` + `DocumentVersion` through the
    normal ingestion path; this row only records the proposal and its
    review decision, so the audit trail explains where a document came from.
    """

    __tablename__ = "knowledge_drafts"
    __table_args__ = (Index("ix_drafts_status", "status"),)

    gap_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("knowledge_gaps.id"), nullable=False, index=True
    )
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(31), nullable=False, default=DraftStatus.PENDING)
    author_kind: Mapped[str] = mapped_column(String(31), nullable=False, default="human")
    reviewed_by: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    reviewed_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    review_notes: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # Set once approved AND published, pointing at the created document.
    published_document_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("documents.id"), nullable=True
    )


_WHITESPACE = re.compile(r"\s+")
# Terminal punctuation and leading question words vary between askers and
# carry no meaning for deduplication. "There" is included so "Hi there, ..."
# collapses the same way "Hi, ..." does - one greeting word is not enough.
_LEADING_NOISE = re.compile(
    r"^(?:(?:hi|hello|hey|please|pls|thanks|thank you|good morning|good afternoon|there)\b[\s,]*)+",
    re.IGNORECASE,
)
_TRAILING_PUNCT = re.compile(r"[?!.]+$")


def normalize_question(text: str) -> str:
    """Fold a question to its deduplication key.

    Deliberately conservative: case, whitespace, greeting prefixes and
    trailing punctuation are removed, nothing else. Aggressive stemming
    would merge "can I export data" with "can I import data" - two gaps
    that need different documents.
    """
    collapsed = _WHITESPACE.sub(" ", text.strip())
    collapsed = _LEADING_NOISE.sub("", collapsed)
    collapsed = _TRAILING_PUNCT.sub("", collapsed)
    # Lowercase only after the noise strip, so an all-caps greeting still
    # matches its lowercase form.
    return collapsed.lower().strip()


def question_hash(text: str) -> str:
    """Stable fingerprint of the normalised question.

    Returns "" when nothing meaningful survives normalisation. `record_gap`
    treats a falsy hash as "nothing to record", so a question made only of
    greetings and punctuation cannot create a queue row that no reviewer
    could act on - and, more importantly, cannot all collide onto one row
    and manufacture a fake frequency signal.
    """
    normalized = normalize_question(text)
    if not normalized:
        return ""
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


# Reasons that indicate a *knowledge* gap rather than a request the platform
# should refuse. A restricted request is not a gap: documenting it would be
# the wrong fix, and queuing it would train reviewers to ignore the queue.
GAP_REASON_CODES: frozenset[str] = frozenset(
    {
        "NO_AUTHORIZED_EVIDENCE",
        "EVIDENCE_BELOW_THRESHOLD",
        "CONFLICTING_SOURCES",
    }
)


def is_knowledge_gap(reason_code: str) -> bool:
    """Whether an abstention reason should open a gap.

    CONFLICTING_SOURCES counts: contradictory documentation is a knowledge
    defect, and the fix is for a human to decide which source is correct.
    """
    return reason_code in GAP_REASON_CODES
