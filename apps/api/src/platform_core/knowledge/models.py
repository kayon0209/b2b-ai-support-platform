"""Knowledge domain models (ticket 10, docs/domain-model.md).

- DocumentVersion is the immutable retrieval unit: content hash + object
  URI in MinIO with tenant-prefixed keys.
- Ingestion lifecycle is explicit; jobs are idempotent by
  (document_version_id, pipeline_version).
- Chunk rows arrive with the indexing pipeline (ticket 12-13); the table
  is defined here with embedding/search_vector reserved for later.
"""

import enum
import uuid
from typing import Any

from sqlalchemy import (
    BigInteger,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import UserDefinedType

from platform_core.orm_base import Base, PkMixin, TenantMixin


class IngestionStatus(enum.StrEnum):
    UPLOADED = "uploaded"
    PARSING = "parsing"
    CHUNKING = "chunking"
    EMBEDDING = "embedding"
    INDEXING = "indexing"
    READY = "ready"
    FAILED = "failed"
    QUEUED_FOR_RETRY = "queued_for_retry"
    SUPERSEDED = "superseded"
    EXPIRED = "expired"
    DRAFT = "draft"
    ACTIVE = "active"
    PROCESSING = "processing"


class Vector(UserDefinedType[str]):
    """pgvector column placeholder (dim set at index build time)."""

    cache_ok = True

    def get_col_spec(self, **kw: Any) -> str:
        return "vector(1536)"


class KnowledgeSpace(Base, PkMixin, TenantMixin):
    __tablename__ = "knowledge_spaces"

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(31), nullable=False, default="active")
    default_policy_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)


class KnowledgeSource(Base, PkMixin, TenantMixin):
    __tablename__ = "knowledge_sources"
    __table_args__ = (
        UniqueConstraint("tenant_id", "space_id", "name", name="uq_source_per_space"),
    )

    space_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("knowledge_spaces.id"), nullable=False, index=True
    )
    # upload|website|notion|confluence|drive|api
    type: Mapped[str] = mapped_column(String(31), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    # Reference to secret manager entry, never credentials inline.
    config_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    sync_cursor: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(31), nullable=False, default="active")


class Document(Base, PkMixin, TenantMixin):
    __tablename__ = "documents"

    space_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("knowledge_spaces.id"), nullable=False, index=True
    )
    source_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("knowledge_sources.id"), nullable=True
    )
    canonical_uri: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    owner_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # public|internal|confidential|restricted
    classification: Mapped[str] = mapped_column(String(31), nullable=False, default="internal")

    __table_args__ = (UniqueConstraint("tenant_id", "canonical_uri", name="uq_document_uri"),)


class DocumentVersion(Base, PkMixin, TenantMixin):
    __tablename__ = "document_versions"
    __table_args__ = (
        UniqueConstraint("document_id", "version_label", name="uq_version_label"),
        Index("ix_docversion_status_dates", "status", "effective_at", "expires_at"),
    )

    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("documents.id"), nullable=False, index=True
    )
    version_label: Mapped[str] = mapped_column(String(63), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(127), nullable=False)
    effective_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    expires_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # draft|processing|active|superseded|expired|failed
    status: Mapped[str] = mapped_column(String(31), nullable=False, default="draft")
    # MinIO object URI with tenant prefix (docs/security.md object storage)
    object_uri: Mapped[str] = mapped_column(Text, nullable=False)
    parser_version: Mapped[str] = mapped_column(String(63), nullable=False, default="v1")
    ingestion_status: Mapped[str] = mapped_column(String(31), nullable=False, default="uploaded")
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, default=dict, server_default="{}"
    )


class Chunk(Base, PkMixin, TenantMixin):
    """One chunk per document version section. Embedding added when the
    pgvector extension migration lands (ticket 14); text column is the
    retrievable excerpt."""

    __tablename__ = "chunks"
    __table_args__ = (UniqueConstraint("document_version_id", "ordinal", name="uq_chunk_ordinal"),)

    document_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("document_versions.id"), nullable=False, index=True
    )
    section_path: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    ordinal: Mapped[int] = mapped_column(nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    text_hash: Mapped[str] = mapped_column(String(127), nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, default=dict, server_default="{}"
    )
