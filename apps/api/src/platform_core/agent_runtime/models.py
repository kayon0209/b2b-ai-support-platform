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
    output_hash: Mapped[str | None] = mapped_column(String(127), nullable=True)
    token_usage: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    latency_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    abstain_reason: Mapped[str | None] = mapped_column(String(127), nullable=True)


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


class ConversationTurn(Base, PkMixin, TenantMixin):
    """One redacted turn of one conversation (iteration plan 2.1).

    The raw message lives only in Chatwoot (docs/security.md). What is
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
    # Source of the turn: "chatwoot" (fetched live) or "platform" (our own
    # reply). Local rows win when merging with a live fetch.
    source: Mapped[str] = mapped_column(String(15), nullable=False, default="platform")
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
