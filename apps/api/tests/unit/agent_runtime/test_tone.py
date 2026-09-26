"""Feature list 5.4: tone is measured, and the measurement is quiet.

The two failure modes of a style checker, both asserted here:

- **It fires on legitimate answers.** A checker that fails on preference
  (informal address) produces noise, and a noisy checker gets switched off -
  so a preference is a *signal*, not a violation, and the clean/unclean
  boundary is asserted directly.
- **It overlaps with the commitment red line.** 6.1 blocks wording the platform
  has no authority to say. Overlap would let a style rule suppress legitimate
  answers, so the boundary is pinned: tone-only phrases must not be treated as
  commitments, and the report must not claim they are.

Also: the rate over no answers is None, not 1.0. A metric that reads perfect
while measuring nothing is the failure this whole file exists to avoid.
"""

from __future__ import annotations

from platform_core.agent_runtime.tone import (
    ToneReport,
    check_tone,
    tone_consistency_rate,
)


def test_a_plain_helpful_answer_is_clean() -> None:
    assert check_tone("您的订单预计 5 个工作日发出，我帮您留意物流更新。").clean


def test_over_promising_is_a_violation() -> None:
    report = check_tone("这件事百分百没问题，您放心。")
    assert not report.clean
    assert any(name == "over_promising" for name, _term in report.violations)


def test_over_familiar_language_is_a_violation() -> None:
    report = check_tone("亲，您的订单已经发货了哦。")
    assert not report.clean
    assert any(name == "over_familiar" for name, _term in report.violations)


def test_deflecting_language_is_a_violation() -> None:
    report = check_tone("这个不关我的事，你问别人吧。")
    assert not report.clean
    assert any(name == "deflecting" for name, _term in report.violations)


def test_the_violation_carries_the_matched_term() -> None:
    """A rule name tells a writer what to avoid; the term tells them where."""
    report = check_tone("亲，订单已发货。")
    assert report.violations
    _name, term = report.violations[0]
    assert term


def test_informal_address_is_a_signal_not_a_violation() -> None:
    """Preference must not make an answer unclean, or the checker is noise."""
    report = check_tone("你的订单已经发货了。")
    assert report.clean
    assert "informal_address" in report.signals


def test_the_respectful_form_produces_no_signal() -> None:
    assert check_tone("您的订单已经发货了。").signals == ()


def test_mixing_both_address_forms_is_not_flagged() -> None:
    """ "您" and "你" together is a style choice, not a defect."""
    assert check_tone("您的订单已发出，你可以查物流。").signals == ()


def test_a_statement_of_fact_is_not_deflection() -> None:
    """Only refusal-shaped phrasing is listed; reporting a process is not."""
    assert check_tone("退款的时效取决于银行，通常 3 到 5 个工作日。").clean


def test_empty_text_is_clean_rather_than_an_error() -> None:
    assert check_tone("").clean


def test_the_report_defaults_to_clean() -> None:
    assert ToneReport().clean


def test_the_consistency_rate_counts_clean_answers() -> None:
    texts = ["您的订单已发货。", "亲，已发货。", "您的退款正在处理。", "百分百没问题。"]
    assert tone_consistency_rate(texts) == 0.5


def test_the_rate_over_no_answers_is_unknown_not_perfect() -> None:
    """1.0 over zero answers would read as a healthy metric measuring nothing."""
    assert tone_consistency_rate([]) is None


def test_tone_rules_do_not_claim_to_be_the_commitment_red_line() -> None:
    """6.1 owns commitments; this module must not silently absorb them.

    A priced promise is a 6.1 matter. Tone flags the *register* of the same
    sentence, and the two are reported separately so neither can suppress the
    other's legitimate answers.
    """
    from platform_core.agent_runtime.qa_path import redline_violations

    priced = "我们可以给您打九折，3 天交货。"
    assert redline_violations(priced), "6.1 must still catch the commitment"
    # The tone checker says nothing about it: no banned phrasing in that text.
    assert check_tone(priced).clean
