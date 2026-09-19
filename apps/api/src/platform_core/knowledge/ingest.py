"""Ingestion pipeline: explicit state machine + structure-aware chunking
(tickets 12-13; iteration plan 1.1/1.6/4.6).

States (docs/domain-model.md):
  UPLOADED -> PARSING -> CHUNKING -> EMBEDDING -> INDEXING -> READY
  any active -> FAILED; FAILED -> QUEUED_FOR_RETRY
  READY -> SUPERSEDED | EXPIRED

Jobs are idempotent by (document_version_id, pipeline_version): re-running
a completed pipeline is a no-op, a failed one restarts cleanly from the
current persisted state. The chunking configuration is part of the pipeline
identity: a config change bumps PIPELINE_VERSION, which alone triggers a
re-chunk of every version - no separate invalidation mechanism.
"""

import hashlib
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any

from platform_core.knowledge.models import IngestionStatus

PIPELINE_VERSION = "v2"

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


# --- Structure-aware chunking (ticket 13; plan 1.1/1.6/4.6) -----------------

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")


@dataclass
class Section:
    path: list[str]  # heading hierarchy, e.g. ["Refunds", "Window"]
    text: str
    # Chunk-level facts the rest of the pipeline may consume: content_kind
    # ("table" chunks are never split or overlapped), oversized flag, and the
    # cleaning ledger for this chunk.
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ChunkingConfig:
    """Chunk-shape parameters, resolved from settings at run time.

    Part of the pipeline identity: the effective config is written into the
    document version's metadata next to PIPELINE_VERSION, so any chunk set is
    reproducible from its row and a config change alone triggers a re-chunk.
    """

    max_chars: int = 1200
    min_chars: int = 50
    overlap_chars: int = 150

    def __post_init__(self) -> None:
        if self.max_chars < self.min_chars:
            raise ValueError("max_chars must be >= min_chars")
        if self.min_chars < 1:
            raise ValueError("min_chars must be positive")
        if self.overlap_chars < 0:
            raise ValueError("overlap_chars must be non-negative")
        # Overlap at or above the chunk size would make every chunk mostly a
        # copy of its neighbour: pure duplication in the index.
        if self.overlap_chars >= self.max_chars:
            raise ValueError("overlap_chars must be smaller than max_chars")

    def as_metadata(self) -> dict[str, int]:
        return {
            "max_chars": self.max_chars,
            "min_chars": self.min_chars,
            "overlap_chars": self.overlap_chars,
        }


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
    """Extract text from a DOCX document, tables included (plan 1.6).

    Tables are rendered as markdown so a row keeps its column semantics —
    "the warranty period is column 3" survives only if the header row
    travels with the cells. A table is emitted as ONE fenced block, which
    `chunk_sections` then treats as atomic: splitting a table across chunks
    severs rows from their headers, and no overlap can repair that.

    Paragraphs are joined with newlines so `parse_markdown_sections` sees
    paragraph boundaries. Raises `IngestionError` on corrupt files.
    """
    import io

    from docx import Document

    try:
        doc = Document(io.BytesIO(raw))
    except Exception as exc:
        raise IngestionError(f"DOCX parse failed: {type(exc).__name__}: {exc}") from exc

    parts: list[str] = []
    # Walk the document body in order so a table lands between the paragraphs
    # it belongs between, not appended at the end where its context is lost.
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    body = doc.element.body
    for child in body.iterchildren():
        if child.tag.endswith("}p"):
            text = Paragraph(child, doc).text
            if text:
                parts.append(text)
        elif child.tag.endswith("}tbl"):
            table = Table(child, doc)
            rows: list[list[str]] = []
            for row in table.rows:
                rows.append([cell.text.strip().replace("\n", " ") for cell in row.cells])
            parts.append(_render_markdown_table(rows))
    return "\n\n".join(parts)


