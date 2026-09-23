"""Feature list 5.4: measuring brand tone, instead of trusting the prompt.

The audit's finding was precise: tone lived entirely in the prompt, so there
was no way to say whether a given answer matched it. "The prompt asks for it"
is not a measurement, and when the tone drifts - a model upgrade, a re-worded
system message - nothing notices.

**What this is not.** It is not the commitment red line (6.1). That rule is
about *authority*: a promised price or lead time is something the platform
cannot grant. Tone is about *register*: "百分百没问题" is a tone failure AND a
commitment, "亲" is only a tone failure. Keeping them separate matters because
they have different fixes - a commitment must be blocked, a tone slip may only
need a note - and because merging them would let a style rule block a
legitimate answer.

Deterministic and offline for the same reason as everything else on this path:
a tone score produced by a model cannot be argued with, and the first question
anyone asks about a flagged answer is "why".

Two rule kinds, because they need different handling:

- **Banned phrases** are always wrong in customer-facing text (over-promising,
  over-familiar). These are violations.
- **Required phrasing** (a second-person form) is a preference with legitimate
  exceptions, so it is reported as a signal rather than a violation. A checker
  that fails on style preferences produces noise, and a noisy checker is
  switched off.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Over-promising: the platform cannot warrant an outcome, so the wording is
# wrong regardless of intent. Distinct from 6.1's red line, which is about
# commercial commitments with numbers in them ("3 天交货", "打九折").
_OVER_PROMISING = re.compile(
    r"百分百|百分之百|绝对(?:没|能|可以)|保证(?:没|能|可以)?|一定能|肯定不会|绝无问题"
    r"|absolutely guaranteed|guaranteed|100% (?:certain|sure)",
    re.I,
)

# Over-familiar register. Fine between colleagues, wrong from a company's
# support desk to a procurement manager.
_OVER_FAMILIAR = re.compile(r"亲[，,。!！~]|亲亲|宝贝|么么哒|小仙女", re.I)

# Deflecting: these turn a question away without answering or routing it.
# "没办法" as a statement of fact about a process is not the same as "没办法"
# as a refusal, so only the refusal-shaped forms are listed.
_DEFLECTING = re.compile(r"不关我(?:的)?事|不是我负责|没办法帮|不知道你(?:们)?|别问我", re.I)

_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("over_promising", _OVER_PROMISING),
    ("over_familiar", _OVER_FAMILIAR),
    ("deflecting", _DEFLECTING),
)

# A second-person form is expected in Chinese customer-facing text. Reported as
# a signal, not a violation - see the module docstring.
_RESPECTFUL_ADDRESS = re.compile(r"您")
_INFORMAL_ADDRESS = re.compile(r"你(?!们)")


@dataclass(frozen=True)
class ToneReport:
    """Rule failures, plus the preference signals, with the matched terms."""

    violations: tuple[tuple[str, str], ...] = ()
    signals: tuple[str, ...] = ()

    @property
    def clean(self) -> bool:
        """No rule was broken. Signals do not make an answer unclean."""
        return not self.violations


def check_tone(text: str) -> ToneReport:
    """Evaluate `text` against the brand-tone rules.

    Returns matched terms alongside each rule name so the finding is
    actionable: "over_familiar" tells a writer what to avoid, "亲" tells them
    where.
    """
    if not text:
        return ToneReport()

    violations: list[tuple[str, str]] = []
    for name, pattern in _RULES:
        matched = pattern.findall(text)
        if matched:
            violations.append((name, matched[0] if isinstance(matched[0], str) else name))

    signals: list[str] = []
    if _INFORMAL_ADDRESS.search(text) and not _RESPECTFUL_ADDRESS.search(text):
        # Only when the respectful form is absent: "您" and "你" in one sentence
        # is a style choice, and flagging it would be the noise this design
        # avoids.
        signals.append("informal_address")

    return ToneReport(violations=tuple(violations), signals=tuple(signals))


def tone_consistency_rate(texts: list[str]) -> float | None:
    """Share of answers with no tone violation. None when nothing was measured.

    The dashboard number 5.4 asks for. None rather than 1.0 over an empty set:
    perfect consistency over zero answers is not a measurement, and reporting
    it as one is how a metric looks healthy while measuring nothing.
    """
    if not texts:
        return None
    clean = sum(1 for text in texts if check_tone(text).clean)
    return round(clean / len(texts), 3)


__all__ = ["ToneReport", "check_tone", "tone_consistency_rate"]
