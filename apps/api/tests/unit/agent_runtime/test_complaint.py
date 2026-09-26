"""The complaint-claim detector: a claim is not a question about the policy.

The research report puts compensation at L6 (必须转人工) and at L1 (可直接回答)
in the same breath - 赔付政策 is a document, 我要索赔 is a claim under it. So
this detector has two ways to be wrong and both are tested: answering a claim,
and hijacking a question that merely mentions one.
"""

import pytest

from platform_core.agent_runtime.complaint import is_complaint_claim


@pytest.mark.parametrize(
    "message",
    [
        # Chinese claims. Note none of these carry Scene.COMPLAINT - "板子短路了，
        # 我要索赔" measures as technical_support - which is why the scene axis
        # is not what the gate keys on.
        "板子短路了，我要索赔",
        "开短路不良我要索赔",
        "我要投诉",
        "我要投诉你们的服务",
        "你们这批板子质量太差了，我要退货",
        "我要申请退款",
        "货期延误了，我要你们赔偿",
        "这个不良品请给我赔偿",
        # English claims.
        "this is unacceptable, I want compensation",
        "the boards arrived shorted, I want a refund",
        "I want to file a complaint",
        "I demand a refund for these defective boards",
        "Can I get a refund for the defective batch?",
        "can you refund me?",
        "I need a refund for this order",
        "let me speak to a manager",
    ],
)
def test_a_claim_for_a_remedy_is_a_complaint(message: str) -> None:
    assert is_complaint_claim(message) is True


@pytest.mark.parametrize(
    "message",
    [
        # Asking how the process works. The report puts 赔付政策/退换货规则 at
        # L1, so these are the answers the corpus is supposed to give, and
        # handing them to a queue would be refusing a documented capability.
        "这个不良品怎么理赔",
        "全测板开短路不良怎么赔付？",
        "你们的赔付政策是什么",
        "怎么投诉",
        "退货运费谁出",
        "what is your refund policy",
        "how do I file a complaint",
        "Are monthly plans refundable?",
        "I need to know how refunds work",
        # Ordinary questions that happen to be about the same topics.
        "半孔怎么做",
        "最小线宽能做多少",
        "我的订单到哪一步了",
        "板子报错，一直死机",
        "I was overcharged on the March invoice",
        "I cannot log in and my MFA codes are rejected",
        "Do you support SAML SSO, and what does the enterprise tier cost?",
        # The write path's canonical request, and a regression: it names a
        # remedy ("escalate") without claiming one, and an earlier version of
        # this detector read any declarative remedy mention as a claim, which
        # took a legitimate `jira.create_issue` request away from the tool that
        # acts on it. Escalating a defect to engineering is not compensation.
        "Please escalate this defect to your engineering team",
        "",
    ],
)
def test_a_question_about_a_remedy_is_not_a_claim(message: str) -> None:
    assert is_complaint_claim(message) is False


@pytest.mark.parametrize(
    "message",
    [
        # Both of these measure as Scene.COMPLAINT, because the scene pattern
        # counts "still not" as a complaint signal. They are an order-status
        # question and a shipment question, and the read path answers the
        # second one outright. This is the measured reason the gate does not
        # consult the scene.
        "my order has still not arrived",
        "The shipment still not updated, where is it?",
    ],
)
def test_a_stalled_order_is_not_a_claim(message: str) -> None:
    """The scene would call these complaints; the detector must not."""
    assert is_complaint_claim(message) is False


@pytest.mark.parametrize(
    "message",
    [
        "板子短路了",
        "这批货有虚焊",
    ],
)
def test_a_defect_report_with_no_remedy_asked_for_is_not_a_claim(message: str) -> None:
    """Deliberately out of scope, and recorded as such.

    The report lists these as scenario-D phrasings, but they ask for nothing:
    no compensation, no return, no escalation. Detecting them would mean
    matching bare defect vocabulary, which is the same vocabulary an engineer
    uses to ask for help diagnosing a board ("为什么板子会短路"), and an
    over-broad guard here sends real technical questions to a human queue.

    They are not unhandled: with no claim to make, the run continues to the
    knowledge path and abstains on evidence like any other unsupported
    question, which still ends in a handoff.
    """
    assert is_complaint_claim(message) is False
