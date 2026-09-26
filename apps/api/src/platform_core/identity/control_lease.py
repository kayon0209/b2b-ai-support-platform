"""Conversation control lease (ticket 9).

Invariants (docs/agent.md, docs/domain-model.md):
- Exactly one owner (ai|human|queue) per conversation at a time.
- Human ownership overrides AI immediately.
- Every customer-visible AI send performs compare-and-set on lease_version
  IMMEDIATELY before dispatch; a version mismatch aborts the send.
"""

import enum
import uuid

from sqlalchemy import BigInteger, Index, String, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from platform_core.orm_base import Base, PkMixin, TenantMixin


class LeaseOwner(enum.StrEnum):
    AI = "ai"
    HUMAN = "human"
    QUEUE = "queue"
    CLOSED = "closed"


class ControlLeaseError(Exception):
    pass


class LeaseConflict(ControlLeaseError):
    """CAS failed: lease moved to another owner or version advanced."""


class ConversationControlLease(Base, PkMixin, TenantMixin):
    __tablename__ = "conversation_control_leases"
    __table_args__ = (
        UniqueConstraint("tenant_id", "conversation_ref_id", name="uq_lease_per_conversation"),
        Index("ix_lease_owner", "owner_type"),
        # Conversation queue polling is the highest-volume read in the human
        # workbench. Partial indexes keep exact queue counts and ordered pages
        # bounded to the matching ownership state instead of scanning every
        # lease for the tenant on each poll.
        Index(
            "ix_workbench_queue_order",
            "tenant_id",
            "updated_at",
            "id",
            postgresql_where=text("owner_type = 'queue'"),
        ),
        Index(
            "ix_workbench_human_owner_order",
            "tenant_id",
            "owner_ref",
            "updated_at",
            "id",
            postgresql_where=text("owner_type = 'human'"),
        ),
        Index(
            "ix_workbench_human_waiting_count",
            "tenant_id",
            "owner_ref",
            postgresql_where=text("owner_type = 'human' AND mode = 'HUMAN_WAITING_CUSTOMER'"),
        ),
    )

    conversation_ref_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    owner_type: Mapped[str] = mapped_column(String(15), nullable=False)
    owner_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    mode: Mapped[str] = mapped_column(String(63), nullable=False, default="AI_ACTIVE")
    lease_version: Mapped[int] = mapped_column(nullable=False, default=1)
    expires_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    changed_reason: Mapped[str] = mapped_column(String(255), nullable=False, default="created")
    updated_at: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
