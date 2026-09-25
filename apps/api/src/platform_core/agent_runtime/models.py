"""Agent runtime models (ticket 16, docs/domain-model.md).

AgentRun records every model invocation with full version lineage (prompt,
model, retrieval config, policy bundle, code release) so any answer can be
reproduced and audited. Citation rows tie claims to immutable document
versions that were present in the runtime context.
"""

import enum
import uuid
from typing import Any

from sqlalchemy import BigInteger, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from platform_core.orm_base import Base, PkMixin, TenantMixin


class RunStatus(enum.StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    ABSTAINED = "abstained"
    HANDED_OFF = "handed_off"
    FAILED = "failed"
    # Accepted, never executed, and no longer waiting: the retention sweep
    # closes placeholders that outlived any plausible queue latency. Without
    # it `queued` means two different things - "a worker will get to this in a
    # second" and "this was abandoned three days ago" - and both the quota
    # counter and the replay list have to guess which. See
    # `agent_runtime/abandoned.py`.
    ABANDONED = "abandoned"
    # Accepted, never executed, and nobody is on it. Distinct from
    # HANDED_OFF, which is a claim about a *person*: the conversation left the
    # AI and went to the human queue, so until somebody claims it, no one has
    # it. Recording these as HANDED_OFF made eighteen unanswered questions look
    # like eighteen answered-by-a-colleague ones in every list, replay and
    # metric that reads the status. Measured on a live stack: 20 messages in,
    # 3 replies out, 18 runs reported as handed to a human.
    SUPERSEDED = "superseded"


class Route(enum.StrEnum):
    KNOWLEDGE_QA = "knowledge_qa"
    CASE_STATUS = "case_status"
    BUSINESS_READ = "business_read"
    BUSINESS_WRITE = "business_write"
    SENSITIVE = "sensitive"
    OUT_OF_SCOPE = "out_of_scope"
    HUMAN_REQUIRED = "human_required"


class PromptTemplate(Base, PkMixin, TenantMixin):
    """Immutable prompt lineage: publishing a change creates a new version."""

    __tablename__ = "prompt_versions"
    __table_args__ = (
        UniqueConstraint("tenant_id", "template_name", "version", name="uq_prompt_version"),
    )

    template_name: Mapped[str] = mapped_column(String(127), nullable=False, index=True)
    version: Mapped[int] = mapped_column(nullable=False)
    # Template body contains no customer data; safe to store.
    body: Mapped[str] = mapped_column(Text, nullable=False)
    published: Mapped[bool] = mapped_column(nullable=False, default=False)


class AgentRun(Base, PkMixin, TenantMixin):
    __tablename__ = "agent_runs"
    __table_args__ = (Index("ix_agent_runs_status", "status"),)

    conversation_ref_id: Mapped[uuid.UUID] = mapped_column(nullable=False, index=True)
    case_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    route: Mapped[str] = mapped_column(String(31), nullable=False)
    status: Mapped[str] = mapped_column(String(31), nullable=False, default="queued")
    # When the run was enqueued, in epoch seconds (UTC). Named to match
    # AuditEvent.occurred_at rather than `created_at`: AgentRun records an
    # event, and the quality dashboard windows over this column. It is
    # nullable because rows written before migration 0012 have no value;
    # the aggregator skips them rather than guessing a timestamp.
    started_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    prompt_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("prompt_versions.id"), nullable=True
    )
    model_config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    retrieval_config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    policy_version: Mapped[str] = mapped_column(String(63), nullable=False, default="v1")
    code_version: Mapped[str] = mapped_column(String(63), nullable=False, default="dev")
    trace_id: Mapped[str] = mapped_column(String(63), nullable=False, default="")
    input_hash: Mapped[str] = mapped_column(String(127), nullable=False, default="")
    # Number of terminal claims taken on this run (migration 0060). Bumped by
    # `agent_runtime/terminal.claim_terminal` inside the same UPDATE that checks
    # the status, so it is the receipt for a compare-and-set - and a run whose
    # version moved without the status becoming terminal is one a worker claimed
    # and then did not finish. See that module for why the claim has to be
    # taken before the reply is dispatched.
    version: Mapped[int] = mapped_column(nullable=False, default=0, server_default="0")
    output_hash: Mapped[str | None] = mapped_column(String(127), nullable=True)
    token_usage: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    latency_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    abstain_reason: Mapped[str | None] = mapped_column(String(127), nullable=True)


def run_executed() -> Any:
    """Criterion selecting runs that actually executed.

    The queue endpoint writes a `queued` row so the caller gets an id, and
    `orchestrator._answer_run` adopts it and fills in the question's hash the
    moment execution begins. A row that is still empty therefore means the
    placeholder was never adopted: accepted, never run. The orchestrator's own
    docstring calls that an honest state, and it is - it just is not *usage*.

    Aggregating those rows does more damage than a wrong denominator. The
    placeholder carries the queue's default `route` ("knowledge_qa", set in
    `chat_service.queue_agent_run`) and no intent snapshot at all, so counting
    it reports route traffic that never happened and pours rows that never ran
    into the intent distribution's `unrecorded` bucket - the one bucket that is
    supposed to mean "this run predates the field".

    Measured on 2026-09-22 against the dev database, one tenant, one month:
    usage read 76 runs against 42 that executed.

    Defined once, here, because the same predicate is needed by usage
    accounting and by both quality aggregations; three hand-written copies is
    how they drift apart again.
    """
    return AgentRun.input_hash != ""


