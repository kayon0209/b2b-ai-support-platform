"""Feature list 5.2: the same answer, dressed for the channel.

The assertions that carry the design:

- **Format changes; the claim does not.** A plain-text channel loses the
  asterisks, not the sentence. Any adapter that could silently drop a clause
  is a correctness bug wearing a formatting costume.
- **Unknown channels fall to plain text.** Sending `**bold**` to a surface that
  does not render it shows the customer the asterisks; sending plain text to a
  rich surface only loses emphasis. The failure must fall on the harmless side.
- **Truncation is at a sentence boundary and is visible.** A silent mid-sentence
  cut reads as a broken platform, and can remove the half of a sentence that
  carried the meaning.
"""

from __future__ import annotations

from platform_core.support_bridge.channel_format import (
    Channel,
    adapt_for_channel,
    spec_for,
    strip_markdown,
)


def test_plain_text_channel_loses_markup_but_keeps_the_words() -> None:
    text = "**交期**是 5 个工作日，依据 `价目表 v3.2`。"
    adapted = adapt_for_channel(text, channel="sms")
    assert "**" not in adapted and "`" not in adapted
    assert "交期" in adapted and "5 个工作日" in adapted and "价目表 v3.2" in adapted


def test_markdown_channel_keeps_its_markup() -> None:
    text = "**交期**是 5 个工作日"
    assert adapt_for_channel(text, channel="email") == text


def test_an_unknown_channel_degrades_to_plain_text() -> None:
    """Guessing rich shows asterisks to the customer; guessing plain is safe."""
    adapted = adapt_for_channel("**bold**", channel="some-new-channel")
    assert "*" not in adapted
    assert spec_for("some-new-channel").supports_markdown is False


def test_a_missing_channel_is_treated_as_unknown_not_as_rich() -> None:
    assert spec_for(None).supports_markdown is False
    assert spec_for("").supports_markdown is False


def test_links_stay_visible_when_markup_is_stripped() -> None:
    """A URL is the one thing a customer must be able to act on."""
    adapted = strip_markdown("详见[报价说明](https://example.com/quote)")
    assert "https://example.com/quote" in adapted


def test_short_text_passes_through_unchanged() -> None:
    text = "您的订单已发货。"
    assert adapt_for_channel(text, channel="sms") == text


def test_long_text_is_truncated_at_a_sentence_boundary() -> None:
    sentence = "第一句话结束了。第二句话也结束了。第三句话会被截掉。"
    text = sentence * 20  # comfortably past the SMS ceiling
    adapted = adapt_for_channel(text, channel="sms")
    assert len(adapted) <= 320
    assert adapted.startswith("第一句话结束了。")
    assert adapted.endswith("…")
    # Ends at a boundary, so the kept part is a whole number of sentences: the
    # character before the ellipsis is one a sentence could end on.
    assert adapted[-2] in "。！？；.!"


def test_truncation_never_silently_drops_without_a_marker() -> None:
    text = "很长的内容。" * 100
    adapted = adapt_for_channel(text, channel="sms")
    assert adapted.endswith("…")


def test_a_decimal_version_number_is_not_a_sentence_boundary() -> None:
    """The product-specific trap: 价目表 v3.2 is a price basis, not a full stop."""
    text = "依据价目表 v3.2 的规则计算。" + "补充说明。" * 40
    adapted = adapt_for_channel(text, channel="sms")
    assert "v3.2 的规则计算。" in adapted


def test_markdown_is_stripped_before_measuring_the_limit() -> None:
    """Counting characters that will not be sent would cut the message short."""
    heavy = "*" * 2 + "内容" + "*" * 2
    spec = spec_for("sms")
    assert spec.max_chars >= len(adapt_for_channel(heavy * 20, channel="sms"))


def test_each_channel_has_its_own_ceiling() -> None:
    assert spec_for("sms").max_chars < spec_for("wechat").max_chars
    assert spec_for("wechat").max_chars < spec_for("email").max_chars


def test_the_channel_enum_names_are_stable() -> None:
    """They arrive from channel metadata; renaming silently downgrades a
    channel to the unknown default."""
    assert Channel.SMS.value == "sms"
    assert Channel.EMAIL.value == "email"
    assert Channel.WECHAT.value == "wechat"
    assert Channel.WEB_CHAT.value == "web_chat"


def test_channel_matching_is_case_and_space_insensitive() -> None:
    assert spec_for("  SMS  ").max_chars == spec_for("sms").max_chars
