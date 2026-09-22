"""Feature list 7.2 (emotion detection) and 7.1's seventh handoff trigger.

The assertions that matter are the boundaries between levels, because that is
where the routing consequence changes:

- FRUSTRATED does NOT hand off. A waiting customer is still answerable, and
  escalating every impatient message is how a support queue fills up. If
  someone later relaxes this, the queue-depth argument in `emotion.py` is the
  thing they are overruling, and this test is what makes them notice.
- ESCALATION_RISK outranks ANGRY in the same message, because the external
  action (regulator, lawyer) is the time-critical one, not the tone.
- The level carries its own evidence. An unexplained handoff is the failure
  mode this module was written to avoid.
"""

from __future__ import annotations

from platform_core.agent_runtime.emotion import Emotion, detect_emotion


def test_an_ordinary_question_is_calm() -> None:
    signal = detect_emotion("我的订单什么时候发货？")
    assert signal.level is Emotion.CALM
    assert signal.should_handoff is False


def test_impatience_is_frustrated() -> None:
    signal = detect_emotion("这个单我催了好几次了，到底什么时候能发货？")
    assert signal.level is Emotion.FRUSTRATED


def test_frustration_alone_does_not_hand_off() -> None:
    """The queue-depth decision: impatience is still answerable."""
    signal = detect_emotion("等了很久了，尽快帮我处理一下")
    assert signal.level is Emotion.FRUSTRATED
    assert signal.should_handoff is False


def test_anger_hands_off() -> None:
    signal = detect_emotion("你们的服务太差了，完全是敷衍！")
    assert signal.level is Emotion.ANGRY
    assert signal.should_handoff is True


def test_regulator_or_language_of_legal_action_is_escalation_risk() -> None:
    for text in ("我要投诉到12315", "再不解决我就找律师起诉", "我要找媒体曝光你们"):
        signal = detect_emotion(text)
        assert signal.level is Emotion.ESCALATION_RISK, text
        assert signal.should_handoff is True


def test_escalation_risk_outranks_anger_in_the_same_message() -> None:
    """The external action is the time-critical one, not the tone."""
    signal = detect_emotion("你们太差了，我要找律师")
    assert signal.level is Emotion.ESCALATION_RISK


def test_the_level_carries_the_terms_that_produced_it() -> None:
    signal = detect_emotion("我要投诉到12315")
    assert "12315" in signal.terms
    assert signal.terms  # never empty when a level was assigned


def test_english_anger_vocabulary_is_detected() -> None:
    signal = detect_emotion("This is unacceptable, I've been ignored three times")
    assert signal.level is not Emotion.CALM


def test_empty_text_is_calm_rather_than_an_error() -> None:
    signal = detect_emotion("")
    assert signal.level is Emotion.CALM
    assert signal.should_handoff is False


def test_detection_is_deterministic() -> None:
    """Same text, same answer - it feeds a routing decision, so it must be
    reproducible and arguable."""
    text = "你们太差了，我要找律师"
    assert detect_emotion(text) == detect_emotion(text)
