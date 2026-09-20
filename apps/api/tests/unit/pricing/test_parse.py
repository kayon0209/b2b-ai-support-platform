"""Parameter extraction: complete requests only, never guessed.

The refusal cases are the point. A quantity filled in from a default would
produce a confident price for an order nobody described.
"""

import pytest

from platform_core.pricing.parse import parse_quote_request


def test_a_fully_specified_request_is_parsed() -> None:
    req = parse_quote_request("你好，4层板 100x80mm 板厚1.6mm 沉金 500片，请问多少钱？")

    assert req is not None
    assert req.layers == 4
    assert req.length_mm == 100.0
    assert req.width_mm == 80.0
    assert req.thickness_mm == 1.6
    assert req.surface_finish == "ENIG"
    assert req.quantity == 500
    assert req.lead_time_tier == "standard"


def test_expedite_is_recognised() -> None:
    req = parse_quote_request("2层 100x100 板厚1.6mm 喷锡 100片 加急")

    assert req is not None
    assert req.lead_time_tier == "expedite"


@pytest.mark.parametrize(
    "text",
    [
        "4层板多少钱？",  # no dimensions, no quantity
        "100x80mm 500片 沉金",  # no layer count
        "4层 100x80 板厚1.6mm 沉金",  # no quantity
        "4层 100x80 500片 板厚1.6mm",  # no surface finish
        "4层 100x80 500片 沉金",  # no thickness
        "请问做个板子贵不贵",
    ],
)
def test_an_incomplete_request_is_not_guessed_at(text: str) -> None:
    assert parse_quote_request(text) is None


def test_a_bare_millimetre_figure_is_not_read_as_thickness() -> None:
    """ "1.6mm" alone is usually a dimension in this trade, not a board
    thickness. Guessing would feed a wrong multiplier into the price."""
    assert parse_quote_request("4层 100x80 1.6mm 500片 沉金") is None
