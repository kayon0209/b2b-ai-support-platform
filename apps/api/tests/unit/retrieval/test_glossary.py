"""Feature list 3.6: the glossary bridges languages, and is insertable as-is.

The assertions that matter are about the two ways a glossary breaks silently:

- **A duplicate alias is a failed insert, not a resolved conflict.**
  `knowledge_aliases` has UNIQUE (tenant_id, alias), because one surface form
  meaning two things makes query expansion a coin flip. A glossary with a
  duplicate would therefore fail partway through seeding, leaving a
  half-installed glossary that nobody notices until recall is wrong.
- **The bridge is directional, so a one-way entry only helps one language.**
  Symmetric coverage is asserted for the terms a mixed-language conversation
  actually uses; the intentionally one-way aliases are not counted as if they
  bridged both ways.
"""

from __future__ import annotations

from collections import Counter

from platform_core.retrieval.glossary import concepts, glossary_rows


def test_rows_have_the_shape_the_loader_returns() -> None:
    """`load_aliases` yields (alias, term, weight) - a seeder inserts these."""
    for row in glossary_rows():
        assert len(row) == 3
        alias, term, weight = row
        assert isinstance(alias, str) and alias
        assert isinstance(term, str) and term
        assert isinstance(weight, float)


def test_an_alias_never_maps_to_itself() -> None:
    """A self-map expands a query into itself, which is pure noise."""
    for alias, term, _weight in glossary_rows():
        assert alias != term, alias


def test_no_alias_appears_twice() -> None:
    """The guard: UNIQUE (tenant_id, alias) would reject the second insert."""
    counts = Counter(alias for alias, _term, _weight in glossary_rows())
    duplicates = [alias for alias, seen in counts.items() if seen > 1]
    assert duplicates == [], f"aliases repeated: {duplicates}"


def test_every_weight_is_positive() -> None:
    """Zero or negative would make the row inert or adversarial in fusion."""
    for _alias, _term, weight in glossary_rows():
        assert weight > 0


def test_the_bridge_is_symmetric_for_core_terms() -> None:
    """A one-way entry only helps one language - the point is both."""
    rows = {(alias, term) for alias, term, _ in glossary_rows()}
    for chinese, english in (
        ("阻抗", "impedance"),
        ("钢网", "stencil"),
        ("回流焊", "reflow"),
        ("交期", "lead time"),
        ("替代料", "substitute"),
        ("封装", "footprint"),
    ):
        assert (chinese, english) in rows, f"missing {chinese} -> {english}"
        assert (english, chinese) in rows, f"missing {english} -> {chinese}"


def test_generic_terms_are_weighted_below_specific_ones() -> None:
    """'BOM' names a document form, not a subject - it must not dominate."""
    weights = {alias: weight for alias, _term, weight in glossary_rows()}
    assert weights["bom"] < weights["impedance"]
    assert weights["bom清单"] < weights["阻抗"]


def test_concepts_counts_unordered_pairs_not_rows() -> None:
    """Reporting rows as concepts would double the apparent coverage."""
    assert concepts() < len(glossary_rows())
    assert concepts() > 0


def test_the_glossary_actually_expands_a_mixed_language_query() -> None:
    """End to end through the retrieval helper the platform already uses."""
    from platform_core.retrieval.hybrid import expand_with_aliases

    expanded, applied = expand_with_aliases("impedance 控制怎么做", list(glossary_rows()))
    assert "阻抗" in applied
    assert "阻抗" in expanded


def test_expansion_keeps_the_customers_own_words() -> None:
    """Additive, like the rest of query rewriting: nothing is replaced."""
    from platform_core.retrieval.hybrid import expand_with_aliases

    expanded, _applied = expand_with_aliases("stencil 什么时候好", list(glossary_rows()))
    assert "stencil" in expanded
    assert "钢网" in expanded
