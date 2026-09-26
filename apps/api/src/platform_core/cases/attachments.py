"""Evidence attached to a case: what is acceptable, where it lives, the row.

The research report's stage 3 pairs a quality complaint with its evidence -
"`case.create` + 证据附件（MinIO 预签名 URL）→ 人工裁定". The complaint half
exists; this is the evidence half, and it exists because the adjudication is a
person's and they cannot make it from a subject line.

**Uploads go through the API; reads use a pre-signed URL.** That split is not
arbitrary and it is the one the knowledge corpus already chose: routing the
bytes through the API is what lets the content type and the size cap be
enforced *before* anything is stored, whereas a pre-signed PUT lets a client
write arbitrary bytes and only then have the API discover they are not allowed,
after the object exists. Reading is the opposite problem - a pre-signed GET
gives the reviewer a short-lived URL without handing out a credential, which is
what a browser `<img>` or a download link needs.

**The allowlist is this module's, not the knowledge corpus's.** They are
different rules for different content: knowledge documents are parsed and
indexed, so images are useless there; a quality complaint's evidence is
frequently a photograph of the board or a Gerber archive, so they are the
point here. Sharing one list would mean either refusing the evidence or
indexing a JPEG.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from platform_core.cases.models import Case, CaseAttachment

# What a complaint's evidence actually is: a photograph of the board, a
# measurement log, a PDF report, or a fabrication archive.
ALLOWED_ATTACHMENT_TYPES = frozenset(
    {
        "image/jpeg",
        "image/png",
        "image/webp",
        "application/pdf",
        "text/plain",
        "text/csv",
        # Gerber/ODB++ and drill data arrive as archives. Accepted as opaque
        # bytes: the platform does not parse them, and pretending to validate
        # a format it does not read would be theatre.
        "application/zip",
        "application/gzip",
        "application/x-tar",
        "application/octet-stream",
    }
)

# The same ceiling the knowledge corpus uses. Not shared as a constant because
# the two limits are separate decisions that happen to agree today; a board
# photograph and a parsed policy document have no reason to move together.
MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024

# Long enough for a real filename, short enough that the column is never the
# constraint. Applied to the stored display name, not to the object key.
MAX_FILENAME_CHARS = 255


class AttachmentError(Exception):
    """A refused attachment, with a code the API maps to a status."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


def validate_attachment(*, content_type: str | None, data: bytes) -> str:
    """Return the accepted content type, or refuse with a reason.

    Refusing rather than coercing: `application/octet-stream` is accepted
    because Gerber archives genuinely arrive that way, but an unlisted type is
    not silently relabelled - the uploader is told which rule they missed.
    """
    if not data:
        raise AttachmentError("EMPTY_ATTACHMENT", "the uploaded file is empty")
    if len(data) > MAX_ATTACHMENT_BYTES:
        raise AttachmentError(
            "ATTACHMENT_TOO_LARGE",
            f"attachment exceeds {MAX_ATTACHMENT_BYTES // (1024 * 1024)} MiB",
        )
    if not content_type or content_type not in ALLOWED_ATTACHMENT_TYPES:
        raise AttachmentError("UNSUPPORTED_ATTACHMENT_TYPE", f"content type {content_type!r}")
    return content_type


def attachment_object_key(
    *,
    tenant_id: uuid.UUID,
    case_id: uuid.UUID,
    attachment_id: uuid.UUID,
    filename: str,
) -> str:
    """Build the object key. **Never from the uploader's path.**

    The key is `<tenant>/cases/<case>/<attachment>/<display name>`, and every
    component before the name is generated, so `../../other-tenant/secret.pdf`
    cannot escape: the separators in the name are replaced, exactly as
    `knowledge.storage.ObjectKey` does. The tenant prefix means a bucket-level
    mistake still leaves one tenant's evidence separated from another's.
    """
    safe = filename.replace("/", "_").replace("\\", "_").strip()
    # A segment of exactly `.` or `..` is the one remaining way a name could
    # move the path, because that is the only case a URL consumer normalises.
    # A longer name that merely *contains* dots (`.._.._x.jpg`) is a single
    # literal segment and moves nothing - which is why the check is equality
    # and not a substring test. An empty name lands here too.
    if safe in {"", ".", ".."}:
        safe = "attachment"
    return f"{tenant_id}/cases/{case_id}/{attachment_id}/{safe[:MAX_FILENAME_CHARS]}"


async def require_case(session: AsyncSession, *, case_id: uuid.UUID) -> None:
    """The case must exist in this tenant.

    Under RLS another tenant's case is simply invisible, so NOT_FOUND covers
    both "does not exist" and "belongs to someone else" - the same collapse the
    knowledge upload uses for spaces, and for the same reason: attaching
    evidence must not become a probe for other tenants' case ids.
    """
    found = (await session.execute(select(Case.id).where(Case.id == case_id))).scalar_one_or_none()
    if found is None:
        raise AttachmentError("CASE_NOT_FOUND", str(case_id))


async def create_attachment(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    case_id: uuid.UUID,
    filename: str,
    content_type: str,
    data: bytes,
    uploaded_by: str | None,
    storage: Any,
) -> CaseAttachment:
    """Store the bytes, then record the row.

    Store first: a row pointing at an object that failed to upload is a broken
    reference an operator cannot repair, whereas an object with no row is
    unreferenced bytes that cost storage and nothing else. The same ordering
    the knowledge upload uses.
    """
    import time

    await require_case(session, case_id=case_id)

    attachment_id = uuid.uuid4()
    display_name = (filename or "attachment")[:MAX_FILENAME_CHARS]
    key = attachment_object_key(
        tenant_id=tenant_id,
        case_id=case_id,
        attachment_id=attachment_id,
        filename=display_name,
    )
    # Off the event loop: `put_object` is a synchronous HTTP call, and a 25 MiB
    # body would otherwise stall every other request in this process for the
    # duration of the transfer.
    await asyncio.to_thread(storage.put_object, key, data, content_type)

    row = CaseAttachment(
        id=attachment_id,
        tenant_id=tenant_id,
        case_id=case_id,
        object_key=key,
        filename=display_name,
        content_type=content_type,
        size_bytes=len(data),
        uploaded_by=uploaded_by,
        created_at=int(time.time()),
    )
    session.add(row)
    await session.flush()
    return row


async def list_attachments(session: AsyncSession, *, case_id: uuid.UUID) -> list[CaseAttachment]:
    """Newest last: evidence is read in the order it was gathered."""
    rows = (
        (
            await session.execute(
                select(CaseAttachment)
                .where(CaseAttachment.case_id == case_id)
                .order_by(CaseAttachment.created_at, CaseAttachment.id)
            )
        )
        .scalars()
        .all()
    )
    return list(rows)
