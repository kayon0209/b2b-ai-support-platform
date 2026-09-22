"""Feature list 3.2: business-line routing.

The line is what the question is *about*, independent of the scene (what the
customer wants). These tests pin the two distinctions that make it useful
downstream rather than decorative:

- PCBA is assembly (SMT), not fabrication (PCB). Routing a panel of assembled
  boards to the bare-board team is the exact misroute this axis exists to
  prevent, so it is asserted directly, not just via "SMT words match SMT".
- No line is a legitimate answer. An invoice or password question mentions no
  product; guessing a line would hand the conversation to a team that cannot
  help, so UNSPECIFIED is asserted as correct behaviour, not as a miss.
"""

from __future__ import annotations

from platform_core.agent_runtime.intent import BusinessLine, classify


def test_pcb_question_is_routed_to_the_pcb_line() -> None:
    detection = classify("我的 PCB 打样什么时候能出货？")
    assert detection.business_line is BusinessLine.PCB


def test_fr4_and_impedance_vocabulary_stays_on_the_pcb_line() -> None:
    detection = classify("FR4 板材的阻抗能做到多少？沉金工艺可以吗？")
    assert detection.business_line is BusinessLine.PCB


def test_pcba_is_assembly_not_fabrication() -> None:
    """`\bpcb\b` must not match inside "pcba" - the whole point of SMT."""
    detection = classify("PCBA 贴片加工的报价是多少？")
    assert detection.business_line is BusinessLine.SMT


def test_smt_process_vocabulary_is_routed_to_smt() -> None:
    detection = classify("回流焊炉温曲线怎么设？锡膏印刷有偏移")
    assert detection.business_line is BusinessLine.SMT


def test_component_sourcing_vocabulary_is_routed_to_components() -> None:
    detection = classify("这个芯片有没有国产替代料？料号是什么？")
    assert detection.business_line is BusinessLine.COMPONENT


def test_dfm_review_vocabulary_is_routed_to_dfm() -> None:
    detection = classify("DFM 工艺评审报告出来了吗？")
    assert detection.business_line is BusinessLine.DFM


def test_panelisation_is_dfm_even_though_it_sounds_like_pcb() -> None:
    """Overlapping vocabulary must resolve to the more specific line.

    "拼板" matches both, and the manufacturability reading is the one a
    customer asking about panelisation wants.
    """
    detection = classify("拼板和工艺边要怎么设计？")
    assert detection.business_line is BusinessLine.DFM


def test_a_question_about_no_product_line_is_unspecified() -> None:
    """Guessing a line would misroute the handoff - absence is the answer."""
    for question in ("我的发票什么时候开？", "帮我重置一下登录密码"):
        assert classify(question).business_line is BusinessLine.UNSPECIFIED


def test_business_line_is_recorded_in_the_audit_snapshot() -> None:
    snapshot = classify("PCB 打样的交期是多久？").as_dict()
    assert snapshot["business_line"] == BusinessLine.PCB.value


def test_the_line_evidences_itself_on_its_own_signal_axis() -> None:
    detection = classify("这个电容的封装是什么？")
    axes = {signal.axis for signal in detection.signals}
    assert "business_line" in axes


def test_line_and_scene_are_independent() -> None:
    """A complaint about a PCB is COMPLAINT *and* PCB, not one or the other."""
    detection = classify("我要投诉！你们的 PCB 板质量太差了")
    assert detection.business_line is BusinessLine.PCB
    assert detection.scene.value == "complaint"
