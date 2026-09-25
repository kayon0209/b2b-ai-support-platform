"""Knowledge document ingest and access service.

This is the write path into the knowledge base. Before it existed, `documents`
and `document_versions` had models, an ingestion state machine (`ingest.py`),
ACLs (`acl.py`), object storage with SigV4 signing (`storage.py`) - and no way
for a document to enter the system. The retrieval side was fully implemented
and permanently empty.

Two rules drive the design:

1. **The object key is derived server-side, never accepted from the client.**
   `ObjectKey.to_key()` builds `<tenant>/<version>/<filename>`. If a caller
   could supply the key, a tenant could read another tenant's object by naming
   it, and the tenant-prefix rule in docs/security.md would be decorative.

2. **Downloads are pre-signed and short-lived, never proxied or public.**
   `storage.presign_get` existed but had no caller, so the documented access
   path ("pre-signed short-lived URLs for all client-facing access") was
   unimplemented. `authorize_download` is the only way to obtain a URL, and it
   re-checks the ACL at request time rather than trusting a previously issued
   link - a URL that leaks after revocation is still a leak, but it expires in
   minutes rather than living as long as the object.

ACL evaluation is delegated to `platform_core.knowledge.acl_service` so the
same predicate is used here and in retrieval. A document that is not readable
must not be downloadable either; two implementations of "can this principal
read this document" is how those two answers drift apart.
"""

import hashlib
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from platform_core.audit import service as audit_service
from platform_core.identity.tenant_context import TenantContext
from platform_core.knowledge import ingest
from platform_core.knowledge.models import (
    Document,
    DocumentVersion,
    IngestionStatus,
    KnowledgeSpace,
)
from platform_core.knowledge.scanning import (
    ContentScanner,
    ContentTypeMismatch,
    ScanVerdict,
    run_scan,
    verify_declared_type,
)
from platform_core.knowledge.storage import (
    ObjectKey,
    StorageValidationError,
    validate_content_type,
)

# Upload ceiling. Not a storage limit - a request-body limit. A 25 MiB cap
# keeps a single upload from occupying a worker for minutes while still
# covering the documents this pilot handles (policy PDFs, help articles,
# spreadsheets). Larger corpora belong in a sync connector, not an upload.
MAX_UPLOAD_BYTES = 25 * 1024 * 1024

VALID_CLASSIFICATIONS = ("public", "internal", "confidential", "restricted")


