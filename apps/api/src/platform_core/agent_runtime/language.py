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

# Kana, CJK ideographs (extensions A and unified) and Hangul. Deliberately
# wider than Han characters alone - a Japanese or Korean customer is not
# writing English either, and the fallback for them is the same text a Chinese
# customer gets, which is closer to right than the English would be.
_CJK = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af]")


def answers_in_chinese(question: str | None) -> bool:
    """Whether this customer's words are written in a CJK script."""
    return _CJK.search(question or "") is not None


__all__ = ["answers_in_chinese"]
