"""Every field a log call passes must survive to the log line.

`JsonLogger` applies an allowlist: with no `TraceContext` attached, a field
whose name is not in `ALLOWED_LOG_FIELDS` is **dropped silently** - that is the
documented policy, and it is the right policy for a redaction boundary. What
made it a defect is that call sites were passing names the allowlist does not
have, so the lines looked complete and were not.

Measured before this file existed, in `worker/ingestion_consumer.py`:

    logger.warning("ingestion_deferred", version_id=..., reason_code=type(exc).__name__,
                   detail=str(exc)[:200])

- `version_id` is not allowlisted (the field is `document_version_id`) → the
  document that never ingested left no id in the log
- `detail` is not allowlisted → the reason went nowhere
- `reason_code` *is* allowlisted, so it survived as `"_Retryable"` - the
  exception's class name, which names no dependency

The same file also logged `chunks=` on the success path, where the allowlist
has `chunk_count`. Three quiet losses in one function, all found by running the
worker and reading its output, none of them by any existing test.

A source scan rather than a behavioural test, because the contract *is* about
call sites: any line added in future is covered without anyone remembering to
parameterise a test.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from observability import ALLOWED_LOG_FIELDS

REPO_ROOT = Path(__file__).resolve().parents[4]
SOURCE_ROOTS = (
    REPO_ROOT / "apps" / "api" / "src",
    REPO_ROOT / "apps" / "worker" / "src",
    REPO_ROOT / "packages",
)

_CALL = re.compile(r"\blogger\.(?:info|warning|error|debug|critical)\(")
# `ctx` selects the enrichment path instead of the allowlist. `exc_info` is a
# logging facility that `JsonLogger` forwards to the stdlib logger, so it is
# not a field either.
_NOT_A_FIELD = {"ctx", "exc_info"}


def _call_arguments(text: str, open_paren: int) -> str:
    """The argument text of the call starting at `open_paren`, parens balanced."""
    depth = 0
    for index in range(open_paren, len(text)):
        char = text[index]
        if char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
            if depth == 0:
                return text[open_paren + 1 : index]
    return text[open_paren + 1 :]


def _top_level_keywords(arguments: str) -> list[str]:
    """Keyword names at nesting depth 0, so `str(a.b)` is not parsed as a field."""
    names: list[str] = []
    depth = 0
    current = ""
    for char in arguments + ",":
        if char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
        if char == "," and depth == 0:
            match = re.match(r"\s*([A-Za-z_][A-Za-z0-9_]*)\s*=", current)
            if match:
                names.append(match.group(1))
            current = ""
        else:
            current += char
    return names


def _source_files() -> list[Path]:
    files: list[Path] = []
    for root in SOURCE_ROOTS:
        if root.is_dir():
            files.extend(sorted(root.rglob("*.py")))
    return files


def test_the_scan_finds_log_calls_at_all() -> None:
    """A guard on the guard: a scan matching nothing would pass forever."""
    total = sum(
        len(_CALL.findall(path.read_text(encoding="utf-8", errors="ignore")))
        for path in _source_files()
    )
    assert total > 20, f"only {total} logger calls found - has the pattern drifted?"


def test_every_logged_field_is_allowlisted() -> None:
    """The failure this prevents: a line that reads as complete and is not."""
    offenders: list[str] = []
    for path in _source_files():
        text = path.read_text(encoding="utf-8", errors="ignore")
        for match in _CALL.finditer(text):
            arguments = _call_arguments(text, match.end() - 1)
            for name in _top_level_keywords(arguments):
                if name in _NOT_A_FIELD or name in ALLOWED_LOG_FIELDS:
                    continue
                line = text.count("\n", 0, match.start()) + 1
                rel = path.relative_to(REPO_ROOT)
                offenders.append(f"{rel}:{line} passes {name!r}, which JsonLogger drops")

    assert offenders == [], (
        "these log fields would be dropped silently at runtime; either rename "
        "them to an allowlisted name or add the name to ALLOWED_LOG_FIELDS "
        "deliberately:\n  " + "\n  ".join(offenders)
    )


@pytest.mark.parametrize("required", ["reason_code", "error_code", "document_version_id"])
def test_the_fields_an_operator_needs_to_triage_are_allowlisted(required: str) -> None:
    """Pinned by name: these are the ones a failure investigation reaches for."""
    assert required in ALLOWED_LOG_FIELDS
