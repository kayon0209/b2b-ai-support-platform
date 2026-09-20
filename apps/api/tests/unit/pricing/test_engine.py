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


def test_the_service_declines_until_a_real_table_is_supplied() -> None:
    """The production path, in one assertion.

    `service.RULES` ships empty, so a fully-specified customer request still
    produces nothing and goes to a person. This is the guard against someone
    "helpfully" adding a sample price table on the way to production.
    """
    from platform_core.pricing import service

    assert service.RULES.version == "unconfigured"
    assert service.RULES.layers == {}
    assert service.quote_from_text("4层板 100x80mm 板厚1.6mm 沉金 500片多少钱？") is None


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
