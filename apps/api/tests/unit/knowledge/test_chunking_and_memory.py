"""Unit tests: chunking config, overlap, tables, cleaning, front matter,
colloquial normalization and entity-protected query rewriting (plan 1.1/
1.4/1.6/4.6)."""

from __future__ import annotations

import pytest

from platform_core.agent_runtime.conversation import (
    Turn,
    TurnRole,
    normalize_colloquial,
    rewrite_query,
)
from platform_core.knowledge.ingest import (
    ChunkingConfig,
    Section,
    chunk_sections,
    clean_text,
    dedupe_chunks,
    extract_front_matter,
)

# --- ChunkingConfig ---------------------------------------------------------


def test_overlap_at_or_above_max_is_rejected() -> None:
    with pytest.raises(ValueError):
        ChunkingConfig(max_chars=100, min_chars=10, overlap_chars=100)


def test_max_below_min_is_rejected() -> None:
    with pytest.raises(ValueError):
        ChunkingConfig(max_chars=40, min_chars=50)


def test_negative_overlap_is_rejected() -> None:
    with pytest.raises(ValueError):
        ChunkingConfig(overlap_chars=-1)


# --- Overlap ----------------------------------------------------------------


def _long_section(paragraphs: int = 8) -> Section:
    return Section(
        path=["Doc"],
        text="\n\n".join(f"Paragraph {i} " + "x" * 40 for i in range(paragraphs)),
    )


def test_overlap_carries_previous_tail_into_next_chunk() -> None:
    config = ChunkingConfig(max_chars=300, min_chars=10, overlap_chars=120)
    chunks = chunk_sections([_long_section()], config)
    assert len(chunks) > 1
    # The tail paragraphs of chunk N appear at the head of chunk N+1, in
    # order - a shared region, not a single duplicated line.
    last_tail = chunks[0].text.split("\n\n")[-1]
    assert chunks[1].text.startswith(chunks[0].text.split("\n\n")[-2][:80])
    assert last_tail in chunks[1].text


def test_zero_overlap_produces_disjoint_chunks() -> None:
    config = ChunkingConfig(max_chars=300, min_chars=10, overlap_chars=0)
    chunks = chunk_sections([_long_section()], config)
    assert len(chunks) > 1
    assert chunks[0].text.split("\n\n")[-1] not in chunks[1].text


def test_same_config_is_reproducible() -> None:
    config = ChunkingConfig(max_chars=600, min_chars=10, overlap_chars=100)
    first = [c.text for c in chunk_sections([_long_section()], config)]
    second = [c.text for c in chunk_sections([_long_section()], config)]
    assert first == second


def test_different_config_produces_different_chunks() -> None:
    long = _long_section()
    a = [c.text for c in chunk_sections([long], ChunkingConfig(600, 10, 0))]
    b = [c.text for c in chunk_sections([long], ChunkingConfig(300, 10, 0))]
    assert a != b


def test_single_overlong_paragraph_is_flagged_not_dropped() -> None:
    config = ChunkingConfig(max_chars=200, min_chars=10, overlap_chars=0)
    section = Section(path=["Doc"], text="Sentence one. " * 40)
    chunks = chunk_sections([section], config)
    assert all(c.meta.get("oversized") for c in chunks)
    assert all(len(c.text) <= 200 for c in chunks)


# --- Tables (plan 1.6) ------------------------------------------------------


def test_table_block_is_atomic_and_marked() -> None:
    table = "[TABLE]\n| Plan | Window |\n| --- | --- |\n| Annual | 30 days |\n[/TABLE]"
    filler = "Prose around the table. " * 60
    section = Section(path=["Warranty"], text=filler + "\n\n" + table + "\n\n" + filler)
    config = ChunkingConfig(max_chars=600, min_chars=10, overlap_chars=100)
    chunks = chunk_sections([section], config)
    table_chunks = [c for c in chunks if c.meta.get("content_kind") == "table"]
    assert len(table_chunks) == 1
    assert table_chunks[0].text.count("Annual") == 1
    assert table_chunks[0].text.startswith("[TABLE]")
    # No prose chunk gained a table tail via overlap.
    for chunk in chunks:
        if chunk.meta.get("content_kind") != "table":
            assert "[TABLE]" not in chunk.text


