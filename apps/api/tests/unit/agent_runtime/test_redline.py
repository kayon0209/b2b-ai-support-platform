"""The commercial-commitment detector: narrow, and tested on both sides.

This decides whether an already-generated answer is allowed to reach a
customer. It had no tests at all, and it was behind a default-off flag, so
nothing had ever exercised it - a guard whose failure is unobservable is worth
exactly as much as one that was never written.

The detector needs a commitment verb *and* a commercial object in the same
sentence. Both halves are tested, because each direction is a real failure:
missing a promise ships a liability, and firing on a mere mention makes the
platform refuse to answer questions about its own price list.
"""

import pytest

from platform_core.agent_runtime.qa_path import redline_violations


@pytest.mark.parametrize(
    "text",
    [
        "我们保证交期 7 天。",
        "我方承诺这个价格不变。",
        "We guarantee delivery by Friday.",
        "I can assure you the refund amount will be credited tomorrow.",
    ],
)
def test_a_commitment_about_a_commercial_object_is_caught(text: str) -> None:
    assert redline_violations(text) != []


@pytest.mark.parametrize(
    "text",
    [
        # The commercial object with no promise: stating how pricing works is
        # the documented L1 answer, and blocking it would refuse a customer
        # the answer the knowledge base exists to give.
        "标准交期以报价单为准。",
        "价格根据板厚与数量计算，详见在线计价工具。",
        "The price list is versioned; see the current schedule.",
        # A promise about something that is not commercial.
        "我们保证会尽快回复您。",
        "",
    ],
)
def test_a_mention_without_a_promise_is_allowed(text: str) -> None:
    assert redline_violations(text) == []


def test_detection_is_per_sentence_not_per_answer() -> None:
    """One bad sentence does not condemn the paragraph, and is named.

    The violating sentence is returned so a reviewer can judge it by eye
    rather than re-running the model.
    """
    text = "价格以报价单为准。我们保证交期 7 天。"

    violations = redline_violations(text)

    assert violations == ["我们保证交期 7 天"]
