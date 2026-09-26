"""Is this message agreeing to something the platform is waiting on?

A confirmation is not a write request and never classifies as one. "确认" on
its own carries no verb, no object and no intent to act; it means "proceed"
only in a conversation that is *waiting* for it. So this answers a narrower
question than `intent.classify` does, and it is never consulted on its own -
the caller pairs it with the context that makes the answer meaningful (an EQ
case in `waiting_customer`, linked to this conversation).

Deliberately lexical, for the same reason tool selection is: this decides
whether the platform acts, and a non-deterministic chooser would make the audit
trail describe a coin flip.

Deliberately narrow in three ways, because the failure mode is proposing a
confirmation the customer never gave:

- **short**. A confirmation is a sentence, not a paragraph. A long message that
  happens to contain "ok" is someone explaining something, and reading it as
  assent is how a platform agrees on a customer's behalf.
- **no negation**. "不确认" contains "确认" and means the opposite. The negation
  check runs first and wins.
- **not a question**. "可以吗？" contains "可以" and is asking, not agreeing.
"""

from __future__ import annotations

import re

# Whole-token matches for Latin script, so "okay" is matched by "okay" and not
# by a substring of something else.
_AFFIRMATIONS = frozenset(
    {
        "yes",
        "yep",
        "yeah",
        "ok",
        "okay",
        "sure",
        "confirm",
        "confirmed",
        "agree",
        "agreed",
        "approve",
        "approved",
        "correct",
        "right",
        "proceed",
        "go ahead",
        "looks good",
        "lgtm",
    }
)

# Substring matches for Chinese, which has no word boundaries to anchor on.
# Kept to phrases rather than single characters: 行 and 是 appear inside far too
# many unrelated words (银行, 行业, 但是) to be a signal.
_AFFIRMATIONS_CJK = (
    "确认",
    "同意",
    "可以",
    "好的",
    "没问题",
    "批准",
    "就按",
    "按这个",
    "按此",
    "收到",
)

# Checked before the affirmations above and never overridden.
_NEGATIONS = frozenset(
    {
        "no",
        "not",
        "don't",
        "dont",
        "cancel",
        "hold",
        "wait",
        "reject",
        "decline",
        "stop",
        "later",
        "unsure",
    }
)
_NEGATIONS_CJK = ("不", "别", "先不", "等等", "取消", "拒绝", "暂缓", "再等")

# A confirmation is one short clause. 48 characters covers every realistic
# form - "确认" (2), "确认，按这个方案生产" (10), "Confirmed, go ahead" (19),
# "Yes, please proceed with production" (37) - and excludes a sentence that is
# also asking for something else.
#
# The threshold started at 80 and a test caught the gap immediately:
# "ok but please also update the delivery address to the Shenzhen office" is 76
# characters, contains "ok", and is not assent - it is a different request with
# a courtesy in front. Length is a proxy for "this message is only agreeing",
# and a loose proxy here means confirming on the customer's behalf.
_MAX_CHARS = 48

_QUESTION_MARKS = ("?", "？")
_LATIN_WORD = re.compile(r"[a-z']+")


def is_confirmation(question: str) -> bool:
    """True when the message reads as assent to something already asked.

    Only ever meaningful with context: the same text in a conversation that is
    not waiting on the customer is an ordinary pleasantry, and the caller is
    responsible for not asking this question then.
    """
    text = question.strip().lower()
    if not text or len(text) > _MAX_CHARS:
        return False
    if text.endswith(_QUESTION_MARKS):
        return False
    if any(term in text for term in _NEGATIONS_CJK):
        return False

    words = set(_LATIN_WORD.findall(text))
    if words & _NEGATIONS:
        return False

    if any(term in text for term in _AFFIRMATIONS_CJK):
        return True
    if words & _AFFIRMATIONS:
        return True
    # Multi-word English forms ("go ahead", "looks good") are phrases, so they
    # are matched against the collapsed text rather than the token set.
    collapsed = " ".join(_LATIN_WORD.findall(text))
    return any(phrase in collapsed for phrase in ("go ahead", "looks good"))