# --- Cleaning (plan 4.6) ----------------------------------------------------


def test_clean_text_strips_page_numbers_and_reports() -> None:
    raw = "Header text\n\n12\nPage 3/9\n\nBody continues here"
    cleaned, report = clean_text(raw)
    assert "12" not in cleaned.splitlines()
    assert "Page 3/9" not in cleaned
    assert report.boilerplate_lines_removed == 2


def test_clean_text_can_be_disabled() -> None:
    raw = "Header\n\n7\nBody"
    cleaned, report = clean_text(raw, strip_boilerplate=False)
    assert "7" in cleaned
    assert report.boilerplate_lines_removed == 0


def test_dedupe_drops_repeat_chunks_keeps_first() -> None:
    a = Section(path=["A"], text="The refund window is 30 days.")
    b = Section(path=["B"], text="the  refund  window  is  30  days.")  # same, noisy
    c = Section(path=["C"], text="Shipping takes two weeks.")
    kept, dropped = dedupe_chunks([a, b, c])
    assert dropped == 1
    assert [chunk.path for chunk in kept] == [["A"], ["C"]]


# --- Front matter (plan 1.5) ------------------------------------------------


def test_front_matter_is_split_and_lowercased() -> None:
    body, meta = extract_front_matter("---\nmodel: EC-500\nRegion: EMEA\n---\n\nBody text")
    assert meta == {"model": "ec-500", "region": "emea"}
    assert body.startswith("Body text")


def test_no_front_matter_is_indifferent() -> None:
    body, meta = extract_front_matter("Just a body")
    assert meta == {}
    assert body == "Just a body"


# --- Colloquial normalization (plan 1.4) ------------------------------------


def test_normalization_appends_canonical_term() -> None:
    aliases = [("the wifi box", "gateway", 1.0)]
    result, applied = normalize_colloquial("The wifi box keeps dropping", aliases)
    assert applied == ["gateway"]
    assert result.endswith("gateway")


def test_normalization_is_word_bounded_for_latin() -> None:
    aliases = [("ec", "error code", 1.0)]
    result, applied = normalize_colloquial("EC-504 after update", aliases)
    # "EC" inside "EC-504" is not the alias; nothing fires.
    assert applied == []
    assert result == "EC-504 after update"


def test_normalization_skips_when_canonical_already_present() -> None:
    aliases = [("the wifi box", "gateway", 1.0)]
    result, applied = normalize_colloquial("The wifi box gateway setup", aliases)
    assert applied == []
    assert result == "The wifi box gateway setup"


def test_normalization_matches_an_uppercase_alias() -> None:
    """The defect this pins: the query is case-folded, the alias was not.

    Every alias carrying an uppercase ASCII letter matched nothing at all -
    "V割" against "v割 ...", "EQ", "MI", "TGZ". That is the whole
    abbreviation vocabulary of a PCB corpus, so the alias table looked
    seeded and rewrote nothing. Existing cases used only lowercase aliases,
    which is why it survived.
    """
    assert normalize_colloquial("V割 怎么做", [("V割", "V-CUT", 1.0)])[1] == ["V-CUT"]
    assert normalize_colloquial("eq 还没确认", [("EQ", "工程确认", 1.0)])[1] == ["工程确认"]
    assert normalize_colloquial("MI 参数改一下", [("MI", "制作指示", 1.0)])[1] == ["制作指示"]


def test_uppercase_latin_alias_stays_word_bounded() -> None:
    """Case-folding must not loosen the whole-token rule for latin aliases."""
    assert normalize_colloquial("TGZX package", [("TGZ", "工程文件包", 1.0)])[1] == []


# --- Entity protection (plan 1.4) -------------------------------------------


def test_rewrite_preserves_model_numbers_verbatim() -> None:
    prior = [Turn(role=TurnRole.CUSTOMER, text="My EC-504 gateway shows error code 7")]
    query, rewritten = rewrite_query("issues?", prior)
    assert rewritten
    assert "EC-504" in query  # verbatim, not stemmed or dropped


def test_rewrite_without_entities_still_adds_subject() -> None:
    prior = [Turn(role=TurnRole.CUSTOMER, text="What is the annual plan refund window?")]
    query, rewritten = rewrite_query("exclusions?", prior)
    assert rewritten
    assert "refund" in query
