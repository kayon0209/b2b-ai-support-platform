"""ExternalResourceRef and InboxEvent models.

ExternalResourceRef (docs/domain-model.md): the custom platform references
Chatwoot entities through mappings and never duplicates them as authoritative
records. UNIQUE(tenant_id, system, resource_type, external_id).

InboxEvent: raw webhook deliveries persisted before any work is enqueued
(docs/api-contracts.md processing rules). Payload is minimized: we store a
hash plus redacted metadata, never the full customer payload unencrypted.
"""

import enum
import uuid
from typing import Any

from sqlalchemy import BigInteger, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from platform_core.orm_base import Base, PkMixin, TenantMixin


class ExternalResourceRef(Base, PkMixin, TenantMixin):
    __tablename__ = "external_resource_refs"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "system",
            "resource_type",
            "external_id",
            name="uq_external_ref",
        ),
    )

    system: Mapped[str] = mapped_column(String(31), nullable=False, default="chatwoot")
    resource_type: Mapped[str] = mapped_column(String(63), nullable=False, index=True)
    external_id: Mapped[str] = mapped_column(String(255), nullable=False)
    external_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_version: Mapped[str | None] = mapped_column(String(63), nullable=True)
    last_synced_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, default=dict, server_default="{}"
    )


class InboxEventStatus(enum.StrEnum):
    RECEIVED = "received"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    DUPLICATE = "duplicate"


class InboxEvent(Base, PkMixin, TenantMixin):
    """Transactional inbox row. Written in the same transaction as the
    enqueue decision; consumers deduplicate by delivery_id."""

    __tablename__ = "inbox_events"
    __table_args__ = (
        UniqueConstraint("tenant_id", "delivery_id", name="uq_inbox_delivery"),
        Index("ix_inbox_events_status", "status"),
    )

    delivery_id: Mapped[str] = mapped_column(String(255), nullable=False)
    event_type: Mapped[str] = mapped_column(String(127), nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(127), nullable=False)
    # Minimized payload: IDs, timestamps, content hash — no raw message body.
    minimized_payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    status: Mapped[InboxEventStatus] = mapped_column(
        String(31), nullable=False, default=InboxEventStatus.RECEIVED.value
    )
    received_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # Claim bookkeeping. `received_at` is when the row arrived and is the FIFO
    # key; these three are when a worker took it and when it last proved it was
    # still alive. Reclaiming on `received_at` instead of these turns a backlog
    # into duplicate work - see migration 0056.
    claimed_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    heartbeat_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    worker_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    processed_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    conversation_ref_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("external_resource_refs.id"), nullable=True
    )


class VisitorSessionRevocation(Base, PkMixin, TenantMixin):
    """A visitor token that has been withdrawn (migration 0057).

    The key is the token's own jti, not the conversation. Revoking the
    conversation was tried first and is wrong: `POST /v1/support/sessions`
    mints a fresh token for the same conversation, so a customer who ended a
    session and came back could never return. Ending a session means "stop
    holding this credential"; only deletion should make returning impossible,
    and this platform does not offer deletion here.

    The revocation list itself is read on every authenticated customer request,
    so the table is a primary-key lookup and nothing more - and `visitor_token`
    stays a pure function that needs no database.

    Declared here so the schema test's "every table is known to the ORM" holds;
    the write and read paths are in `visitor_revocation` as explicit SQL, since
    they run in the caller's transaction rather than through a session of their
    own.
    """

    __tablename__ = "visitor_session_revocations"
    __table_args__ = (
        # Plain unique, not partial: `ON CONFLICT (tenant_id, token_jti)` needs
        # a constraint it can infer. Postgres permits many NULLs in a unique
        # column, so rows without a jti do not collide.
        UniqueConstraint("tenant_id", "token_jti", name="uq_visitor_revocation_tenant_jti"),
        Index("ix_visitor_revocation_revoked_at", "revoked_at"),
    )

    # Which credential was withdrawn. Nullable for the operator-closed case,
    # which records a conversation without naming a token.
    token_jti: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    # Not a foreign key: the ref is derived from (tenant, external id) by the
    # channel adapter, so it is not necessarily a row in any single table.
    # Kept for the audit trail - "who ended what" cannot be answered by a jti.
    conversation_ref: Mapped[uuid.UUID] = mapped_column(nullable=False)
    revoked_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # `visitor` today; `operator` exists so support can close a chat from the
    # workbench without another migration.
    ended_by: Mapped[str] = mapped_column(
        String(32), nullable=False, default="visitor", server_default="visitor"
    )
