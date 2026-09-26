"""Suite hygiene: couplings between test files that only surface as flakes.

These tests read the test sources rather than running them. That is the point:
the defect they catch is invisible at runtime until an unrelated file happens
to run first, at which point it presents as "the suite is flaky" and gets
re-run until it passes.

**The invariant is deliberately narrow.** A sweep for "no two test files share
a tenant id" found fourteen shared ids across twenty-odd files, and the suite
is green - so sharing an id is not itself the problem. What breaks is sharing
an id *while seeding a different slug*: the seeds guard with
`ON CONFLICT (slug) DO NOTHING`, which does not cover the primary key, so
whichever file runs second dies on `tenants_pkey` with an error that names
neither file's intent. That is the property asserted here.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]

_UUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F-]{27}"
# `TENANT`, `TENANT_A`, `TENANT_OTHER`... and the same for slugs.
_TENANT_CONST = re.compile(rf'^([A-Z_]*TENANT[A-Z_]*)\s*=\s*"({_UUID})"', re.MULTILINE)
_SLUG_CONST = re.compile(r'^([A-Z_]*SLUG[A-Z_]*)\s*=\s*"([^"]+)"', re.MULTILINE)


def _test_files() -> list[Path]:
    roots = (REPO_ROOT / "apps" / "api" / "tests", REPO_ROOT / "tests")
    found: list[Path] = []
    for root in roots:
        if root.is_dir():
            found.extend(sorted(root.rglob("test_*.py")))
    return found


def _suffix(name: str) -> str:
    """The part after TENANT/SLUG, so `TENANT_A` pairs with `SLUG_A`."""
    for word in ("TENANT", "SLUG"):
        if word in name:
            return name.split(word, 1)[1]
    return ""


def _tenant_slug_pairs(source: str) -> list[tuple[str, str]]:
    """(tenant id, slug) for each id/slug constant pair in one file.

    Only files that declare both are considered: without a slug constant there
    is no pair to disagree about, and inventing one would flag files that
    happen to reuse a bare id.
    """
    tenants = {_suffix(n): u.lower() for n, u in _TENANT_CONST.findall(source)}
    slugs = {_suffix(n): s for n, s in _SLUG_CONST.findall(source)}
    return [(tenants[k], slugs[k]) for k in tenants.keys() & slugs.keys()]


def test_a_shared_tenant_id_is_seeded_with_one_slug_everywhere() -> None:
    """Two files may reuse an id only if they agree on its slug.

    Three integration files all used `01900000-...-d1` with three different
    slugs. Each seeded with `ON CONFLICT (slug) DO NOTHING`, which does not
    suppress a primary-key conflict, so the second file to run failed with
    `duplicate key value violates unique constraint "tenants_pkey"` - in the
    full suite only, and in a file that passed on its own.
    """
    by_id: dict[str, dict[str, list[str]]] = {}
    for path in _test_files():
        rel = str(path.relative_to(REPO_ROOT))
        for tenant_id, slug in _tenant_slug_pairs(path.read_text(encoding="utf-8")):
            by_id.setdefault(tenant_id, {}).setdefault(slug, []).append(rel)

    conflicts = {tid: slugs for tid, slugs in by_id.items() if len(slugs) > 1}
    assert conflicts == {}, (
        "a tenant id is seeded with different slugs in different files, which "
        f"fails on tenants_pkey for whichever runs second: {conflicts}"
    )


_CLAIM_CALLS = (
    re.compile(r"drain_ingestion_once\((?P<body>.*?)\n\s*\)", re.DOTALL),
    re.compile(r"claim_versions\((?P<body>.*?)\n\s*\)", re.DOTALL),
    # Single-line form: `claim_versions(session, batch=N)`.
    re.compile(r"claim_versions\((?P<body>[^\n]*)\)"),
)
_VERSION_IDS = re.compile(r"version_ids\s*=")

# Calls that must stay un-narrowed, with the reason. Narrowing them would
# remove the behaviour under test rather than make it deterministic.
_UNNARROWED_BY_DESIGN = {
    # Asserts the queue is empty, so an empty candidate set is the subject.
    "test_empty_queue_returns_no_claims",
    # Compares a narrowed claim against an un-narrowed one; the un-narrowed
    # call is the control.
    "test_a_targeted_claim_reaches_a_row_the_fifo_would_starve",
}


def _code_only(source: str) -> str:
    """Blank out comments and string literals so the scan sees code, not prose.

    Without this the check reads its own explanation - the docstring below
    quotes the offending call shape verbatim, as does the `_UNNARROWED_BY_DESIGN`
    comment - and reports violations in this file. `tokenize` is the reliable
    way to do it: a regex for `\"\"\"...\"\"\"` mis-handles the many other
    string literals in a test file, and does not see `#` comments at all.

    Line numbering is preserved (removed tokens become blank space, not new
    lines) because the caller maps a match offset back to a line to find the
    enclosing test function.
    """
    import io
    import tokenize

    lines = source.split("\n")
    try:
        for tok in tokenize.generate_tokens(io.StringIO(source).readline):
            if tok.type not in (tokenize.COMMENT, tokenize.STRING):
                continue
            (srow, scol), (erow, ecol) = tok.start, tok.end
            if srow == erow:
                lines[srow - 1] = lines[srow - 1][:scol] + lines[srow - 1][ecol:]
                continue
            # Multi-line literal: keep the first line's prefix, blank the rest.
            lines[srow - 1] = lines[srow - 1][:scol]
            for r in range(srow, erow - 1):
                lines[r] = ""
            lines[erow - 1] = " " * ecol + lines[erow - 1][ecol:]
    except tokenize.TokenError:
        return source
    return "\n".join(lines)


def test_ingestion_tests_narrow_the_claims_they_issue() -> None:
    """A test that seeds a version must claim *that* version, not the oldest N.

    `claim_ingestion_versions` is a global FIFO: it has no tenant filter and
    takes the oldest N claimable rows **anywhere**. `created_at` is whole
    seconds (migration 0017's INSERT trigger), so every row written in the
    same second is one ordering tie that `LIMIT n` cuts arbitrarily. A test
    that seeds a row and then calls `drain_ingestion_once(batch=5)` without
    narrowing is therefore asserting on a row it has no guarantee of
    receiving: it passes in isolation, where its row is the only candidate,
    and fails in the full suite, where another file's rows share its second.

    Measured: `test_embeddings_are_written_so_the_vector_half_of_fusion_works`
    passed alone and failed in a full run with `0 chunks` for its own version.
    Six further call sites were found the same way.
    Migration 0039 added the `version_ids` narrowing so the caller can ask for
    what it actually holds.

    This reads the test sources rather than running them, like the tenant/slug
    check above, because the defect is invisible at runtime until an unrelated
    file happens to run first. Two calls are exempt and the exemption is
    per-test, not per-file - see `_UNNARROWED_BY_DESIGN`.
    """
    offenders: list[str] = []
    for path in _test_files():
        source = path.read_text(encoding="utf-8")
        code = _code_only(source)
        owned = _test_function_spans(source)
        rel = str(path.relative_to(REPO_ROOT))

        for pattern in _CLAIM_CALLS:
            for match in pattern.finditer(code):
                body = match.group("body")
                if _VERSION_IDS.search(body):
                    continue
                name = _enclosing_test(owned, code[: match.start()].count("\n") + 1)
                if name in _UNNARROWED_BY_DESIGN:
                    continue
                offenders.append(f"{rel}::{name}: {' '.join(body.split())[:100]}")

    assert offenders == [], (
        "these ingestion calls claim from the global FIFO without narrowing to "
        "the version under test, so they can be starved by another test's rows "
        "sharing the same `created_at` second:\n  " + "\n  ".join(offenders)
    )


def _test_function_spans(source: str) -> list[tuple[str, int, int]]:
    """(name, first_line, last_line) for every `test_*` function in a module."""
    import ast

    spans: list[tuple[str, int, int]] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.FunctionDef) and node.name.startswith("test_"):
            end = node.end_lineno or node.lineno
            spans.append((node.name, node.lineno, end))
    return spans


def _enclosing_test(owned: list[tuple[str, int, int]], line: int) -> str:
    """The name of the `test_*` function containing `line`, or `<module>`."""
    hits = [name for name, start, end in owned if start <= line <= end]
    return hits[-1] if hits else "<module>"