class Citation(Base, PkMixin, TenantMixin):
    __tablename__ = "citations"
    __table_args__ = (
        # One citation per claim per run
        UniqueConstraint("agent_run_id", "claim_index", name="uq_citation_claim"),
    )

    agent_run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("agent_runs.id"), nullable=False, index=True
    )
    # Nullable since 0036: a tool-sourced citation has no document version -
    # its provenance is source_uri ("tool://...") plus excerpt_hash.
    document_version_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    chunk_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    excerpt_hash: Mapped[str] = mapped_column(String(127), nullable=False)
    source_uri: Mapped[str] = mapped_column(Text, nullable=False)
    claim_index: Mapped[int] = mapped_column(nullable=False, default=0)
    retrieval_score: Mapped[float] = mapped_column(nullable=False, default=0.0)


# What a run is for, as stored on `AgentRun.model_config["mode"]`.
#
# `internal_draft` means "produce the answer, send nothing" - the operator is
# looking at what the platform *would* say. It shares the suppression mechanism
# with shadow mode (feature 9.2) because the two differ only in who asked: a
# flag asks for every conversation, a mode asks for this one.
MODE_CUSTOMER_REPLY = "customer_reply"
MODE_INTERNAL_DRAFT = "internal_draft"
VALID_MODES: frozenset[str] = frozenset({MODE_CUSTOMER_REPLY, MODE_INTERNAL_DRAFT})


# How an agent's reply was composed, as stored on `ConversationTurn.origin`.
#
# `ORIGIN_UNKNOWN` (empty) and `ORIGIN_FREE` are **different facts** and must not
# share a value:
#
# - unknown  - the client did not say. Every row written before migration 0051
#              says this, and so does any client that does not report provenance.
# - free     - the agent typed it themselves, and the client said so.
#
# Collapsing them makes "nobody reported" indistinguishable from "everybody
# typed", and the only place that difference lands is the adoption denominator -
# so the error always flatters the feature it is measuring.
ORIGIN_UNKNOWN = ""
ORIGIN_FREE = "free"
ORIGIN_CANNED = "canned"
ORIGIN_AI_SUGGESTION = "ai_suggestion"
KNOWN_ORIGINS: frozenset[str] = frozenset(
    {ORIGIN_UNKNOWN, ORIGIN_FREE, ORIGIN_CANNED, ORIGIN_AI_SUGGESTION}
)


class ConversationTurn(Base, PkMixin, TenantMixin):
    """One redacted turn of one conversation (iteration plan 2.1).

    The raw message remains with its source channel (docs/security.md). What is
    persisted here is the REDACTED text: memory must be able to read the
    words ("my plan is annual") to resolve anaphora, so a hash alone will
    not do - but storing the unredacted sentence would create a second
    copy of customer PII that retention then has to track. `text_hash`
    keeps an integrity anchor over the original bytes.
    """

    __tablename__ = "conversation_turns"
    __table_args__ = (UniqueConstraint("id", "tenant_id", name="uq_conversation_turns_id_tenant"),)

    conversation_ref_id: Mapped[uuid.UUID] = mapped_column(nullable=False, index=True)
    # customer|agent|system|tool (conversation.TurnRole values)
    role: Mapped[str] = mapped_column(String(15), nullable=False)
    text_redacted: Mapped[str] = mapped_column(Text, nullable=False)
    text_hash: Mapped[str] = mapped_column(String(127), nullable=False)
    ts: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    # Correlation marker: "clarify:<reason>" for clarification notices, tool
    # references for tool turns. Read by the clarify-streak guard.
    ref: Mapped[str] = mapped_column(String(127), nullable=False, default="")
    # Source of the turn: "agent" for a human reply or "platform" for a
    # platform-authored turn. Older external-source values remain readable.
    source: Mapped[str] = mapped_column(String(15), nullable=False, default="platform")
    # How the text was composed. Empty means *unknown*, not *free-typed* - see
    # migration 0051 for why that distinction is kept rather than resolved.
    origin: Mapped[str] = mapped_column(String(31), nullable=False, default="")
    # Which template, when one was used. `canned_replies.usage_count` counts
    # insertions; this records what was actually sent.
    canned_reply_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("canned_replies.id"), nullable=True
    )
    # Who wrote it, when the platform authored the turn. NULL for customer turns.
    # The lease holds current ownership, so it cannot answer "who said this" for
    # a conversation that has since changed hands.
    author_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")


class ContactFact(Base, PkMixin, TenantMixin):
    """One durable fact about a contact, across conversations (plan 2.5).

    Current statements override history (ON CONFLICT ... DO UPDATE): the
    customer correcting themselves is the normal case, and "latest wins" is
    the only conflict rule that needs no arbitration. Only facts extracted
    from CUSTOMER turns are ever written here - an assistant's own claim
    must not become its memory (model self-feedback).
    """

    __tablename__ = "contact_facts"
    __table_args__ = (
        UniqueConstraint("tenant_id", "contact_ref", "key", name="uq_contact_fact_key"),
    )

    contact_ref: Mapped[uuid.UUID] = mapped_column(nullable=False, index=True)
    key: Mapped[str] = mapped_column(String(63), nullable=False)
    value: Mapped[str] = mapped_column(String(255), nullable=False)
    source_turn_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    confidence: Mapped[int] = mapped_column(nullable=False, default=100)  # percent
    updated_at: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    expires_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
