"""Feature list 5.2: adapt an answer to the channel it is sent on.

The rule that shapes everything here: **adapting the format must never change
the claim**. Trimming Markdown asterisks is formatting; dropping the sentence
that names the source is not. A customer on SMS and a customer on email must be
told the same thing, in different clothing.

Two consequences worth stating, because both are easy to get wrong:

- **An unknown channel degrades conservatively, to plain text.** Not to
  Markdown: a channel that does not render `**bold**` shows the asterisks to
  the customer, so guessing "rich" produces visibly broken output, while
  guessing "plain" merely loses emphasis. The failure has to fall on the
  harmless side.
- **Truncation happens at a sentence boundary and is marked.** Cutting
  mid-sentence reads as a broken system rather than a length limit, and a
  silent cut can remove the half of a sentence that carried the meaning.
  Cutting at a boundary with an ellipsis says "there was more" without
  pretending the remainder never existed.

Where the channel is known (`Chatwoot` inboxes map 1:1 to a channel), the
caller passes it; where it is not, the default applies. The spec table is data
rather than branching so adding a channel is one row, not a new `if`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum


class Channel(StrEnum):
    """Delivery surfaces the platform can send on."""

    WEB_CHAT = "web_chat"
    EMAIL = "email"
    WECHAT = "wechat"
    SMS = "sms"


@dataclass(frozen=True)
class ChannelSpec:
    """What one channel can carry."""

    max_chars: int
    supports_markdown: bool


# Character ceilings are the channel's own limits, not style preferences:
# SMS is 2 concatenated segments (the point where handsets start splitting),
# WeChat truncates long bubbles, email is bounded so one answer cannot become
# a wall, and web chat keeps the richest budget because the panel scrolls.
_CHANNEL_SPECS: dict[Channel, ChannelSpec] = {
    Channel.SMS: ChannelSpec(max_chars=320, supports_markdown=False),
    Channel.WECHAT: ChannelSpec(max_chars=1000, supports_markdown=False),
    Channel.WEB_CHAT: ChannelSpec(max_chars=4000, supports_markdown=True),
    Channel.EMAIL: ChannelSpec(max_chars=8000, supports_markdown=True),
}

# Unknown channel: plain text, and short enough to survive the narrowest
# surface. See the module docstring for why "safe" means plain rather than
# rich.
_UNKNOWN_SPEC = ChannelSpec(max_chars=1000, supports_markdown=False)

_MARKDOWN_PATTERNS = (
    (re.compile(r"\*\*(.+?)\*\*", re.S), r"\1"),  # bold
    (re.compile(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", re.S), r"\1"),  # italic
    (re.compile(r"`(.+?)`", re.S), r"\1"),  # inline code
    (re.compile(r"^\s{0,3}#{1,6}\s*", re.M), ""),  # headings
    (re.compile(r"^\s{0,3}[-*+]\s+", re.M), "· "),  # bullets stay readable
    (re.compile(r"\[(.+?)\]\((.*?)\)", re.S), r"\1 (\2)"),  # keep the URL visible
)

# A sentence ends at Chinese punctuation (which needs no trailing space), or at
# Latin punctuation followed by whitespace/end. The `(?<!\d)` guard matters for
# this product specifically: "价目表 v3.2" and "1.6mm 板厚" are ordinary answers
# here, and treating that dot as a sentence end would truncate a price basis in
# half - the one thing 4B.5 exists to preserve.
_SENTENCE_END = re.compile(r"[。！？；]|(?<!\d)[.!?;](?=\s|$)")


def spec_for(channel: str | None) -> ChannelSpec:
    """The spec for a channel name, or the conservative default.

    Takes a string rather than the enum because channel identifiers arrive from
    channel metadata, where a typo or a new channel must not raise in the
    middle of a reply.
    """
    if not channel:
        return _UNKNOWN_SPEC
    try:
        return _CHANNEL_SPECS[Channel(channel.strip().lower())]
    except ValueError:
        return _UNKNOWN_SPEC


def strip_markdown(text: str) -> str:
    """Remove markup that a plain-text channel would show literally.

    Deliberately keeps list markers (as `·`) and bare URLs: they carry meaning
    a customer needs, and a URL is the one thing that must survive intact.
    """
    result = text
    for pattern, replacement in _MARKDOWN_PATTERNS:
        result = pattern.sub(replacement, result)
    return result


def _truncate_at_boundary(text: str, limit: int) -> str:
    """Cut to at most `limit` chars, preferring the last sentence boundary.

    The ellipsis is budgeted *inside* the limit rather than appended after it.
    Getting that wrong is not cosmetic: a body one character over an SMS
    ceiling becomes a second segment, and one character over WeChat's limit is
    what the channel truncates - with no ellipsis to say so.

    Falls back to a hard cut when the first sentence alone is longer than the
    limit, because the alternative is returning nothing at all.
    """
    if len(text) <= limit:
        return text
    # One character reserved: the ellipsis itself counts against the channel.
    window = text[: max(0, limit - 1)]
    boundary = 0
    for match in _SENTENCE_END.finditer(window):
        boundary = match.end()
    if boundary:
        return window[:boundary].rstrip()[: limit - 1] + "…"
    return window.rstrip() + "…"


def adapt_for_channel(text: str, *, channel: str | None) -> str:
    """`text` as it should be sent on `channel`.

    Order matters: truncation is measured on what will actually be delivered,
    so Markdown is stripped first - counting characters that will not be sent
    would cut a message shorter than the channel allows.
    """
    spec = spec_for(channel)
    adapted = text if spec.supports_markdown else strip_markdown(text)
    return _truncate_at_boundary(adapted, spec.max_chars)


__all__ = [
    "Channel",
    "ChannelSpec",
    "adapt_for_channel",
    "spec_for",
    "strip_markdown",
]
