"""`docs/feature-list-index.md` must keep up with the code it indexes.

The problem this guards
-----------------------
Source comments cite a "feature list N.N" that was never committed - it is the
product plan written for this customer during project setup, and it is not on
the internet either. Twenty-seven citations across twenty-one numbers pointed at
nothing a reader could open.

`docs/feature-list-index.md` is the replacement: each number mapped to a module,
a test count and a decision record. A mapping document about code drifts the
moment the code moves, and an index whose rows no longer resolve is worse than
no index - it looks authoritative while sending people to the wrong file.

What is asserted
----------------
1. **Every citation is indexed.** A new `feature list N.N` in a comment without
   a row here means the next reader hits the same dead end this file exists to
   remove. This is the assertion that actually matters, and it is the one that
   would have caught the original gap.
2. **Every implementation path exists.** A row naming a module that was renamed
   or deleted is worse than a missing row.
3. **Citations written without the `feature list` prefix are included.**
   `orchestrator.py` writes "(7.2)" and "7.1 trigger 7" with no prefix at all -
   four citations that a literal `feature list` search misses, and which were
   therefore missing from the first version of the index.

Parsed with a pattern that accepts both forms rather than grepping for one
string, because the second version of this bug was precisely a citation format
the first pattern did not match.
"""

from __future__ import annotations

import pathlib
import re

REPO_ROOT = pathlib.Path(__file__).resolve().parents[4]
SOURCE = REPO_ROOT / "apps" / "api" / "src" / "platform_core"
INDEX = REPO_ROOT / "docs" / "feature-list-index.md"

# Three citation forms exist in this tree, all measured rather than assumed:
#   "feature list 3.2"                 - the common one
#   "feature list 4A.3 / 4A.4"         - two numbers, spaces around the slash
#   "feature list 6.1/6.2"             - two numbers, no spaces
#   "Emotion (7.2)"  /  "7.1 trigger 7" - no prefix at all, and the number
#                                        comes *before* the keyword in one of them
# The merged forms are why a single-number pattern reports the second number as
# "indexed but no longer cited", and the prefix-less forms are why a literal
# `feature list` search misses four citations entirely.
_NUMBER = r"[0-9]{1,2}(?:\.[0-9]+)*(?:[A-Z](?:\.[0-9]+)?)?"
_CITATION = re.compile(
    rf"feature list ({_NUMBER})(?:\s*/\s*({_NUMBER}))?"
    rf"|({_NUMBER})\s+trigger\b"
    rf"|Emotion\s+\(({_NUMBER})"
)


def _cited_numbers() -> set[str]:
    found: set[str] = set()
    for path in SOURCE.rglob("*.py"):
        text = path.read_text(encoding="utf-8", errors="replace")
        for match in _CITATION.finditer(text):
            found.update(group for group in match.groups() if group)
    return found


def _indexed_numbers() -> set[str]:
    if not INDEX.is_file():
        return set()
    text = INDEX.read_text(encoding="utf-8")
    # Only the mapping table, so a number mentioned in prose elsewhere in the
    # document does not count as indexed.
    table = text.split("## 编号 → 实现映射", 1)[-1].split("## 原始编号无法核对", 1)[0]
    # Read the FIRST COLUMN only. Reading the whole table picks up line counts,
    # test counts and ADR numbers - which is what the first version did, and it
    # reported 18 numbers the code never cited.
    numbers: set[str] = set()
    for line in table.splitlines():
        if not line.startswith("|"):
            continue
        first_cell = line.split("|")[1]
        # A "/" in this cell means two numbers share a row, which is what the
        # merged citations look like ("6.1 / 6.2", "4A.3 / 4A.4").
        numbers.update(re.findall(r"[0-9]{1,2}(?:\.[0-9]+)*(?:[A-Z](?:\.[0-9]+)?)?", first_cell))
    return numbers


def test_every_cited_number_has_a_row() -> None:
    """The assertion that matters: a citation nobody can look up.

    Also catches the reverse failure, which is quieter - a row for a number the
    code no longer cites means the index has started describing a past state.
    """
    assert INDEX.is_file(), (
        "docs/feature-list-index.md is missing; every 'feature list N.N' citation "
        "in the source points at a document this repository does not contain"
    )
    cited = _cited_numbers()
    indexed = _indexed_numbers()

    missing = sorted(cited - indexed)
    assert not missing, (
        f"cited but not indexed: {missing}. Add a row, or say 'not implemented' "
        "explicitly - a blank is read as 'not yet looked up', which is a weaker "
        "and less useful conclusion than 'this does not exist'"
    )

    stale = sorted(indexed - cited)
    assert not stale, (
        f"indexed but no longer cited anywhere: {stale}. The index is describing "
        "code that has moved on"
    )


def test_every_implementation_path_in_the_index_exists() -> None:
    """A row naming a file that was renamed sends people to the wrong place.

    Read from the table's backtick column rather than from a list kept beside
    it, so the two cannot drift.
    """
    if not INDEX.is_file():
        return
    text = INDEX.read_text(encoding="utf-8")
    table = text.split("## 编号 → 实现映射", 1)[-1].split("## 原始编号无法核对", 1)[0]

    missing: list[str] = []
    for number, path in re.findall(
        r"^\|\s*([0-9][^|]*?)\s*\|[^|]*\|\s*`([^`]+)`\s*\|", table, re.M
    ):
        if not (SOURCE / path).is_file():
            missing.append(f"{number.strip()} -> {path}")
    assert not missing, f"the index points at files that do not exist: {missing}"


def test_the_index_states_that_the_original_is_unavailable() -> None:
    """The most important sentence in the file, guarded against deletion.

    Without it a reader who finds the index may believe it *is* the feature list
    rather than a mapping built from the code - and then treat an inferred
    subject line as if it were the original wording. The index says explicitly
    that the mapping is checkable and the original text is not recoverable.
    """
    assert INDEX.is_file()
    text = INDEX.read_text(encoding="utf-8")
    # Compared with all whitespace removed: the document wraps mid-phrase -
    # "不在本\n仓库" is split across lines 4 and 5 - so a literal match on a
    # phrase that straddles a line break tests the wrapping, not the sentence.
    flat = "".join(text.split())
    for required in ("不在本仓库", "无法核对", "不是公开文档"):
        assert required in flat, (
            f"the index no longer says {required!r} - without it a reader may "
            "mistake this mapping for the original feature list"
        )
