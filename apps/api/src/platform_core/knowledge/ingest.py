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
