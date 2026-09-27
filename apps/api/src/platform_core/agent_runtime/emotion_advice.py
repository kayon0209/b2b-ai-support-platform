"""Explainable, advisory emotion trend and priority recommendations (R2-01).

This module reads only redacted customer turns, emits no quoted text, and has
no persistence or business side effects. Its output is a review aid; it never
changes Case priority, SLA, lease ownership, a tool proposal, or a customer
message. Tenant persistence and supervisor correction belong to the API layer.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from platform_core.agent_runtime.emotion import (
    Emotion,
    EmotionEvidenceDisposition,
    find_emotion_matches,
)

MAX_TREND_TURNS = 5
ADVICE_VERSION = "emotion-advice-v1"
FLAG_EMOTION_PRIORITY_ADVICE = "agent.emotion_priority_advice"

EmotionTrend = Literal["rising", "stable", "falling", "unknown"]
AttentionAdvice = Literal["none", "monitor", "review", "urgent_review"]
PriorityAdvice = Literal["p0", "p1", "p2"] | None

_LEVEL_RANK = {
    Emotion.CALM: 0,
    Emotion.FRUSTRATED: 1,
    Emotion.ANGRY: 2,
    Emotion.ESCALATION_RISK: 3,
}


@dataclass(frozen=True)
class EmotionTurn:
    turn_id: str
    role: str
    text: str


class EmotionEvidenceRef(BaseModel):
    """A pointer to an existing conversation span, never a copied phrase."""

    model_config = ConfigDict(frozen=True)

    turn_id: str = Field(min_length=1, max_length=255)
    start: int = Field(ge=0)
    end: int = Field(gt=0)
    level: Emotion
    reason_code: str = Field(min_length=1, max_length=63)


class EmotionPriorityAdvice(BaseModel):
    """Structured queue guidance that a human may accept or correct."""

    model_config = ConfigDict(frozen=True)

    advice_id: str = Field(min_length=64, max_length=64)
    version: str = ADVICE_VERSION
    current_level: Emotion
    trend: EmotionTrend
    attention: AttentionAdvice
    suggested_case_priority: PriorityAdvice
    reason_codes: tuple[str, ...]
    evidence: tuple[EmotionEvidenceRef, ...]
    suppressed_evidence_count: int = Field(ge=0)
    ambiguous_tone: bool
    analyzed_customer_turns: int = Field(ge=0, le=MAX_TREND_TURNS)
    advisory_only: Literal[True] = True


@dataclass(frozen=True)
class _TurnSignal:
    turn_id: str
    level: Emotion
    ambiguous: bool
    reason_codes: tuple[str, ...]
    evidence: tuple[EmotionEvidenceRef, ...]
    suppressed: int


def _signal_for_turn(turn: EmotionTurn) -> _TurnSignal:
    matches = find_emotion_matches(turn.text)
    direct = [item for item in matches if item.disposition is EmotionEvidenceDisposition.ACTIVE]
    ambiguous = any(item.disposition is EmotionEvidenceDisposition.AMBIGUOUS for item in matches)
    current = max(
        (item.level for item in direct), key=lambda level: _LEVEL_RANK[level], default=Emotion.CALM
    )
    reasons = {
        item.reason_code
        for item in matches
        if item.disposition is not EmotionEvidenceDisposition.ACTIVE
    }
    if current is Emotion.ESCALATION_RISK:
        reasons.add("external_escalation_language")
    elif current is Emotion.ANGRY:
        reasons.add("direct_anger_language")
    elif current is Emotion.FRUSTRATED:
        reasons.add("direct_frustration_language")
    evidence = tuple(
        EmotionEvidenceRef(
            turn_id=turn.turn_id,
            start=item.start,
            end=item.end,
            level=item.level,
            reason_code=item.reason_code,
        )
        for item in direct
    )
    return _TurnSignal(
        turn_id=turn.turn_id,
        level=current,
        ambiguous=ambiguous,
        reason_codes=tuple(sorted(reasons)),
        evidence=evidence,
        suppressed=len(matches) - len(direct),
    )


def _trend(signals: Sequence[_TurnSignal]) -> EmotionTrend:
    if len(signals) < 2 or signals[-1].ambiguous or signals[-2].ambiguous:
        return "unknown"
    previous = _LEVEL_RANK[signals[-2].level]
    current = _LEVEL_RANK[signals[-1].level]
    if current > previous:
        return "rising"
    if current < previous:
        return "falling"
    return "stable"


def advice_id(
    *, tenant_id: uuid.UUID, conversation_ref_id: uuid.UUID, timeline_revision: int
) -> str:
    """Bind a correction to the exact tenant conversation revision."""
    material = f"{ADVICE_VERSION}:{tenant_id}:{conversation_ref_id}:{timeline_revision}".encode(
        "ascii"
    )
    return hashlib.sha256(material).hexdigest()


def recommend_emotion_priority(
    *,
    tenant_id: uuid.UUID,
    conversation_ref_id: uuid.UUID,
    timeline_revision: int,
    turns: Sequence[EmotionTurn],
) -> EmotionPriorityAdvice:
    """Return advisory attention based on the latest five customer turns.

    Assistant, system, and tool messages are ignored. Ambiguous or suppressed
    evidence never raises a recommendation to p0/p1. Every ranking signal has
    a stable reason code, while customer wording remains in the tenant-bound
    transcript and is referenced only by turn id and Unicode span offsets.
    """
    customer_turns = [turn for turn in turns if turn.role == "customer"][-MAX_TREND_TURNS:]
    signals = [_signal_for_turn(turn) for turn in customer_turns]
    latest = (
        signals[-1]
        if signals
        else _TurnSignal(
            turn_id="",
            level=Emotion.CALM,
            ambiguous=False,
            reason_codes=(),
            evidence=(),
            suppressed=0,
        )
    )
    trend = _trend(signals)
    ambiguous = any(signal.ambiguous for signal in signals[-2:])
    sustained_frustration = (
        len(signals) >= 2
        and all(
            _LEVEL_RANK[signal.level] >= _LEVEL_RANK[Emotion.FRUSTRATED] for signal in signals[-2:]
        )
        and not ambiguous
    )

    reasons = set(latest.reason_codes)
    if trend == "rising" and _LEVEL_RANK[latest.level] >= _LEVEL_RANK[Emotion.FRUSTRATED]:
        reasons.add("emotion_trend_rising")
    if sustained_frustration:
        reasons.add("emotion_sustained_across_turns")
    if ambiguous:
        reasons.add("emotion_context_needs_human_review")
    suppressed = sum(signal.suppressed for signal in signals)
    if suppressed:
        reasons.add("quoted_or_negated_terms_not_ranked")

    if latest.level is Emotion.ESCALATION_RISK and not latest.ambiguous:
        attention: AttentionAdvice = "urgent_review"
        suggested_priority: PriorityAdvice = "p0"
    elif latest.level is Emotion.ANGRY and not latest.ambiguous:
        attention = "review"
        suggested_priority = "p1"
    elif sustained_frustration or trend == "rising":
        attention = "monitor"
        suggested_priority = "p2"
    elif ambiguous:
        attention = "monitor"
        suggested_priority = None
    else:
        attention = "none"
        suggested_priority = None

    evidence = tuple(item for signal in signals[-2:] for item in signal.evidence)
    return EmotionPriorityAdvice(
        advice_id=advice_id(
            tenant_id=tenant_id,
            conversation_ref_id=conversation_ref_id,
            timeline_revision=timeline_revision,
        ),
        current_level=latest.level,
        trend=trend,
        attention=attention,
        suggested_case_priority=suggested_priority,
        reason_codes=tuple(sorted(reasons)),
        evidence=evidence,
        suppressed_evidence_count=suppressed,
        ambiguous_tone=ambiguous,
        analyzed_customer_turns=len(customer_turns),
    )


__all__ = [
    "ADVICE_VERSION",
    "FLAG_EMOTION_PRIORITY_ADVICE",
    "MAX_TREND_TURNS",
    "EmotionEvidenceRef",
    "EmotionPriorityAdvice",
    "EmotionTurn",
    "advice_id",
    "recommend_emotion_priority",
]
