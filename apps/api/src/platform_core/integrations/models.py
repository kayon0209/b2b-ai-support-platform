"""Integration Hub models (ticket 27, docs/integrations.md).

- Connector: per-tenant external system registration. Credentials are a
  secret-manager reference, never inline values.
- SyncCursor: incremental, resumable sync positions per resource type.
- DeadLetterItem: exhausted retries become visible records, not silence.
"""

import enum
import uuid
from typing import Any

from sqlalchemy import BigInteger, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from platform_core.orm_base import Base, PkMixin, TenantMixin


class ConnectorStatus(enum.StrEnum):
    ACTIVE = "active"
    DEGRADED = "degraded"
    NEEDS_REAUTH = "needs_reauth"
    DISABLED = "disabled"


class Connector(Base, PkMixin, TenantMixin):
    __tablename__ = "connectors"
    __table_args__ = (
        UniqueConstraint("tenant_id", "provider", "name", name="uq_connector_per_tenant"),
    )

    provider: Mapped[str] = mapped_column(String(63), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(31), nullable=False, default="active")
    # feature capabilities claimed by the adapter: read_account, create_issue...
    capabilities: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    configuration: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    # Secret manager reference (e.g. "vault://kv/crm/acme"), never credentials.
    credential_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Inbound webhook signing secret, a *different* secret from credential_ref:
    # the provider signs with this one and the two rotate independently, so
    # overloading one column would mean rotating either breaks the other.
    webhook_secret_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Webhook endpoint signing secret reference for inbound deliveries.
    last_health_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)


class SyncCursor(Base, PkMixin, TenantMixin):
    __tablename__ = "sync_cursors"
    __table_args__ = (
        UniqueConstraint("connector_id", "resource_type", name="uq_cursor_per_resource"),
    )

    connector_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("connectors.id"), nullable=False, index=True
    )
    resource_type: Mapped[str] = mapped_column(String(63), nullable=False)
    cursor: Mapped[str | None] = mapped_column(Text, nullable=True)
    watermark: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    updated_at: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)


class DeadLetterItem(Base, PkMixin, TenantMixin):
    __tablename__ = "dead_letter_items"
    __table_args__ = (Index("ix_dead_letter_connector", "connector_id"),)

    connector_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("connectors.id"), nullable=True
    )
    resource_type: Mapped[str] = mapped_column(String(63), nullable=False)
    # What the failure is about (migration 0061). Not a foreign key: the column
    # already points at several kinds of thing by string, and a foreign key
    # would have to name one. A dangling id is a dead letter about something
    # since deleted, which is harmless and better than blocking that delete.
    # NULL for connector rows written before this - those are identified by
    # `operation_digest`, which is a different and equally deliberate answer.
    resource_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True, index=True)
    operation: Mapped[str] = mapped_column(String(63), nullable=False)
    # Redacted operation summary; raw payloads never stored here.
    operation_digest: Mapped[str] = mapped_column(String(127), nullable=False)
    error_code: Mapped[str] = mapped_column(String(63), nullable=False)
    error_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempts: Mapped[int] = mapped_column(nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(31), nullable=False, default="pending")
    created_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
    resolved_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
