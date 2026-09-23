"""Feature list 7.2: emotion detection, and 7.1's seventh handoff trigger.

Deliberately deterministic - a weighted lexicon, not a model call - for the
same reason pricing is (ADR 0007): this feeds a routing decision, and a
routing decision that cannot be reproduced cannot be argued with. A customer
who was escalated because a model "sensed frustration" has no explanation
coming; one escalated because they wrote "忍无可忍" has a quotable reason,
and the reason is recorded on the run as evidence.

What this is NOT:

- It does not change what the AI says. Angry customers get a human, not a
  warmer tone. Sentiment-driven tone adaptation is how a bot ends up
  apologising for something it has no authority to apologise for, which the
  commitment red line (6.1) exists to prevent - so the two do not interact.
- It is not a confidence score. Like the intent layer, levels are ordinal
  buckets from matched terms, not probabilities.

Levels: CALM < FRUSTRATED < ANGRY < ESCALATION_RISK. A text gets the highest
level any of its terms evidences, so one threat outranks ten mild gripes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum


class Emotion(StrEnum):
    """Ordinal buckets, strongest last."""

    CALM = "calm"
    FRUSTRATED = "frustrated"
    ANGRY = "angry"
    ESCALATION_RISK = "escalation_risk"


@dataclass(frozen=True)
class EmotionSignal:
    """The detected level plus the terms that produced it.

    The terms are kept so an operator reading the handoff can see *why* the
    run went to a person. A level with no quoted evidence is exactly the
    unexplained routing decision this module exists to avoid.
    """

    level: Emotion
    terms: tuple[str, ...]

    @property
    def should_handoff(self) -> bool:
        """Feature list 7.1, trigger 7 of 7.

        FRUSTRATED does not hand off on its own: a customer who has waited is
        annoying to talk to but is usually still answerable, and sending every
        impatient message to a queue is how a queue fills up. ANGRY and above
        is where the conversation has stopped being about the question.
        """
        return self.level in (Emotion.ANGRY, Emotion.ESCALATION_RISK)


# Escalation-risk vocabulary is external action: regulator, press, lawyer.
# It outranks anger because it is the one that needs a person *now* - an angry
# customer can be answered by a good answer, a customer quoting 12315 cannot.
_ESCALATION_RISK = re.compile(
    r"12315|工商|监管|消协|消费者协会|投诉到|曝光|媒体|记者|律师|起诉|法院|举报|仲裁"
    r"|监管部门|找你们领导|总部投诉"
    # English: the same external-action vocabulary. A bilingual corpus is not
    # a nice-to-have here - an English message threatening legal action is
    # exactly as urgent as a Chinese one, and missing it because the lexicon
    # was written in one language is the kind of gap that only surfaces when
    # the customer has already filed.
    r"|lawyer|attorney|sue|lawsuit|court|legal action|media|press|journalist"
    r"|regulator|ombudsman|consumer protection|report you|better business",
    re.I,
)

# Anger: the conversation has become about the company, not the question.
_ANGRY = re.compile(
    r"太差|垃圾|骗子|气死|忍无可忍|无法接受|荒唐|离谱|愤怒|强烈不满|敷衍|推脱|推诿"
    r"|忽悠|耍我|太过分|不负责任|差评|骗人|欺诈|坑人"
    r"|unacceptable|outrageous|ridiculous|absurd|terrible|awful|furious|angry"
    r"|scam|fraud|cheat|cheated|lied|lying|ignored|worthless|useless|pathetic"
    r"|disgusted|sick of|had enough",
    re.I,
)

# Frustration: impatience and repetition, still about the question.
_FRUSTRATED = re.compile(
    r"失望|不满|又没|还是没|一直没|催了|迟迟|等了?很久|等了?好几天|着急|紧急|尽快"
    r"|到底|多久了|再三|反复|已经说过|第三次|第二次|不耐烦|拖"
    r"|frustrated|disappointed|disappointment|still not|yet again|urgent|asap"
    r"|hurry|delayed|delay|waiting|three times|twice|second time|repeated"
    r"|overdue|week(s)? later|month(s)? later",
    re.I,
)

_LEVEL_ORDER: tuple[Emotion, ...] = (
    Emotion.ESCALATION_RISK,
    Emotion.ANGRY,
    Emotion.FRUSTRATED,
    Emotion.CALM,
)

_PATTERN_FOR_LEVEL: dict[Emotion, re.Pattern[str]] = {
    Emotion.ESCALATION_RISK: _ESCALATION_RISK,
    Emotion.ANGRY: _ANGRY,
    Emotion.FRUSTRATED: _FRUSTRATED,
}


def detect_emotion(text: str) -> EmotionSignal:
    """Highest level the text evidences, with the terms behind it."""
    if not text:
        return EmotionSignal(Emotion.CALM, ())
    terms: list[str] = []
    for level in _LEVEL_ORDER:
        if level is Emotion.CALM:
            break
        found = _PATTERN_FOR_LEVEL[level].findall(text)
        if found:
            # `findall` returns tuples when the pattern has groups; these have
            # none, so every element is a plain string match.
            terms.extend(str(term) for term in found)
            return EmotionSignal(level, tuple(dict.fromkeys(terms)))
    return EmotionSignal(Emotion.CALM, ())
