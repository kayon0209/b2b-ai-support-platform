"""Feature list 3.8: tolerate typos in the intent layer.

The retrieval layer already tolerates a misspelling (it matches on terms and
vectors, either of which survives one wrong character). The intent layer does
not: it matches regular expressions against the customer's exact words, so
"定单" instead of "订单" fails to match the order vocabulary entirely and the
question gets routed by whatever else it happens to contain - or by nothing.

**This corrects only a fixed table of homophone substitutions.** It is not a
general spell corrector, and that restraint is the design:

- A general corrector has to guess, and a wrong guess changes the customer's
  meaning. "交货" corrected to "交期" would turn a question about delivery into
  one about lead time, and the platform would answer confidently about the
  wrong thing.
- The table is data, so it can be reviewed, extended per tenant, and diffed.
  The alternative - a model rewriting the query before classification - is a
  step that cannot be audited, on the path that decides what the customer is
  asking.

Scope is deliberately narrow: only strings where the substitution is a
homophone of a word this product actually uses, and only where the misspelling
is not itself a valid word. "开发票" is left alone because it is correct.

**The correction is used for matching, never shown to the customer and never
stored as their words.** `normalize_for_matching` returns the corrected text
for the classifier; the original is what the audit log keeps.
"""

from __future__ import annotations

# Misspelling -> intended word. All pairs are homophones in Mandarin, which is
# why they occur: pinyin input offers the wrong character and the customer
# takes the first suggestion. A pair that is not a homophone would be a
# different claim (a typo, not a homophone), and those are not in scope here.
HOMOPHONE_FIXES: dict[str, str] = {
    "定单": "订单",
    "付宽": "付款",
    "退宽": "退款",
    "发或": "发货",
    "价各": "价格",
    "联细": "联系",
    "客福": "客服",
    "开飘": "开票",
    "物留": "物流",
    "帐单": "账单",
    "版材": "板材",
    "阻亢": "阻抗",
    "线经": "线径",
    "打样": "打样",  # correct; kept explicit so a future edit cannot "fix" it
    "货其": "货期",
    "交负": "交付",
    "质包": "质保",
    "报驾": "报价",
    "批好": "批号",
    "装相": "装箱",
}


def _is_identity(misspelling: str, intended: str) -> bool:
    return misspelling == intended


def normalize_for_matching(text: str) -> str:
    """`text` with known homophone misspellings replaced.

    Returns the input unchanged when there is nothing to fix, so a caller can
    use it unconditionally. Longest key first, so a longer correction is not
    pre-empted by a shorter one that overlaps it.
    """
    if not text:
        return text
    result = text
    for misspelling in sorted(HOMOPHONE_FIXES, key=len, reverse=True):
        intended = HOMOPHONE_FIXES[misspelling]
        if _is_identity(misspelling, intended):
            continue
        if misspelling in result:
            result = result.replace(misspelling, intended)
    return result


def corrections_in(text: str) -> tuple[tuple[str, str], ...]:
    """Which substitutions `text` would incur, for the audit record.

    A correction that changes routing is a decision the platform made about the
    customer's words, so it has to be visible afterwards - otherwise "why was
    this routed to sales" has no answer when the input says 订单 and the log
    says 定单.
    """
    if not text:
        return ()
    found: list[tuple[str, str]] = []
    for misspelling in sorted(HOMOPHONE_FIXES, key=len, reverse=True):
        intended = HOMOPHONE_FIXES[misspelling]
        if _is_identity(misspelling, intended):
            continue
        if misspelling in text:
            found.append((misspelling, intended))
    return tuple(found)


__all__ = ["HOMOPHONE_FIXES", "corrections_in", "normalize_for_matching"]