class KnowledgeError(Exception):
    """Domain failure with a stable wire code."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(detail or code)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class UploadedDocument:
    document_id: uuid.UUID
    version_id: uuid.UUID
    object_key: str
    content_hash: str
    ingestion_status: str


def content_hash(data: bytes) -> str:
    """sha256 over the bytes, prefixed with the algorithm.

    The algorithm is stored with the digest so a future migration to another
    hash does not have to guess which rows used which. Same reasoning as
    password storage, minus the salt - this is an integrity check, not a
    secret.
    """
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def _validate_upload(content_type: str | None, data: bytes) -> str:
    if not data:
        raise KnowledgeError("EMPTY_UPLOAD", "the uploaded file is empty")
    if len(data) > MAX_UPLOAD_BYTES:
        raise KnowledgeError(
            "UPLOAD_TOO_LARGE",
            f"upload exceeds {MAX_UPLOAD_BYTES // (1024 * 1024)} MiB",
        )
    try:
        media = validate_content_type(content_type)
    except StorageValidationError as exc:
        raise KnowledgeError("UNSUPPORTED_CONTENT_TYPE", str(exc)) from exc

    # The allowlist above answers "is this type acceptable"; this answers "are
    # these bytes that type". Before this, a file labelled PDF carrying an
    # executable passed the whole upload path and was retrieved as ordinary
    # knowledge.
    try:
        verify_declared_type(media, data)
    except ContentTypeMismatch as exc:
        raise KnowledgeError("CONTENT_TYPE_MISMATCH", str(exc)) from exc
    return media


async def create_space(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    name: str,
    ctx: TenantContext | None = None,
) -> KnowledgeSpace:
    """Create the container a document is uploaded into.

    This is the bootstrap step the knowledge base was missing. Uploading a
    document requires a space id, the only listing of spaces was a GET, and no
    other code path in the repository constructed a `KnowledgeSpace` - so a
    freshly provisioned tenant had no supported way to create one and the
    retrieval side stayed permanently empty. Measured on a live stack: 150 of
    167 customer questions were answered by abstaining, with nowhere to put the
    evidence.

    Names are not unique per tenant. A duplicate name is a confusing listing,
    not a correctness problem, and refusing it would push operators toward
    generated names that are harder to read than the ones they chose.
    """
    if not name.strip():
        raise KnowledgeError("INVALID_TITLE", "space name must not be blank")
    if len(name) > 255:
        raise KnowledgeError("INVALID_TITLE", "space name must be 255 characters or fewer")

    space = KnowledgeSpace(tenant_id=tenant_id, name=name.strip(), status="active")
    session.add(space)
    await session.flush()

    if ctx is not None:
        await audit_service.record(
            session,
            ctx=ctx,
            action="knowledge.space.created",
            resource_type="knowledge_space",
            resource_id=space.id,
            after={"name": space.name},
        )
    return space


async def _require_space(session: AsyncSession, tenant_id: uuid.UUID, space_id: uuid.UUID) -> None:
    """The space must exist in this tenant.

    Under RLS a foreign space is simply invisible, so NOT_FOUND covers both
    "does not exist" and "belongs to someone else" - the same collapse the
    gap service uses, for the same reason: an upload must not become a probe
    for other tenants' space ids.
    """
    space = (
        await session.execute(
            select(KnowledgeSpace).where(
                KnowledgeSpace.id == space_id,
                KnowledgeSpace.tenant_id == tenant_id,
            )
        )
    ).scalar_one_or_none()
    if space is None:
        raise KnowledgeError("NOT_FOUND", "no such knowledge space for this tenant")


async def create_document(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    space_id: uuid.UUID,
    title: str,
    canonical_uri: str,
    data: bytes,
    content_type: str | None,
    filename: str,
    owner_ref: str | None = None,
    classification: str = "internal",
    version_label: str = "v1",
) -> UploadedDocument:
    """Register a document and its first version, then start ingestion.

    Ordering matters and is deliberate: the version row is committed before
    the object is uploaded. A row without an object is recoverable (re-run
    ingestion, or delete the version); an object without a row is an orphan
    that costs storage and shows up in no listing, and nothing would ever
    clean it up.
    """
    if not title.strip():
        raise KnowledgeError("INVALID_TITLE", "title must not be blank")
    if not canonical_uri.strip():
        raise KnowledgeError("INVALID_URI", "canonical_uri must not be blank")
    if classification not in VALID_CLASSIFICATIONS:
        raise KnowledgeError(
            "INVALID_CLASSIFICATION",
            f"classification must be one of: {', '.join(VALID_CLASSIFICATIONS)}",
        )
    media_type = _validate_upload(content_type, data)
    await _require_space(session, tenant_id, space_id)

    document = Document(
        tenant_id=tenant_id,
        space_id=space_id,
        canonical_uri=canonical_uri,
        title=title,
        owner_ref=owner_ref,
        classification=classification,
    )
    session.add(document)
    await session.flush()

    # The scanner runs on the bytes already in hand, before the object is
    # uploaded, so a rejected file never occupies storage. A scanner outage is
    # recorded as `error` rather than treated as a pass: the version is stored
    # and stays invisible to retrieval until a scan succeeds.
    verdict = run_scan(
        ContentScanner(),
        key="",
        data=data,
        declared_type=media_type,
    )
    if verdict is ScanVerdict.INFECTED:
        raise KnowledgeError(
            "UPLOAD_REJECTED",
            "the file's contents do not match its declared type",
        )

    version = DocumentVersion(
        tenant_id=tenant_id,
        document_id=document.id,
        version_label=version_label,
        scan_status=verdict.as_status().value,
        content_hash=content_hash(data),
        status="processing",
        object_uri="",  # filled in below, once the key is derived
        ingestion_status=IngestionStatus.UPLOADED.value,
        metadata_json={"content_type": media_type, "size_bytes": len(data)},
    )
    session.add(version)
    await session.flush()

    # Key derived from the ids the database just assigned - not from the
    # client-supplied filename, and not from a request parameter.
    key = ObjectKey(
        tenant_id=str(tenant_id),
        document_version_id=str(version.id),
        filename=filename or "upload",
    ).to_key()
    version.object_uri = key
    await session.flush()

    return UploadedDocument(
        document_id=document.id,
        version_id=version.id,
        object_key=key,
        content_hash=version.content_hash,
        ingestion_status=str(version.ingestion_status),
    )


async def get_version(
    session: AsyncSession, *, tenant_id: uuid.UUID, version_id: uuid.UUID
) -> DocumentVersion:
    version = (
        await session.execute(
            select(DocumentVersion).where(
                DocumentVersion.id == version_id,
                DocumentVersion.tenant_id == tenant_id,
            )
        )
    ).scalar_one_or_none()
    if version is None:
        raise KnowledgeError("NOT_FOUND", "no such document version for this tenant")
    return version


async def list_versions(
    session: AsyncSession, *, tenant_id: uuid.UUID, document_id: uuid.UUID
) -> list[DocumentVersion]:
    rows = (
        await session.execute(
            select(DocumentVersion)
            .where(
                DocumentVersion.document_id == document_id,
                DocumentVersion.tenant_id == tenant_id,
            )
            .order_by(DocumentVersion.version_label)
        )
    ).scalars()
    return list(rows)


async def mark_ready(
    session: AsyncSession, *, tenant_id: uuid.UUID, version_id: uuid.UUID
) -> DocumentVersion:
    """Drive the version to READY through the validated state machine.

    Kept in the service so the transition table in `ingest.py` is the only
    place that decides which moves are legal - the router must not set
    `ingestion_status` directly, or the state machine becomes advisory.
    """
    version = await get_version(session, tenant_id=tenant_id, version_id=version_id)
    current = str(version.ingestion_status)
    for target in (
        IngestionStatus.PARSING,
        IngestionStatus.CHUNKING,
        IngestionStatus.EMBEDDING,
        IngestionStatus.INDEXING,
        IngestionStatus.READY,
    ):
        current = ingest.transition(current, target.value)
    version.ingestion_status = current
    version.status = "active"
    await session.flush()
    return version


async def authorize_download(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    version_id: uuid.UUID,
    principal_id: str,
    role: str,
    expires_seconds: int = 300,
) -> tuple[str, str]:
    """Return `(object_key, presigned_url)` for a readable version.

    The ACL check is the point of this function. A download URL is a bearer
    credential for the object, so issuing one to a principal without read
    permission would hand out the document without ever touching the retrieval
    path - the ACL on `knowledge_acls` would be bypassed entirely.
    """
    from platform_core.knowledge.acl_service import can_read_document

    version = await get_version(session, tenant_id=tenant_id, version_id=version_id)
    allowed = await can_read_document(
        session,
        tenant_id=tenant_id,
        document_id=version.document_id,
        principal_id=principal_id,
        role=role,
    )
    if not allowed:
        raise KnowledgeError("NOT_FOUND", "no such document version for this tenant")

    key = str(version.object_uri)
    if not key:
        # A version whose upload never completed. Surfaced explicitly rather
        # than presigning an empty key, which would produce a URL pointing at
        # the bucket root.
        raise KnowledgeError("OBJECT_MISSING", "this version has no stored object")

    from platform_core.config import get_settings

    settings = get_settings()
    # The caller may ask for a shorter window; never a longer one. A longer
    # request is clamped rather than rejected so a client that does not track
    # the policy still gets a working URL.
    ttl = min(expires_seconds, settings.presign_expiry_seconds)
    return key, presign_for(key, expires_seconds=ttl, settings=settings)


def presign_for(key: str, *, expires_seconds: int, settings: Any | None = None) -> str:
    """Sign a key with the configured bucket.

    Wrapped so the storage client is constructed in exactly one place; a
    second construction site with different settings is how a URL ends up
    signed for the wrong endpoint (and rejected by the server that stores the
    object).
    """
    from platform_core.config import get_settings

    url = object_storage(settings or get_settings()).presign_get(
        key, expires_seconds=expires_seconds
    )
    return str(url)


def _secret(value: Any) -> str | None:
    """Read a SecretStr without requiring the caller to know it is one."""
    if value is None:
        return None
    return value.get_secret_value() if hasattr(value, "get_secret_value") else str(value)


def object_storage(settings: Any, *, bucket: str | None = None) -> Any:
    """Build the object-store client. **The one construction site.**

    Public because the store is shared infrastructure that happens to live
    under `knowledge`: a case attachment uploads to the same bucket with the
    same credentials, and a second construction site with different settings is
    how an object ends up written to an endpoint the presigner does not sign
    for. The caller supplies its own content-type policy - see
    `MinioStorage.put_object`.

    `bucket` overrides only the bucket, never the endpoint or the credentials.
    That is what the backup job needs: it writes to a *different* bucket - the
    one where versioning is safe, because the live documents bucket must stay
    unversioned for erasure to mean anything - over the same connection. An
    override that could also redirect the endpoint would be a way for a caller
    to silently ship data somewhere else, which is the exact failure this
    function exists to prevent.

    (`knowledge/storage.py` mixes this infrastructure with `ObjectKey`, which
    is a knowledge concept. Splitting them is a rename, not a redesign, and is
    worth doing when a third caller appears.)
    """
    from platform_core.knowledge.storage import MinioStorage

    return MinioStorage(
        endpoint=settings.object_storage_endpoint,
        access_key=_secret(settings.object_storage_access_key),
        secret_key=_secret(settings.object_storage_secret_key),
        bucket=bucket or settings.object_storage_bucket,
        secure=settings.object_storage_secure,
    )


def upload_object(key: str, data: bytes, content_type: str) -> str:
    """Put the bytes. Separate from row creation so a storage failure can be
    retried without re-registering the document.

    The content-type check lives here rather than in the storage client, which
    is shared with modules whose acceptable types are different (see
    `MinioStorage.put_object`). Validating at this seam keeps the knowledge
    guarantee where the knowledge rule is.
    """
    from platform_core.config import get_settings

    validate_content_type(content_type)
    return str(object_storage(get_settings()).put_object(key, data, content_type))


def get_object(key: str) -> bytes:
    """Read an object's bytes for the ingestion pipeline.

    Sync, like `put_object`: the storage client is httpx-sync, and the
    worker's caller already runs it off the event loop's critical path. Kept
    here rather than in the worker so the bucket and credentials are resolved
    in exactly one place - the same reason `presign_for` is wrapped.
    """
    from platform_core.config import get_settings

    return bytes(object_storage(get_settings()).get_object(key))
