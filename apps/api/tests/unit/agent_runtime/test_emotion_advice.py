"""R2-01: emotion trend advice is evidence-bound and never mutates priority."""

from __future__ import annotations

import uuid

from platform_core.agent_runtime.emotion import (
    Emotion,
    EmotionEvidenceDisposition,
    find_emotion_matches,
)
from platform_core.agent_runtime.emotion_advice import (
    EmotionTurn,
    advice_id,
    recommend_emotion_priority,
)

TENANT = uuid.UUID("01900000-0000-7000-8000-000000000001")
CONVERSATION = uuid.UUID("01900000-0000-7000-8000-000000000002")


def _advice(*texts: str):
    return recommend_emotion_priority(
        tenant_id=TENANT,
        conversation_ref_id=CONVERSATION,
        timeline_revision=17,
        turns=[
            EmotionTurn(turn_id=f"turn-{index}", role="customer", text=text)
            for index, text in enumerate(texts)
        ],
    )


def test_emotion_spans_count_unicode_codepoints_and_keep_text_out_of_output() -> None:
    text = "订单还没发货，你们服务太差了"
    match = next(item for item in find_emotion_matches(text) if item.level is Emotion.ANGRY)
    assert text[match.start : match.end] == "太差"
    assert match.disposition is EmotionEvidenceDisposition.ACTIVE

    advice = _advice("订单还没发货，你们服务太差了")
    report = advice.model_dump_json()
    assert advice.current_level is Emotion.ANGRY
    assert advice.attention == "review"
    assert advice.suggested_case_priority == "p1"
    assert "太差" not in report
    assert advice.evidence[0].turn_id == "turn-0"


def test_quoted_prior_complaint_is_not_ranked_as_current_emotion() -> None:
    matches = find_emotion_matches("客户引用上次聊天：‘你们服务太差了’并问如何处理")
    anger = next(item for item in matches if item.level is Emotion.ANGRY)
    assert anger.disposition is EmotionEvidenceDisposition.QUOTED

    advice = _advice("客户引用上次聊天：‘你们服务太差了’并问如何处理")
    assert advice.current_level is Emotion.CALM
    assert advice.attention == "none"
    assert "quoted_or_negated_terms_not_ranked" in advice.reason_codes


def test_quotes_for_emphasis_remain_direct_evidence() -> None:
    matches = find_emotion_matches("你们服务‘太差了’")
    anger = next(item for item in matches if item.level is Emotion.ANGRY)
    assert anger.disposition is EmotionEvidenceDisposition.ACTIVE


def test_negated_emotion_is_suppressed_and_double_negation_is_ambiguous() -> None:
    negated = find_emotion_matches("我没有不满")
    assert next(item for item in negated if item.level is Emotion.FRUSTRATED).disposition is (
        EmotionEvidenceDisposition.NEGATED
    )

    double = find_emotion_matches("我不是不满")
    assert next(item for item in double if item.level is Emotion.FRUSTRATED).disposition is (
        EmotionEvidenceDisposition.AMBIGUOUS
    )

    advice = _advice("我没有不满")
    assert advice.current_level is Emotion.CALM
    assert advice.suggested_case_priority is None


def test_ironic_or_mixed_tone_does_not_raise_an_automatic_priority() -> None:
    advice = _advice("真是太棒了，我等了很久")
    assert advice.ambiguous_tone is True
    assert advice.suggested_case_priority is None
    assert "emotion_context_needs_human_review" in advice.reason_codes


def test_increasing_frustration_is_only_a_monitor_suggestion() -> None:
    advice = _advice("还没收到进度", "我已经催了三次，还是没消息")
    assert advice.trend == "rising"
    assert advice.attention == "monitor"
    assert advice.suggested_case_priority == "p2"
    assert "emotion_trend_rising" in advice.reason_codes


def test_escalation_language_suggests_review_without_mutating_case_state() -> None:
    advice = _advice("我要投诉到12315")
    assert advice.current_level is Emotion.ESCALATION_RISK
    assert advice.attention == "urgent_review"
    assert advice.suggested_case_priority == "p0"
    assert advice.advisory_only is True


def test_advice_id_changes_with_tenant_conversation_revision() -> None:
    first = advice_id(tenant_id=TENANT, conversation_ref_id=CONVERSATION, timeline_revision=17)
    second = advice_id(tenant_id=TENANT, conversation_ref_id=CONVERSATION, timeline_revision=18)
    other_tenant = advice_id(
        tenant_id=uuid.UUID("01900000-0000-7000-8000-000000000003"),
        conversation_ref_id=CONVERSATION,
        timeline_revision=17,
    )
    assert first != second
    assert first != other_tenant
