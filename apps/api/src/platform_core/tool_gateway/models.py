"""Tool Gateway models (tickets 29-30, docs/domain-model.md Tools section).

Execution sequence: propose -> authorize -> preview -> confirm -> execute
-> verify -> audit -> summarize.

- ToolDefinition: deny-by-default catalog with JSON Schemas and risk class.
- ToolProposal: freezes tool version, sanitized args, action hash, actor,
  permission decision, confirmation scope, expiry.
- ActionConfirmation: binds actor + tool version + argument hash + expiry.
- ToolExecution: idempotent by idempotency_key; UNKNOWN is a terminal-safe
  state for ambiguous outcomes (never reported as success).
"""

import enum
import uuid

from sqlalchemy import BigInteger, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from platform_core.orm_base import Base, PkMixin, TenantMixin


class ToolRisk(enum.StrEnum):
    READ = "read"
    LOW_WRITE = "low_write"
    CONFIRMED_WRITE = "confirmed_write"
    HUMAN_APPROVAL = "human_approval"
    PROHIBITED = "prohibited"


class ProposalStatus(enum.StrEnum):
    PROPOSED = "proposed"
    AUTHORIZED = "authorized"
    CONFIRMED = "confirmed"
    EXECUTING = "executing"
    EXECUTED = "executed"
    VERIFIED = "verified"
    REJECTED = "rejected"
    EXPIRED = "expired"
    FAILED = "failed"
    UNKNOWN = "unknown"


class ToolDefinition(Base, PkMixin, TenantMixin):
    """tenant_id NULL = platform catalog definition (global reference)."""

    __tablename__ = "tool_definitions"
    __table_args__ = (UniqueConstraint("tenant_id", "name", "version", name="uq_tool_version"),)
    __mapper_args__ = {"polymorphic_identity": "tool_definition"}

    tenant_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)  # type: ignore[assignment]
    name: Mapped[str] = mapped_column(String(127), nullable=False, index=True)
    version: Mapped[int] = mapped_column(nullable=False, default=1)
    risk: Mapped[str] = mapped_column(String(31), nullable=False, default="read")
    input_schema: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    output_schema: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    required_permissions: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    timeout_ms: Mapped[int] = mapped_column(nullable=False, default=10_000)
    idempotent: Mapped[bool] = mapped_column(nullable=False, default=True)
    requires_confirmation: Mapped[bool] = mapped_column(nullable=False, default=False)


class ToolProposal(Base, PkMixin, TenantMixin):
    __tablename__ = "tool_proposals"
    __table_args__ = (Index("ix_tool_proposals_status", "status"),)

    tool_definition_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tool_definitions.id"), nullable=False
    )
    # SHA-256 over (tool_name, tool_version, sanitized_args). Confirmation
    # binds to this hash; any argument change invalidates confirmation.
    action_hash: Mapped[str] = mapped_column(String(127), nullable=False, index=True)
    actor_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    # Sanitized: credentials, tokens, PII stripped before storage.
    sanitized_input: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    sanitized_output: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    status: Mapped[str] = mapped_column(String(31), nullable=False, default="proposed")
    permission_decision: Mapped[str] = mapped_column(String(31), nullable=False, default="pending")
    permission_reason: Mapped[str] = mapped_column(String(63), nullable=False, default="")
    required_confirmation: Mapped[bool] = mapped_column(nullable=False, default=False)
    expires_at: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(63), nullable=True)


class ActionConfirmation(Base, PkMixin, TenantMixin):
    __tablename__ = "action_confirmations"
    __table_args__ = (
        UniqueConstraint("proposal_id", "action_hash", name="uq_confirmation_per_action"),
    )

    proposal_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tool_proposals.id"), nullable=False, index=True
    )
    actor_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    action_hash: Mapped[str] = mapped_column(String(127), nullable=False)
    scope: Mapped[str] = mapped_column(String(63), nullable=False, default="single_execution")
    expires_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
    confirmed_at: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)


class ToolExecution(Base, PkMixin, TenantMixin):
    __tablename__ = "tool_executions"
    __table_args__ = (
        UniqueConstraint("tenant_id", "idempotency_key", name="uq_execution_idempotency"),
        Index("ix_tool_executions_status", "status"),
    )

    proposal_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("tool_proposals.id"), nullable=True
    )
    actor_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    tool_definition_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tool_definitions.id"), nullable=False
    )
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(31), nullable=False, default="executing")
    # executed | verified | failed | unknown
    sanitized_input: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    sanitized_output: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    verification_status: Mapped[str | None] = mapped_column(String(31), nullable=True)
    started_at: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    completed_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(63), nullable=True)
