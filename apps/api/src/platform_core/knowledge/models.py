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
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy import (
    text as sa_text,
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
    # Epoch seconds at which the object bytes were confirmed absent from storage
    # (migration 0058). NULL means "not known to be gone" - see the erasure pass
    # for why that is a different question from whether the status is `expired`.
    #
    # Deliberately not a status value. A status says what we decided; this says
    # what happened to the bytes, and only one of those survives a worker that
    # dies between marking a row expired and reaching the endpoint.
    bytes_deleted_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    parser_version: Mapped[str] = mapped_column(String(63), nullable=False, default="v1")
    ingestion_status: Mapped[str] = mapped_column(String(31), nullable=False, default="uploaded")
    # Both timestamps are bigint epoch seconds and are owned by the database
    # (migration 0017 installs INSERT/UPDATE triggers that stamp them).
    #
    # - `created_at` is the FIFO ordering key for the ingestion claim. It is
    #   declared nullable in Python because the value is assigned by the
    #   trigger during INSERT, not by the ORM.
    # - `updated_at` answers "when did this row enter its current state",
    #   which `created_at` cannot. Stale-claim recovery depends on it: a
    #   document uploaded yesterday and claimed a second ago is not stale, and
    #   reclaiming it would let a second worker ingest the same version
    #   concurrently.
    #
    # Neither is written from Python: a caller that forgot to stamp one would
    # make recovery reclaim a row that is actively being processed. The
    # database owns the invariant instead.
    #
    # The `server_default` here is load-bearing, and getting it wrong is a
    # 503 on every upload. Without a server-side default in the ORM's own
    # metadata, SQLAlchemy emits the column in the INSERT as an explicit
    # NULL - and an explicit NULL overrides the table's own DEFAULT, so the
    # NOT NULL constraint fires. Measured: the upload endpoint returned
    # `IntegrityError: null value in column "updated_at"` until this was
    # declared. The trigger cannot save it, because the trigger runs after
    # the row reaches the table and the value is already NULL by then.
    #
    # It must be a **dialect-neutral literal**, not `EXTRACT(EPOCH FROM
    # now())`. The PostgreSQL expression is the honest default and is what
    # migration 0017 uses, but ORM metadata is not Postgres-only: unit tests
    # build the schema with `Base.metadata.create_all` against SQLite, which
    # cannot parse `EXTRACT(...)` and fails at DDL time. Measured: all 13
    # tool_gateway tests failed with
    # `sqlite3.OperationalError` when this was the Postgres expression.
    #
    # `0` is the right neutral placeholder because on PostgreSQL it is never
    # reached: the trigger in migration 0017 overwrites it on every INSERT
    # and UPDATE, so the real value is always the true epoch second. The
    # column is a monotonic "when did this row last change" marker, so a
    # SQLite fixture that leaves it at 0 is simply a row that has never been
    # touched - which is exactly what a freshly created fixture row is.
    created_at: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        server_default=sa_text("0"),
    )
    updated_at: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        server_default=sa_text("0"),
    )
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


class KnowledgeAlias(Base, PkMixin, TenantMixin):
    """A tenant-specific surface form for a canonical corpus term (plan 1.3).

    "GC-500" and "GateWay 500" are the same product only because this tenant
    says so. UNIQUE (tenant_id, alias) means one surface form maps to exactly
    one canonical term per tenant: two meanings for one alias would make
    query expansion a coin flip, so the second insert is refused rather than
    resolved by row order.
    """

    __tablename__ = "knowledge_aliases"
    __table_args__ = (UniqueConstraint("tenant_id", "alias", name="uq_aliases_tenant_alias"),)

    term: Mapped[str] = mapped_column(String(127), nullable=False, index=True)
    alias: Mapped[str] = mapped_column(String(127), nullable=False)
    # Expansion weight in (0, 2]: an alias is at best as authoritative as the
    # term itself, so it can rank a candidate up but never double it. This
    # mirrors migration 0033's column exactly (numeric(4,2), DEFAULT 1.00,
    # CHECK (0, 2]) — declaring it percent here coerced every write through
    # an Integer column and fought the migration's own CHECK constraint.
    weight: Mapped[float] = mapped_column(Numeric(4, 2), nullable=False, default=1.0)
    created_at: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
