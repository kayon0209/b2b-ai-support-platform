"""ORM models for conversation tasks, their events, and copilot drafts.

Mirrors migration 0055 exactly. Two things are worth stating because they look
like omissions and are not:

- **No slot values on `ConversationTask`.** `slots` holds names, origins and
  confirmation flags. The value of a delivery address or a tax id is business
  data with a narrower audience than a task row, and the audit reader reaches
  this table. Values live in the draft/gateway path, which has its own
  authorization.

- **No `succeeded_at` written by the model path.** `completion_evidence`
  records *what* proved completion - a verified tool receipt id, or a recorded
  human action - and the store refuses a terminal transition without one. An
  LLM asserting that a task is done cannot produce that value.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import (
    BigInteger,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from platform_core.agent_runtime.tasks.state_machine import TaskKind, TaskStatus
from platform_core.orm_base import Base, PkMixin, TenantMixin


class SemanticAssessmentRow(Base, PkMixin, TenantMixin):
    """One classification attempt, in any mode. The SHD-01 comparison record."""

    __tablename__ = "semantic_assessments"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "id", "conversation_ref_id", name="uq_semantic_assessments_tenant_ref"
        ),
        Index(
            "ix_semantic_assessments_conversation",
            "tenant_id",
            "conversation_ref_id",
            "created_at",
        ),
        Index("ix_semantic_assessments_mode", "tenant_id", "mode"),
    )

    conversation_ref_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    turn_id: Mapped[str] = mapped_column(String(64), nullable=False)
    mode: Mapped[str] = mapped_column(String(31), nullable=False)
    rule_route: Mapped[str] = mapped_column(String(31), nullable=False, server_default="")
    rule_action: Mapped[str] = mapped_column(String(31), nullable=False, server_default="")
    model_primary_intent: Mapped[str | None] = mapped_column(String(31), nullable=True)
    agreement: Mapped[str] = mapped_column(String(31), nullable=False, server_default="")
    effective_decision: Mapped[str] = mapped_column(String(63), nullable=False, server_default="")
    reason_codes: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    validation_status: Mapped[str] = mapped_column(String(31), nullable=False, server_default="")
    prompt_version: Mapped[str] = mapped_column(String(63), nullable=False, server_default="")
    model_name: Mapped[str] = mapped_column(String(127), nullable=False, server_default="")
    latency_ms: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    prompt_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    completion_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    truncated: Mapped[bool] = mapped_column(nullable=False, server_default="0")
    # Labels, evidence positions, slot names/origins, counts. Never a message
    # body and never a slot value. Built by `SemanticAssessment.as_snapshot`.
    snapshot: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    created_at: Mapped[int] = mapped_column(BigInteger, nullable=False)


class ConversationTask(Base, PkMixin, TenantMixin):
    """One need the customer expressed, tracked to a real outcome."""

    __tablename__ = "conversation_tasks"
    __table_args__ = (
        # The idempotency boundary. A re-delivered turn collides here.
        UniqueConstraint(
            "tenant_id",
            "conversation_ref_id",
            "source_turn_id",
            "task_local_key",
            name="uq_conversation_task_identity",
        ),
        UniqueConstraint(
            "tenant_id", "id", "conversation_ref_id", name="uq_conversation_tasks_tenant_ref"
        ),
        Index(
            "ix_conversation_tasks_status",
            "tenant_id",
            "conversation_ref_id",
            "status",
            "sequence",
        ),
        Index("ix_conversation_tasks_actionable", "tenant_id", "status", "updated_at"),
    )

    conversation_ref_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    source_turn_id: Mapped[str] = mapped_column(String(64), nullable=False)
    # Nullable: a deterministic rule path can create a task with no model
    # involved, and requiring an assessment row would mean inventing one.
    assessment_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    # Server-generated. The model proposes `source_turn_id`; it never chooses
    # the key that makes retries idempotent.
    task_local_key: Mapped[str] = mapped_column(String(63), nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    kind: Mapped[str] = mapped_column(String(31), nullable=False)
    status: Mapped[str] = mapped_column(String(31), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    # Bumped when the action's arguments change, so a confirmation bound to the
    # old revision stops matching.
    action_revision: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    # Hash of the task's meaningful content. Same key + different hash is a
    # conflict to surface, not a duplicate to drop.
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    depends_on: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    condition: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    # Slot NAMES and origins only. See the module docstring.
    slots: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    missing_slots: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    # Why the task is blocked. Shown verbatim to the agent.
    blocked_reason: Mapped[str | None] = mapped_column(String(63), nullable=True)
    proposal_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    execution_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    # What proved completion. Required before a terminal transition.
    completion_evidence: Mapped[str | None] = mapped_column(String(127), nullable=True)
    created_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
    updated_at: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")

    @property
    def status_enum(self) -> TaskStatus:
        return TaskStatus(self.status)

    @property
    def kind_enum(self) -> TaskKind:
        return TaskKind(self.kind)


class ConversationTaskEvent(Base, PkMixin, TenantMixin):
    """Append-only. No update path exists in the application."""

    __tablename__ = "conversation_task_events"
    __table_args__ = (
        UniqueConstraint("tenant_id", "task_id", "sequence", name="uq_conversation_task_event_seq"),
        Index("ix_conversation_task_events_task", "tenant_id", "task_id", "sequence"),
        # Composite, tenancy-carrying. A two-column (tenant_id, task_id) FK
        # cannot be created unless (tenant_id, id) is unique on the parent,
        # which it is not declared to be.
        ForeignKeyConstraint(
            ["tenant_id", "task_id", "conversation_ref_id"],
            [
                "conversation_tasks.tenant_id",
                "conversation_tasks.id",
                "conversation_tasks.conversation_ref_id",
            ],
            name="fk_task_events_task_tenant",
            ondelete="CASCADE",
        ),
    )

    task_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    conversation_ref_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    from_status: Mapped[str | None] = mapped_column(String(31), nullable=True)
    to_status: Mapped[str] = mapped_column(String(31), nullable=False)
    actor_type: Mapped[str] = mapped_column(String(31), nullable=False, server_default="system")
    actor_ref: Mapped[str | None] = mapped_column(String(63), nullable=True)
    reason_code: Mapped[str] = mapped_column(String(63), nullable=False, server_default="")
    trace_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    from_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    to_version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[int] = mapped_column(BigInteger, nullable=False)


class CopilotDraft(Base, PkMixin, TenantMixin):
    """Controlled business data. Never logged, never in a metric label."""

    __tablename__ = "copilot_drafts"
    __table_args__ = (
        # One draft per job: a replayed generation returns this row instead of
        # paying for a second model call.
        UniqueConstraint("tenant_id", "job_id", name="uq_copilot_draft_job"),
        Index("ix_copilot_drafts_conversation", "tenant_id", "conversation_ref_id", "status"),
        ForeignKeyConstraint(
            ["tenant_id", "task_id", "conversation_ref_id"],
            [
                "conversation_tasks.tenant_id",
                "conversation_tasks.id",
                "conversation_tasks.conversation_ref_id",
            ],
            name="fk_copilot_drafts_task_tenant",
            ondelete="CASCADE",
        ),
    )

    conversation_ref_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    actor_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    job_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    # Nullable: a summary job is not attached to a task.
    task_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    kind: Mapped[str] = mapped_column(String(31), nullable=False)
    status: Mapped[str] = mapped_column(String(31), nullable=False)
    # The timeline revision the generation was based on. A new customer message
    # bumps it and the job goes stale rather than overwriting an edit.
    timeline_revision: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    lease_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    source_refs: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    body: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    edited_by_human: Mapped[bool] = mapped_column(nullable=False, server_default="0")
    error_code: Mapped[str | None] = mapped_column(String(63), nullable=True)
    created_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
    updated_at: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")


__all__ = [
    "ConversationTask",
    "ConversationTaskEvent",
    "CopilotDraft",
    "SemanticAssessmentRow",
]
