"""The confirmation detector: narrow on purpose, and tested on both sides.

This decides whether the platform acts on a message that carries no verb and
no object, so the failures worth testing are the ones where it says yes to
something that was not assent.
"""

import pytest

from platform_core.agent_runtime.confirmation import is_confirmation


@pytest.mark.parametrize(
    "message",
    [
        "确认",
        "确认，按这个方案生产",
        "好的，确认",
        "同意",
        "可以",
        "没问题，就按这个来",
        "批准",
        "收到",
        "confirmed",
        "Confirmed, go ahead",
        "yes please proceed",
        "OK",
        "okay",
        "sure, looks good",
        "approved",
        "LGTM",
    ],
)
def test_assent_is_recognised(message: str) -> None:
    assert is_confirmation(message) is True


@pytest.mark.parametrize(
    "message",
    [
        # Negation. Contains the affirmation substring and means the opposite,
        # which is why the negation check runs first and wins.
        "不确认",
        "先不确认",
        "no",
        "not yet",
        "please don't",
        "取消",
        "拒绝",
        "暂缓",
        "hold on",
        # A question. "可以吗？" contains 可以 and is asking, not agreeing.
        "可以吗？",
        "is that ok?",
        "确认了吗？",
        # Not a confirmation at all.
        "",
        "我们的板子什么时候能发货？",
        "what is the refund window?",
    ],
)
def test_non_assent_is_refused(message: str) -> None:
    assert is_confirmation(message) is False


def test_a_long_message_containing_an_affirmation_is_not_assent() -> None:
    """A confirmation is a sentence, not a paragraph.

    Reading a long explanation as assent because it happens to contain "ok" is
    how a platform agrees on a customer's behalf - and this is the guard that
    keeps the detector from firing on ordinary conversation that mentions
    agreement in passing.
    """
    long_message = (
        "Thanks for the update. I have forwarded this to our engineering team "
        "and we will discuss whether we can accept the revised stackup at "
        "ok the meeting next week, but I cannot confirm anything today."
    )
    assert len(long_message) > 80
    assert is_confirmation(long_message) is False


def test_an_affirmation_inside_a_longer_request_is_not_assent() -> None:
    """The detector answers "is this message assent", not "does it contain a
    yes"."""
    assert (
        is_confirmation("ok but please also update the delivery address to the Shenzhen office")
        is False
    )