def _render_markdown_table(rows: list[list[str]]) -> str:
    """Render a table as a fenced markdown block with a kind marker.

    The `[TABLE]` sentinel is what `chunk_sections` keys on: everything
    between the fences is one indivisible chunk, whatever its length.
    """
    if not rows:
        return ""
    header = rows[0]
    width = max(len(r) for r in rows) if rows else 0
    header = header + [""] * (width - len(header))
    lines = ["[TABLE]", "| " + " | ".join(header) + " |", "| " + " | ".join(["---"] * width) + " |"]
    for row in rows[1:]:
        row = row + [""] * (width - len(row))
        lines.append("| " + " | ".join(row) + " |")
    lines.append("[/TABLE]")
    return "\n".join(lines)


_TABLE_BLOCK = re.compile(r"^\[TABLE\]$/", re.MULTILINE)


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


def chunk_sections(sections: list[Section], config: ChunkingConfig | None = None) -> list[Section]:
    """Split long sections on paragraph boundaries with paragraph-level
    overlap; merge tiny adjacent sections under the same parent.

    Overlap (plan 1.1): when a section is split, the tail paragraphs of the
    previous chunk are carried into the head of the next, up to
    `overlap_chars`. The overlap is an upper bound, not an exact figure —
    whole paragraphs are carried or not, because cutting a paragraph
    mid-sentence to hit a character count trades readability for nothing.
    Cross-paragraph arguments ("...as covered in the previous section, the
    window is 30 days") survive the cut only if the antecedent travels with
    the continuation.

    Table blocks (`[TABLE]`...`[/TABLE]`, plan 1.6) are atomic: never split,
    never overlapped, and each becomes its own chunk with
    `content_kind="table"`, so a citation to "row 3 of the warranty table"
    resolves to a chunk that still has its header row.

    Output chunks never exceed `max_chars` except a single paragraph (or one
    table) that is longer on its own; those are hard-split on sentence
    boundaries and flagged `oversized` rather than silently truncated.
    """
    cfg = config or ChunkingConfig()
    chunks: list[Section] = []
    for section in sections:
        if _TABLE_BLOCK.search(section.text) and len(section.text) <= cfg.max_chars:
            # A whole-table section stays one chunk as-is.
            section.meta["content_kind"] = "table"
            chunks.append(section)
            continue
        if len(section.text) <= cfg.max_chars:
            chunks.append(section)
            continue
        chunks.extend(_split_section(section, cfg))
    merged = _merge_tiny(chunks, cfg)
    if cfg.overlap_chars:
        merged = _apply_overlap(merged, cfg)
    return merged


def _split_section(section: Section, cfg: ChunkingConfig) -> list[Section]:
    """Split one over-long section into table-atomic, paragraph-packed chunks."""
    paragraphs = _split_paragraphs(section.text)
    chunks: list[Section] = []
    current: list[str] = []
    current_kind = "text"
    size = 0

    def flush() -> None:
        nonlocal current, current_kind, size
        if current:
            chunks.append(
                Section(
                    path=section.path,
                    text="\n\n".join(current),
                    meta={"content_kind": current_kind} if current_kind != "text" else {},
                )
            )
        current, current_kind, size = [], "text", 0

    for para, kind in paragraphs:
        is_table = kind == "table"
        if is_table and current:
            flush()  # a table never shares a chunk with surrounding prose
        if is_table:
            chunks.append(Section(path=section.path, text=para, meta={"content_kind": "table"}))
            continue
        if size + len(para) > cfg.max_chars and current:
            flush()
        if len(para) > cfg.max_chars:
            # A single paragraph longer than the budget: split by sentence,
            # hard-split as a last resort, and flag it so the tuning report
            # can see how much of the corpus this happens to.
            for piece in _hard_split(para, cfg.max_chars):
                chunks.append(
                    Section(
                        path=section.path,
                        text=piece,
                        meta={"oversized": True, "content_kind": kind},
                    )
                )
            continue
        current.append(para)
        size += len(para)
    flush()
    return chunks


