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
    metadata_json: Mapped[dict] = mapped_column(
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
    minimized_payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    status: Mapped[InboxEventStatus] = mapped_column(
        String(31), nullable=False, default=InboxEventStatus.RECEIVED.value
    )
    received_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
    processed_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    conversation_ref_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("external_resource_refs.id"), nullable=True
    )
