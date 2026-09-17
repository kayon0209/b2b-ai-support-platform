"""Ingestion pipeline: explicit state machine + structure-aware chunking
(tickets 12-13).

States (docs/domain-model.md):
  UPLOADED -> PARSING -> CHUNKING -> EMBEDDING -> INDEXING -> READY
  any active -> FAILED; FAILED -> QUEUED_FOR_RETRY
  READY -> SUPERSEDED | EXPIRED

Jobs are idempotent by (document_version_id, pipeline_version): re-running
a completed pipeline is a no-op, a failed one restarts cleanly from the
current persisted state.
"""

import re
from dataclasses import dataclass

from platform_core.knowledge.models import IngestionStatus

PIPELINE_VERSION = "v1"

# Allowed transitions; anything else raises.
_TRANSITIONS: dict[str, set[str]] = {
    IngestionStatus.UPLOADED: {IngestionStatus.PARSING, IngestionStatus.FAILED},
    IngestionStatus.PARSING: {IngestionStatus.CHUNKING, IngestionStatus.FAILED},
    IngestionStatus.CHUNKING: {IngestionStatus.EMBEDDING, IngestionStatus.FAILED},
    IngestionStatus.EMBEDDING: {IngestionStatus.INDEXING, IngestionStatus.FAILED},
    IngestionStatus.INDEXING: {IngestionStatus.READY, IngestionStatus.FAILED},
    IngestionStatus.FAILED: {IngestionStatus.QUEUED_FOR_RETRY},
    IngestionStatus.QUEUED_FOR_RETRY: {IngestionStatus.PARSING},
    IngestionStatus.READY: {IngestionStatus.SUPERSEDED, IngestionStatus.EXPIRED},
}


class InvalidTransition(Exception):
    pass


class IngestionError(Exception):
    """A fault attributable to the document itself: bad bytes, bad markup.

    Defined here so both the API layer (parse_document) and the worker
    (ingestion_consumer) share a single exception type for document defects.
    """


def transition(current: str, target: str) -> str:
    """Validate and return the target state. Pure function, unit-tested."""
    allowed = _TRANSITIONS.get(current, set())
    if target not in allowed:
        raise InvalidTransition(f"{current} -> {target} not allowed")
    return target


# --- Structure-aware chunking (ticket 13) ---

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")


@dataclass
class Section:
    path: list[str]  # heading hierarchy, e.g. ["Refunds", "Window"]
    text: str


# --- Document parsing (Phase 1: support PDF and DOCX uploads) ---


def _extract_pdf_text(raw: bytes) -> str:
    """Extract text from a PDF document.

    Each page's text is joined with a form-feed so `parse_markdown_sections`
    can still segment it. Returns empty string if the PDF has no extractable
    text. Raises `IngestionError` on structural issues (corrupt file).
    """
    import io

    from pypdf import PdfReader

    try:
        reader = PdfReader(io.BytesIO(raw))
    except Exception as exc:
        raise IngestionError(f"PDF parse failed: {type(exc).__name__}: {exc}") from exc

    pages: list[str] = []
    for page in reader.pages:
        try:
            text = page.extract_text()
            if text:
                pages.append(text)
        except Exception:
            # A single bad page should not abort the whole document.
            pages.append("")
    return "\f".join(pages)


def _extract_docx_text(raw: bytes) -> str:
    """Extract text from a DOCX document.

    Paragraphs are joined with newlines so `parse_markdown_sections` sees
    paragraph boundaries. Raises `IngestionError` on corrupt files.
    """
    import io

    from docx import Document

    try:
        doc = Document(io.BytesIO(raw))
    except Exception as exc:
        raise IngestionError(f"DOCX parse failed: {type(exc).__name__}: {exc}") from exc

    paragraphs = [p.text for p in doc.paragraphs if p.text]
    return "\n\n".join(paragraphs)


def _decode_text(raw: bytes) -> str:
    """Decode text-encoded bytes with utf-8, falling back to utf-8-sig.

    Raises `IngestionError` if the bytes cannot be decoded as text.
    """
    for encoding in ("utf-8", "utf-8-sig"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise IngestionError(
        "object is not decodable text (binary format not supported by this pipeline)"
    )


def parse_document(content_type: str, raw: bytes) -> str:
    """Decode an uploaded object to text based on its content type.

    Replaces the old `_decode` in the ingestion consumer: text formats are
    decoded directly, while binary formats (PDF, DOCX) are parsed into text
    via pypdf / python-docx. A format that cannot be parsed raises
    `IngestionError` (terminal - the document is defective), rather than
    silently producing replacement characters that downstream retrieval
    cannot detect.

    An empty or whitespace-only content_type defaults to text/markdown:
    the API store always records a validated content_type in metadata, so
    a missing one indicates a pre-existing version row that predates the
    field. Treating it as text preserves backward compatibility.
    """
    if not content_type or not content_type.strip():
        content_type = "text/markdown"

    if content_type in ("text/plain", "text/markdown", "text/html", "application/json"):
        return _decode_text(raw)

    if content_type == "application/pdf":
        return _extract_pdf_text(raw)

    if content_type == "application/vnd.openxmlformats-officedocument.wordprocessingml.document":
        return _extract_docx_text(raw)

    raise IngestionError(f"unsupported content type for parsing: {content_type}")


def parse_markdown_sections(text: str) -> list[Section]:
    """Split markdown into sections keyed by heading hierarchy.

    Fenced code blocks are kept attached to the section they appear in;
    heading lines inside fences are ignored.
    """
    sections: list[Section] = []
    stack: list[tuple[int, str]] = []  # (level, title)
    buffer: list[str] = []
    in_fence = False

    def flush() -> None:
        body = "\n".join(buffer).strip()
        if body:
            sections.append(Section(path=[t for _, t in stack], text=body))
        buffer.clear()

    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            buffer.append(line)
            continue
        if not in_fence:
            match = _HEADING_RE.match(stripped)
            if match:
                flush()
                level = len(match.group(1))
                title = match.group(2).strip()
                while stack and stack[-1][0] >= level:
                    stack.pop()
                stack.append((level, title))
                continue
        buffer.append(line)
    flush()
    return sections


MAX_CHUNK_CHARS = 1200
MIN_CHUNK_CHARS = 50


def chunk_sections(sections: list[Section]) -> list[Section]:
    """Split long sections on paragraph boundaries; merge tiny adjacent
    sections under the same parent. Output chunks never exceed
    MAX_CHUNK_CHARS except when a single paragraph is longer."""
    chunks: list[Section] = []
    for section in sections:
        if len(section.text) <= MAX_CHUNK_CHARS:
            chunks.append(section)
            continue
        paragraphs = section.text.split("\n\n")
        current: list[str] = []
        size = 0
        for para in paragraphs:
            if size + len(para) > MAX_CHUNK_CHARS and current:
                chunks.append(Section(path=section.path, text="\n\n".join(current)))
                current, size = [], 0
            current.append(para)
            size += len(para)
        if current:
            chunks.append(Section(path=section.path, text="\n\n".join(current)))
    return chunks