def _split_paragraphs(text: str) -> list[tuple[str, str]]:
    """Paragraphs with a kind: 'table' for fenced table blocks, else 'text'."""
    out: list[tuple[str, str]] = []
    buffer: list[str] = []
    in_table = False

    def flush_buffer() -> None:
        if buffer:
            joined = "\n\n".join(buffer).strip()
            if joined:
                out.append((joined, "table" if in_table else "text"))
            buffer.clear()

    for block in text.split("\n\n"):
        stripped_block = block.strip()
        if stripped_block.startswith("[TABLE]") and stripped_block.endswith("[/TABLE]"):
            # A table rendered as one block (the DOCX renderer's output):
            # atomic, whichever way the fences are spaced.
            flush_buffer()
            out.append((stripped_block, "table"))
        elif stripped_block == "[TABLE]":
            flush_buffer()
            in_table = True
            buffer.append(stripped_block)
        elif stripped_block == "[/TABLE]":
            buffer.append(stripped_block)
            flush_buffer()
            in_table = False
        elif in_table:
            buffer.append(block)
        elif block.strip():
            out.append((block, "text"))
    flush_buffer()
    return out


_SENTENCE_RE = re.compile(r"(?<=[。！？.!?])\s*")


def _hard_split(text: str, max_chars: int) -> list[str]:
    """Sentence split, then a character cut for a sentence that is itself
    over-long. Oversized chunks are flagged upstream; this only keeps the
    contract "no chunk exceeds max_chars unless unavoidable" honest."""
    if len(text) <= max_chars:
        return [text]
    sentences = _SENTENCE_RE.split(text)
    pieces: list[str] = []
    current = ""
    for sentence in sentences:
        while len(sentence) > max_chars:
            if current:
                pieces.append(current)
                current = ""
            pieces.append(sentence[:max_chars])
            sentence = sentence[max_chars:]
        if len(current) + len(sentence) + 1 > max_chars and current:
            pieces.append(current)
            current = sentence
        else:
            current = f"{current} {sentence}".strip()
    if current:
        pieces.append(current)
    return pieces


def _apply_overlap(chunks: list[Section], cfg: ChunkingConfig) -> list[Section]:
    """Carry the previous chunk's tail paragraphs into the next chunk's head.

    Tables are skipped in both directions: a table chunk is atomic and a
    prose chunk must not gain a table tail (the table is already its own
    retrievable unit), and a table chunk must not grow a prose head.
    """
    overlapped: list[Section] = []
    previous_tail: list[str] = []
    for chunk in chunks:
        if chunk.meta.get("content_kind") == "table":
            overlapped.append(chunk)
            previous_tail = []
            continue
        head = "\n\n".join(previous_tail)
        if head:
            overlapped.append(
                Section(path=chunk.path, text=f"{head}\n\n{chunk.text}", meta=dict(chunk.meta))
            )
        else:
            overlapped.append(chunk)
        paragraphs = [p for p in chunk.text.split("\n\n") if p.strip()]
        tail: list[str] = []
        used = 0
        for para in reversed(paragraphs):
            if used + len(para) > cfg.overlap_chars:
                break
            tail.insert(0, para)
            used += len(para) + 2
        previous_tail = tail
    return overlapped


def _merge_tiny(chunks: list[Section], cfg: ChunkingConfig) -> list[Section]:
    """Fold chunks shorter than `min_chars` into a neighbour sharing their
    heading path.

    Only same-path neighbours merge: a one-line note under "Refunds" must not
    be appended to a chunk from "Shipping", or its citation would point at a
    passage that says something else. Tables never merge in either direction
    (a table's rows must keep their own header row). If no neighbour fits
    under `max_chars` the tiny chunk is kept as it is - better a small chunk
    than an over-long one that dilutes the embedding and breaks the
    `max_chars` contract.

    Also drops empty sections, which parsing can produce from a heading with
    no body.
    """
    merged: list[Section] = []
    for chunk in chunks:
        if not chunk.text.strip():
            continue
        previous = merged[-1] if merged else None
        is_table = chunk.meta.get("content_kind") == "table" or (
            previous is not None and previous.meta.get("content_kind") == "table"
        )
        if (
            previous is not None
            and not is_table
            and len(chunk.text) < cfg.min_chars
            and previous.path == chunk.path
            and len(previous.text) + len(chunk.text) + 2 <= cfg.max_chars
        ):
            merged[-1] = Section(
                path=previous.path,
                text=previous.text + "\n\n" + chunk.text,
                meta=dict(previous.meta),
            )
            continue
        merged.append(chunk)
    return merged


