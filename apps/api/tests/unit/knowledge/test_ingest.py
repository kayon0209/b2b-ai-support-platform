"""Unit tests: storage key rules, ingestion state machine, chunking."""

import pytest

from platform_core.knowledge.ingest import (
    InvalidTransition,
    chunk_sections,
    parse_markdown_sections,
    transition,
)
from platform_core.knowledge.storage import (
    ObjectKey,
    StorageValidationError,
    validate_content_type,
)

# --- Object key rules (ticket 11) ---


def test_object_key_is_tenant_prefixed() -> None:
    key = ObjectKey(
        tenant_id="0190-aaaa",
        document_version_id="0190-bbbb",
        filename="handbook v2.pdf",
    ).to_key()
    assert key.startswith("0190-aaaa/")
    assert key == "0190-aaaa/0190-bbbb/handbook v2.pdf"


def test_object_key_neutralizes_path_traversal() -> None:
    key = ObjectKey(
        tenant_id="t1",
        document_version_id="v1",
        filename="../../etc/passwd",
    ).to_key()
    assert "../" not in key and "..\\" not in key


def test_content_type_allowlist() -> None:
    assert validate_content_type("application/pdf") == "application/pdf"
    with pytest.raises(StorageValidationError):
        validate_content_type("application/x-msdownload")
    with pytest.raises(StorageValidationError):
        validate_content_type(None)


# --- Ingestion state machine (ticket 12) ---


def test_happy_path_transitions() -> None:
    for current, target in [
        ("uploaded", "parsing"),
        ("parsing", "chunking"),
        ("chunking", "embedding"),
        ("embedding", "indexing"),
        ("indexing", "ready"),
    ]:
        assert transition(current, target) == target


def test_failure_and_retry_paths() -> None:
    assert transition("parsing", "failed") == "failed"
    assert transition("failed", "queued_for_retry") == "queued_for_retry"
    assert transition("queued_for_retry", "parsing") == "parsing"


def test_invalid_transitions_rejected() -> None:
    with pytest.raises(InvalidTransition):
        transition("uploaded", "ready")  # cannot skip pipeline
    with pytest.raises(InvalidTransition):
        transition("ready", "ready")  # no self-loop
    with pytest.raises(InvalidTransition):
        transition("failed", "ready")  # must go through retry queue
    with pytest.raises(InvalidTransition):
        transition("unknown_state", "parsing")


def test_ready_is_terminal_except_lifecycle() -> None:
    assert transition("ready", "superseded") == "superseded"
    assert transition("ready", "expired") == "expired"
    with pytest.raises(InvalidTransition):
        transition("superseded", "ready")


# --- Structure-aware chunking (ticket 13) ---


def test_markdown_sections_capture_hierarchy() -> None:
    doc = """# Refunds

Refund policy body.

## Window

Thirty days after purchase.

# Shipping

Ships in 48h.
"""
    sections = parse_markdown_sections(doc)
    paths = [s.path for s in sections]
    assert ["Refunds"] in paths
    assert ["Refunds", "Window"] in paths
    assert ["Shipping"] in paths


def test_code_fence_headings_ignored() -> None:
    doc = """# Real

```markdown
# Fake Heading Inside Fence
```

Still real section.
"""
    sections = parse_markdown_sections(doc)
    assert all(s.path == ["Real"] for s in sections)


def test_long_sections_split_on_paragraphs() -> None:
    long_para = "word " * 300  # > MAX_CHUNK_CHARS
    second = "tail " * 300
    sections = [Section(path=["P"], text=long_para + "\n\n" + second)]
    chunks = chunk_sections(sections)
    assert len(chunks) >= 2
    assert all(len(c.text) <= 1500 for c in chunks)  # small overshoot allowance


def test_empty_sections_dropped() -> None:
    sections = parse_markdown_sections("# Title only\n\n\n")
    assert sections == []


from platform_core.knowledge.ingest import Section  # noqa: E402  (used above)
