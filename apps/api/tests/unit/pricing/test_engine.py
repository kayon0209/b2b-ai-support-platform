"""The quoting engine: arithmetic, and above all refusal.

The refusals matter more than the arithmetic. A quoting engine that produces a
plausible number for something its table does not cover is worse than no
engine, because a price is the kind of figure that gets believed and acted on.
Every way of declining is therefore its own test.
"""

import pytest

from platform_core.pricing.engine import (
    LayerRule,
    QuoteBand,
    QuoteRequest,
    RuleSet,
    quote,
    quote_or_human_reason,
)

# A table that exists only for these tests. Deliberately not shipped anywhere:
# no fabricated prices reach production through this module.
_RULES = RuleSet(
    version="test-2026.09",
    currency="CNY",
    layers={
        (2, "standard"): LayerRule(
            setup_minor=3000,
            per_cm2_minor=40,
            breaks=((10, 1.0), (100, 0.85), (1000, 0.7)),
        ),
        (4, "standard"): LayerRule(setup_minor=5000, per_cm2_minor=70, breaks=((10, 1.0),)),
    },
    finishes={"HASL": 1.0, "ENIG": 1.25},
    thicknesses={1.6: 1.0, 2.0: 1.1},
    spread=0.1,
)


def _request(**overrides: object) -> QuoteRequest:
    base = {
        "layers": 2,
        "length_mm": 100.0,
        "width_mm": 100.0,
        "quantity": 100,
        "thickness_mm": 1.6,
        "surface_finish": "HASL",
    }
    base.update(overrides)
    return QuoteRequest(**base)  # type: ignore[arg-type]


def test_an_unconfigured_table_declines_everything() -> None:
    """The most important test in this file.

    No price table ships with the module, so in production every request
    declines and every quote goes to a human - which is 4B.4's rule rather
    than an unfinished feature. The failure mode being guarded against is a
    default table of plausible numbers.
    """
    result = quote(_request(), RuleSet())

    assert isinstance(result, type(quote(_request(), RuleSet())))  # narrowing aid
    assert not isinstance(result, QuoteBand)
    assert result.reason_code == "QUOTE_RULE_MISSING"


def test_a_configured_request_produces_a_band_with_its_basis() -> None:
    band = quote(_request(), _RULES)

    assert isinstance(band, QuoteBand)
    assert band.low_minor < band.high_minor, "a quote is a range, never an exact figure"
    assert band.currency == "CNY"
    # 4B.5: the basis travels with the number so it can be explained later.
    assert "version=test-2026.09" in band.basis
    assert "qty_break=" in band.basis


def test_a_larger_quantity_costs_less_per_board() -> None:
    """4B.3 阶梯价 - asserted on the per-board part, not the total, because
    the total obviously rises with quantity."""
    small = quote(_request(quantity=10), _RULES)
    large = quote(_request(quantity=1000), _RULES)
    assert isinstance(small, QuoteBand) and isinstance(large, QuoteBand)

    per_board_small = (small.low_minor + small.high_minor) / 2 / 10
    per_board_large = (large.low_minor + large.high_minor) / 2 / 1000

    assert per_board_large < per_board_small


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"layers": 8}, "QUOTE_RULE_MISSING"),  # not in the table
        ({"lead_time_tier": "expedite"}, "QUOTE_RULE_MISSING"),  # 加急 -> human
        ({"surface_finish": "OSP"}, "QUOTE_FINISH_UNKNOWN"),
        ({"thickness_mm": 3.2}, "QUOTE_THICKNESS_NONSTANDARD"),  # 非标
        ({"quantity": 1}, "QUOTE_QUANTITY_BELOW_MINIMUM"),
        ({"layers": 0}, "QUOTE_INPUT_INVALID"),
    ],
)
def test_anything_the_table_cannot_price_goes_to_a_person(
    overrides: dict[str, object], reason: str
) -> None:
    result = quote(_request(**overrides), _RULES)

    assert not isinstance(result, QuoteBand)
    assert result.reason_code == reason


def test_the_helper_returns_a_band_or_why_not() -> None:
    band, reason = quote_or_human_reason(_request(), _RULES)
    assert band is not None and reason == ""

    band, reason = quote_or_human_reason(_request(layers=8), _RULES)
    assert band is None and reason == "QUOTE_RULE_MISSING"

    assert pytest is not None


def test_the_shipped_table_carries_its_provenance() -> None:
    """A number without a source cannot be judged.

    The table is public industry data, not a contracted price list, so the
    version and the warning travel with every figure - otherwise it reads as
    this company's price.
    """
    from platform_core.pricing import reference, service

    assert service.RULES.version == reference.VERSION
    assert reference.PROVENANCE["source"]
    assert reference.PROVENANCE["retrieved"]
    assert reference.PROVENANCE["source_updated"]
    assert "not a contracted" in reference.PROVENANCE["basis_note"]

    label = service.quote_label("4层板 100x100mm 板厚1.6mm 沉金 500片多少钱？")
    assert label is not None
    assert "NON-CONTRACTUAL" in label


@pytest.mark.parametrize(
    ("question", "quantity", "published_low", "published_high"),
    [
        # Per-board figures published for a 100x100mm reference board, USD.
        ("2层板 100x100mm 板厚1.6mm 喷锡 100片多少钱？", 100, 1.80, 3.00),
        ("4层板 100x100mm 板厚1.6mm 沉金 500片多少钱？", 500, 3.00, 4.80),
    ],
)
def test_the_band_agrees_with_the_published_figures_it_came_from(
    question: str, quantity: int, published_low: float, published_high: float
) -> None:
    """The table is derived from public data, so check it against that data.

    Without this, "fitted to published figures" is just a comment. The band is
    for the whole order, so divide back down to a per-board figure first.
    """
    from platform_core.pricing import service

    band = service.quote_from_text(question)
    assert band is not None

    per_board_low = band.low_minor / 100.0 / quantity
    per_board_high = band.high_minor / 100.0 / quantity

    # Overlap, not containment: a band computed from range data is not
    # expected to sit neatly inside another range.
    assert per_board_low <= published_high, (per_board_low, published_high)
    assert per_board_high >= published_low, (per_board_high, published_low)


def test_non_standard_thickness_and_expedite_still_go_to_a_person() -> None:
    """4B.4 against the real table, not only against a test one.

    The public source publishes no thickness pricing at all and a +30-150%
    expedite range, so neither can be quoted honestly - both decline.
    """
    from platform_core.pricing import service

    assert service.quote_from_text("4层板 100x100mm 板厚2.0mm 沉金 500片多少钱？") is None
    assert service.quote_from_text(
        "4层板 100x100mm 板厚1.6mm 沉金 500片 加急多少钱？"
    ) is None


def test_quoting_can_be_switched_off_entirely(monkeypatch: pytest.MonkeyPatch) -> None:
    """For anyone who has not confirmed the figures."""
    from platform_core.config import get_settings
    from platform_core.pricing import service

    monkeypatch.setenv("APP_PRICING_RULESET", "empty")
    get_settings.cache_clear()
    try:
        assert service._load_rules().version == "unconfigured"
    finally:
        get_settings.cache_clear()


def test_with_a_table_supplied_the_service_returns_a_labelled_band(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """And when a real table exists, the label carries the basis with it."""
    from platform_core.pricing import service

    monkeypatch.setattr(service, "RULES", _RULES, raising=True)

    label = service.quote_label("4层板 100x80mm 板厚1.6mm 沉金 500片多少钱？")

    assert label is not None
    assert "quote_band=" in label
    assert "version=test-2026.09" in label