# --- Cleaning (plan 4.6) ----------------------------------------------------
#
# Deterministic, individually switchable, and every step reports what it
# changed. A cleaning step that cannot say what it did cannot be audited, and
# an unauditable transformation of the corpus is how "the answer used to be
# there" incidents start.

_PAGE_NUMBER = re.compile(r"^\s*(?:page\s*)?\d{1,4}\s*(?:/\s*\d{1,4})?\s*$", re.IGNORECASE)


@dataclass
class CleaningReport:
    """What the cleaning stage did, written into version metadata."""

    unicode_normalized: int = 0
    boilerplate_lines_removed: int = 0
    duplicate_chunks_removed: int = 0
    chunks_total: int = 0

    def as_metadata(self) -> dict[str, int]:
        return {
            "unicode_normalized": self.unicode_normalized,
            "boilerplate_lines_removed": self.boilerplate_lines_removed,
            "duplicate_chunks_removed": self.duplicate_chunks_removed,
            "chunks_total": self.chunks_total,
        }


def clean_text(raw: str, *, strip_boilerplate: bool = True) -> tuple[str, CleaningReport]:
    """Normalize whitespace and unicode; optionally strip page furniture.

    - NFC normalization folds visually identical accents into one codepoint,
      so a duplicated document hashes the same whether it was uploaded from
      macOS or Windows.
    - Page-number-only lines ("12", "Page 3/9") are index noise from the
      PDF extractor: embedded into a chunk they match "page 12" queries and
      pollute every FTS path.
    """
    report = CleaningReport()
    normalized = unicodedata.normalize("NFC", raw)
    if normalized != raw:
        report.unicode_normalized = 1
    if not strip_boilerplate:
        return normalized, report
    kept: list[str] = []
    for line in normalized.splitlines():
        if _PAGE_NUMBER.match(line):
            report.boilerplate_lines_removed += 1
            continue
        kept.append(line)
    return "\n".join(kept), report


def dedupe_chunks(chunks: list[Section]) -> tuple[list[Section], int]:
    """Drop chunks whose normalized text duplicates an earlier chunk.

    Import/export pipelines produce the same page twice with different
    headings; both indexed, every query retrieves the pair, RRF fuses them
    and the duplicate crowds out a genuinely different source. First
    occurrence wins; the report counts what was dropped.
    """
    seen: set[str] = set()
    kept: list[Section] = []
    dropped = 0
    for chunk in chunks:
        digest = hashlib.sha256(
            re.sub(r"\s+", " ", chunk.text.strip().lower()).encode()
        ).hexdigest()
        if digest in seen:
            dropped += 1
            continue
        seen.add(digest)
        kept.append(chunk)
    return kept, dropped


# --- Front-matter metadata (plan 1.5: the filter's source) -------------------

_FRONT_MATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?", re.DOTALL)

# Keys the retrieval filter is allowed to consume. Shared with the
# MetadataFilter vocabulary — a key not listed here is free-form document
# metadata, never a retrieval constraint.
METADATA_KEYS = (
    "product",
    "model",
    "hardware_version",
    "firmware_version",
    "region",
    "language",
    "doc_type",
    "classification",
    "authority",
)


def extract_front_matter(text: str) -> tuple[str, dict[str, str]]:
    """Split a leading `---` front-matter block off the document body.

    Returns (body, metadata). Values are lowercased and trimmed; unknown keys
    pass through untouched (the filter allowlist decides what is usable).
    No front-matter, no error — most documents have none and the pipeline is
    required to be indifferent to that.
    """
    match = _FRONT_MATTER.match(text)
    if match is None:
        return text, {}
    metadata: dict[str, str] = {}
    for line in match.group(1).splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip().lower()
        value = value.strip().strip('"').strip("'").lower()
        if key and value:
            metadata[key] = value
    return text[match.end() :], metadata
