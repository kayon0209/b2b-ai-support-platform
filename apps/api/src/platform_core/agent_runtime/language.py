"""Which language a customer is being answered in.

Its own module because three customer-visible surfaces need the answer and they
must not disagree: the abstention and outage notices (`qa_path`), the
out-of-hours notice (`hours`) and the queue notice (`queue_status`). Before
this existed, the copy was split three ways - some branches English-only, some
Chinese-only, some with no language rule at all - and a customer's experience
of it was Chinese, English and Chinese again inside one conversation.

**Script, not vocabulary.** Any CJK character means the customer is writing
Chinese. That is the same test the rest of the runtime already uses to decide
what counts as Chinese input (`intent._CN_OBJECT`, `conversation._CJK_CHAR`),
and it reads a mixed question ("PCB 交期?") as Chinese, which is right: that
customer is writing Chinese and quoting a part name.

**Not a setting.** Not a tenant field, not a header, not a request parameter.
One support window serves Chinese and English speakers, so a per-tenant switch
would only move the problem to whichever customers the tenant didn't pick, and
a client-supplied language field would be one more thing to validate against
the tenant. "Answer in the language the question was written in" needs neither.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

# Kana, CJK ideographs (extensions A and unified) and Hangul. Deliberately
# wider than Han characters alone - a Japanese or Korean customer is not
# writing English either, and the fallback for them is the same text a Chinese
# customer gets, which is closer to right than the English would be.
_CJK = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af]")


def answers_in_chinese(question: str | None) -> bool:
    """Whether this customer's words are written in a CJK script."""
    return _CJK.search(question or "") is not None


def conversation_is_chinese(texts: Iterable[str | None]) -> bool:
    """Whether a conversation is being held in Chinese, from all its messages.

    Not the same question as `answers_in_chinese`, and the difference was a
    customer-visible defect. That function asks about **one message**, so a
    message carrying no script signal at all - a bare identifier, a number, an
    English part code - is read as English.

    Measured 2026-09-23: a Chinese-speaking customer asked 我的订单到哪了？, was
    asked for the order number, and replied `SO-9001`. That reply contains no
    CJK character, so the system notice that followed was written in English,
    inside an otherwise entirely Chinese conversation - and the platform had
    *asked* for exactly that message. The rule "answer in the language the
    question was written in" is right; the mistake was applying it to the
    message that happens to be last rather than to the conversation.

    Any Chinese message makes the conversation Chinese: a customer who has
    written Chinese once is writing Chinese, and a later identifier does not
    change that.
    """
    return any(answers_in_chinese(text) for text in texts)


__all__ = ["answers_in_chinese", "conversation_is_chinese"]
