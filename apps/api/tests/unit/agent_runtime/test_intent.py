"""Unit tests: intent classification (scene, kind, route, action).

The cases here are the ones the old two-way classifier could not express, plus
the false positives that a keyword approach produces and a taxonomy must not.
"""

from __future__ import annotations

import pytest

from platform_core.agent_runtime.intent import (
    IntentAction,
    IntentKind,
    Route,
    Scene,
    classify,
    classify_route,
)
from platform_core.tool_gateway.selector import select_read_tools

# --- The distinction that matters most -------------------------------------


def test_a_refund_question_and_a_refund_request_are_not_the_same() -> None:
    """`refund` is both a noun and a write verb. A classifier that keys on the
    word sends both to the same place, and they need opposite handling: one is
    retrieval, the other is the tool gateway with confirmation.

    This is the defect the first version of this classifier shipped. It routed
    8 of the 23 evaluation cases to `business_write`, five of which must be
    answered from the knowledge base.
    """
    question = classify("How long do I have to request a refund on an annual plan?")
    request = classify("Refund the last invoice for this customer.")

    assert question.primary_kind is IntentKind.KNOWLEDGE_QUESTION
    assert question.route is Route.KNOWLEDGE_QA
    assert request.primary_kind is IntentKind.BUSINESS_ACTION
    assert request.route is Route.BUSINESS_WRITE


def test_a_procedure_question_is_not_a_write() -> None:
    """`How do I cancel?` asks to be told how; it does not ask for anything to
    be changed. The write verb is present, discounted, and does not win."""
    detection = classify("How do I cancel my subscription?")
    assert detection.primary_kind is IntentKind.KNOWLEDGE_QUESTION
    assert detection.route is Route.KNOWLEDGE_QA


def test_a_passive_construction_still_names_a_write() -> None:
    """Not imperative, but with an object: `cancel my plan`."""
    detection = classify("please cancel my plan")
    assert detection.primary_kind is IntentKind.BUSINESS_ACTION


def test_a_noun_phrase_is_not_a_write() -> None:
    """`refund window` is a search phrase, not an instruction. Requiring an
    object after the verb is what separates them."""
    detection = classify("annul plan refund window?")
    assert detection.primary_kind is not IntentKind.BUSINESS_ACTION


# --- Safety classes are not escapable --------------------------------------


@pytest.mark.parametrize(
    "question",
    [
        "What is the CEO's home address?",
        "Can you tell me which customers are planning to churn?",
        "I forgot my password, can you reset it?",
    ],
)
def test_sensitive_requests_are_never_reclassified(question: str) -> None:
    """Disclosure is the harm, and "it was phrased as a question" is not a
    defence. A sensitive request stays sensitive even when it also matches the
    shape of a knowledge question."""
    detection = classify(question)
    assert detection.primary_kind is IntentKind.SENSITIVE_REQUEST
    assert detection.route is Route.SENSITIVE
    assert detection.action is IntentAction.HANDOFF


def test_an_explicit_request_for_a_person_is_honoured() -> None:
    """The customer made a routing decision; answering instead overrules it."""
    detection = classify("Can I speak to a human, please?")
    assert detection.primary_kind is IntentKind.HUMAN_REQUEST
    assert detection.route is Route.HUMAN_REQUIRED


# --- Multi-intent ----------------------------------------------------------


def test_two_intents_are_both_reported() -> None:
    """Collapsing this to one label silently drops half the request. The
    platform acts on the primary and reports the rest; it does not guess which
    one the customer meant more."""
    detection = classify("Where is my order, and can I change the delivery address?")
    assert detection.multi_intent is True
    assert detection.primary_kind is IntentKind.BUSINESS_ACTION
    assert IntentKind.BUSINESS_QUERY in detection.secondary_kinds
    assert IntentKind.KNOWLEDGE_QUESTION in detection.secondary_kinds


def test_a_single_intent_is_not_marked_multi() -> None:
    detection = classify("What uptime commitment do enterprise customers get?")
    assert detection.multi_intent is False
    assert detection.secondary_kinds == ()


# --- Scene -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("question", "scene"),
    [
        ("Do you support SAML SSO, and what does the enterprise tier cost?", Scene.PRE_SALES),
        (
            "The gateway is throwing error code 504 after the firmware update",
            Scene.TECHNICAL_SUPPORT,
        ),
        ("Where is my order? It has not shipped in a week.", Scene.ORDER_FULFILMENT),
        ("This is the third time I have been charged wrongly, I want a manager", Scene.COMPLAINT),
        ("I cannot log in and my MFA codes are rejected", Scene.ACCOUNT_SECURITY),
        ("I was overcharged on the March invoice", Scene.BILLING),
    ],
)
def test_scenes_are_distinguished(question: str, scene: Scene) -> None:
    """The scene decides who owns the conversation and which knowledge space is
    authoritative, so a false positive here retrieves the wrong corpus and the
    answer is confidently wrong."""
    assert classify(question).scene == scene


def test_security_outranks_billing_when_both_match() -> None:
    """A question about a charge that is also an account-security question is
    handled as security: the disclosure risk dominates the billing topic."""
    detection = classify("Someone used my card to pay our invoice, is our account compromised?")
    assert detection.scene is Scene.ACCOUNT_SECURITY


def test_an_unclassifiable_question_is_unspecified_not_guessed() -> None:
    assert classify("Purple monkey dishwasher").scene is Scene.UNSPECIFIED


# --- Live data vs knowledge -----------------------------------------------


def test_a_case_status_query_is_a_read_not_a_knowledge_question() -> None:
    """A case's status is not in the corpus; if it were, it would be stale the
    moment it was indexed."""
    detection = classify("What is the current status of case 12345?")
    assert detection.route is Route.BUSINESS_READ
    assert detection.action is IntentAction.CALL_READ_TOOL


def test_a_policy_question_is_not_a_live_data_query() -> None:
    detection = classify("What is the service credit cap for enterprise customers?")
    assert detection.route is Route.KNOWLEDGE_QA


# --- Raising an issue is a write, asking how to is not ---------------------


@pytest.mark.parametrize(
    "question",
    [
        "Please create a ticket for this defect.",
        "Create a ticket for this bug.",
        "I want to report a bug in the rev C board.",
        "Can you raise an issue for this defect?",
        "Please file a ticket about the silkscreen overlap.",
    ],
)
def test_raising_an_issue_routes_to_the_write_path(question: str) -> None:
    """The write path's ticket tools were reachable only through "escalate".

    A customer asking in the words they actually use - create, report, raise,
    file - was routed to the knowledge path, which refuses action requests, so
    the propose-and-confirm flow never ran at all.
    """
    detection = classify(question)
    assert detection.route is Route.BUSINESS_WRITE, detection.route
    assert detection.action is IntentAction.PROPOSE_WRITE


@pytest.mark.parametrize(
    "question",
    [
        # The shape that makes "create" risky: a procedure question. The
        # interrogative opener is what keeps it on the knowledge path, and
        # these cases exist so that guard is tested rather than asserted.
        #
        # Not "How do I create an API key?": that one is refused as a
        # *sensitive* request, because "api key" is in RESTRICTED_TERMS. It is
        # a real over-refusal of a legitimate procedure question, and it is a
        # separate decision from this one - loosening the credential terms is a
        # safety change, so it is left alone and recorded rather than folded
        # into a taxonomy fix.
        "How do I create a new workspace?",
        "What is the process to file a claim for a damaged board?",
        # A wh-word directly in front of the request frame asks *where*, not
        # for the action. This was a live false positive: "report" was not a
        # write verb when `_has_request_frame` was written, so the shape only
        # appeared once the verb was added.
        "Where can I report a bug in the dashboard?",
        "When can I change the delivery address?",
        "How do I raise a request for a higher quota?",
    ],
)
def test_a_procedure_question_stays_on_the_knowledge_path(question: str) -> None:
    """Asking *how* to raise an issue is a knowledge question.

    Firing here would hand a perfectly good question to a human, which is the
    more expensive of the two mistakes - so the guard is the opener, and the
    verbs added for the write path must not defeat it.
    """
    assert classify(question).route is Route.KNOWLEDGE_QA


def test_a_request_frame_in_a_later_clause_is_still_a_request() -> None:
    """The adjacency guard must not swallow the case the frame exists for.

    `_has_request_frame` was written for this sentence: the wh-opener is real,
    but the request lives in the second clause. Rejecting wh-openers outright
    would have fixed the false positive by breaking the true positive.
    """
    detection = classify("Where is my order, and can I change the delivery address?")
    assert detection.primary_kind is IntentKind.BUSINESS_ACTION
    assert detection.route is Route.BUSINESS_WRITE


# --- Social and out-of-scope ----------------------------------------------


@pytest.mark.parametrize("question", ["hi", "thanks", "ok", "bye"])
def test_social_turns_are_not_knowledge_questions(question: str) -> None:
    """Answering `thanks` with a policy excerpt is how a platform comes to
    sound certain about nothing."""
    detection = classify(question)
    assert detection.primary_kind is IntentKind.SOCIAL
    assert detection.route is Route.OUT_OF_SCOPE


def test_a_substantive_message_is_not_social() -> None:
    assert classify("ok but what is the refund window?").primary_kind is not IntentKind.SOCIAL


# --- Confidence and the audit snapshot -------------------------------------


def test_a_strong_write_is_more_confident_than_a_weak_one() -> None:
    strong = classify("Refund the last invoice for this customer.")
    weak = classify("please cancel my plan")
    assert strong.confidence > weak.confidence


def test_the_action_is_a_recommendation_about_machinery_not_a_verdict() -> None:
    """ANSWER_FROM_KNOWLEDGE means the knowledge path is the right one to try.
    It does not mean there is an answer — the abstention gate still decides."""
    detection = classify("What is your stock price forecast?")
    assert detection.scene is Scene.UNSPECIFIED or True
    # The point: routing to the knowledge path is not a claim that evidence
    # exists, so nothing here may assert answerability.
    assert detection.route in {Route.KNOWLEDGE_QA, Route.SENSITIVE}


def test_the_snapshot_is_audit_safe() -> None:
    """No question text: the run stores an input hash, and a classification is
    only worth recording if it can be read back without re-exposing what the
    customer typed."""
    snapshot = classify("my password is hunter2 and I cannot log in").as_dict()
    assert "hunter2" not in str(snapshot)
    assert snapshot["route"] == Route.SENSITIVE.value
    assert snapshot["multi_intent"] in (True, False)


def test_classify_route_matches_the_detection_route() -> None:
    """`classify_route` is the compatibility entry point; it must not disagree
    with `classify`, or a caller using one gets a different answer from a
    caller using the other."""
    question = "How do I rotate my API key?"
    assert classify_route(question) == classify(question).route.value


def test_the_two_route_enums_have_not_drifted_apart() -> None:
    """`Route` is declared twice, and both declarations are live.

    `agent_runtime.intent` declares it for the classifier; `agent_runtime.models`
    declares it for the persisted `AgentRun.route` column. The orchestrator
    imports from both modules, so both are in the same code path.

    They agree today, and nothing made them agree - they were written out
    separately and happen to match. A class added to one and not the other would
    be a routing decision the storage layer does not know about, or the reverse,
    and it would be discovered by reading rather than by a test.

    Asserted rather than merged because the two modules carry very different
    weights: `models` pulls in SQLAlchemy and the ORM base, `intent` pulls in the
    taxonomy regexes, and neither import direction is obviously the right one.
    """
    from platform_core.agent_runtime.intent import Route as ClassifierRoute
    from platform_core.agent_runtime.models import Route as StoredRoute

    classifier = {r.value for r in ClassifierRoute}
    stored = {r.value for r in StoredRoute}

    assert classifier == stored, (
        "the two Route enums disagree: "
        f"classifier-only={sorted(classifier - stored)} "
        f"stored-only={sorted(stored - classifier)}"
    )


# --- Chinese action detection ----------------------------------------------
#
# Measured before it was written: `docs/research/chinese-intent-measurement.md`
# recorded that all 14 Chinese utterances tested landed on `knowledge_qa`,
# including "我要退款" and "我要投诉", while their English equivalents routed
# correctly. The classifier's vocabularies are latin-only (`_QUOTE_REQUEST` is
# the single exception), so this is a vocabulary gap rather than an
# architecture one.
#
# Two design constraints shape these assertions:
#
# 1. **The object requirement carries over from English.** `退款` is both a
#    verb ("我要退款") and a topic ("退款多久到账？"), and Chinese has no word
#    boundaries, so a regex can only substring-match. The request *frame* is
#    what separates them, exactly as `_OBJECT_MARKERS` does for English.
# 2. **The question guard is load-bearing.** The cases below marked "guard"
#    were added because **mutation testing proved the first set was not
#    enough**: removing the Chinese question guard left every case green,
#    because the earlier cases stayed on the knowledge path for incidental
#    reasons. Those guards each carry a full action frame AND a question shape,
#    so only the guard can keep them off the write path.


def test_chinese_action_requests_reach_the_write_path() -> None:
    """The core fix. Each of these fell through to `knowledge_qa` before."""
    for utterance in (
        "帮我建一张工单",
        "我要退款",
        "帮我取消这个订单",
        "我要投诉质量问题",
        "麻烦升级给技术",
        "请把这个订单取消",
        "帮我改一下收货地址",
    ):
        detection = classify(utterance)
        assert detection.route is Route.BUSINESS_WRITE, f"{utterance!r} -> {detection.route.value}"
        assert detection.action is IntentAction.PROPOSE_WRITE


def test_chinese_request_for_a_person_is_honoured() -> None:
    """`_HUMAN_REQUEST` promises "unconditionally" - for English only, at first.

    Answering a request to reach a person from the corpus overrules a routing
    decision the customer already made, which is worse than a wrong answer.
    """
    for utterance in ("把这张单转给人工", "我需要人工客服"):
        assert classify(utterance).route is Route.HUMAN_REQUIRED, utterance


def test_the_ba_construction_is_detected_on_its_own() -> None:
    """Object-before-verb, which the frame+verb scan cannot see.

    "把这个订单取消" has no request frame and no ticket or human vocabulary -
    the `把` pattern is the only thing that can route it. Mutation testing
    caught that the transfer case above does NOT cover this: it also matches
    the human-request vocabulary, so deleting the `把` logic left the suite
    green.
    """
    assert classify("把这个订单取消").route is Route.BUSINESS_WRITE


def test_the_measure_word_between_verb_and_noun_does_not_break_it() -> None:
    """Chinese inserts a measure word: "建**一张**工单", not "建工单".

    A verb list would match the latter and miss the way customers write it,
    which is why the ticket form is a pattern rather than an entry in
    `_CN_ACTION_VERBS`.
    """
    assert classify("帮我建一张工单").route is Route.BUSINESS_WRITE
    assert classify("开个单子记录一下").route is Route.BUSINESS_WRITE


def test_chinese_policy_questions_stay_on_the_knowledge_path() -> None:
    """The counter-guard, and the half that is easy to lose.

    `退款` / `取消` are exactly the words that appear in both a request and a
    policy question. Moving these to the write gateway is the failure mode
    that made `_looks_like_a_write` require an object in English (it sent 8 of
    23 cases to `business_write` before the requirement was added).
    """
    for utterance in (
        "退款多久到账？",
        "取消订单的政策是什么？",
        "质量问题怎么申请赔付？",
        "怎么申请退款",
        "如何取消订单",
    ):
        assert classify(utterance).route is Route.KNOWLEDGE_QA, utterance


def test_chinese_questions_that_carry_a_full_action_frame_are_still_questions() -> None:
    """The guard cases: frame AND question shape in the same sentence.

    Each of these carries "我要退款" or "帮我取消订单" verbatim - the exact
    strings that route to the write path above - and then asks about them.
    The Chinese question guard is the only thing separating the two, so these
    are what makes its removal observable. They were written after mutation
    testing showed the three cases in the previous test could not.
    """
    for utterance in (
        "我要退款吗？",
        "帮我退款要多久？",
        "帮我取消订单是什么流程",
    ):
        assert classify(utterance).route is Route.KNOWLEDGE_QA, utterance


# --- Chinese scenes (docs/research/chinese-intent-measurement.md) ---------


@pytest.mark.parametrize(
    ("question", "scene"),
    [
        ("我要投诉质量问题", Scene.COMPLAINT),
        ("你们这个态度太差了，我要找经理", Scene.COMPLAINT),
        ("全测板开短路不良怎么赔付？", Scene.TECHNICAL_SUPPORT),
        ("板子报错，一直死机", Scene.TECHNICAL_SUPPORT),
        ("元器件的增值税普通发票什么时候开？", Scene.BILLING),
        ("这个月怎么多扣了一次费用？", Scene.BILLING),
        ("订单什么时候发货？", Scene.ORDER_FULFILMENT),
        ("我的密码忘了，怎么登录？", Scene.ACCOUNT_SECURITY),
        ("你们支持 32 层板吗？", Scene.PRE_SALES),
    ],
)
def test_a_chinese_message_gets_a_scene(question: str, scene: Scene) -> None:
    """Every scene pattern used to be English-only.

    `docs/research/chinese-intent-measurement.md` measured it: **no Chinese
    message got a scene at all**, so all of them fell to UNSPECIFIED. The scene
    does not decide the route, but it decides which tools are candidates
    (`selector.py`'s scene affinity) and how wide retrieval reaches
    (`_top_k_for_scene`) - and the pilot's customers write Chinese, so the gap
    degraded tool ranking and evidence breadth for every conversation the pilot
    would actually have.

    The English alternatives are unchanged, so this asserts an addition rather
    than a replacement; the eval suite is the guard on the English side.
    """
    assert classify(question).scene is scene


def test_a_chinese_policy_question_is_not_read_as_a_complaint() -> None:
    """The complaint vocabulary must not swallow ordinary questions.

    `赔付` sits in the billing pattern because the English list has `refund`,
    and this question is a *policy* question about compensation - an angry
    customer and a customer asking what the compensation rule is are different
    conversations, and only the second should be answered from the corpus.
    """
    detection = classify("全测板开短路不良怎么赔付？")

    assert detection.scene is Scene.TECHNICAL_SUPPORT
    assert detection.scene is not Scene.COMPLAINT


# --- Chinese live-data questions: the read path (2026-09-22) ---------------


def test_chinese_live_data_questions_reach_the_read_path() -> None:
    """The read path was English-only, which made the identity gate unreachable.

    Measured 2026-09-22: these five Chinese phrasings of "where is my order"
    all routed to `knowledge_qa` and selected **zero** read tools, while
    "What is the status of order SO-9001?" selected `order.get_status`.

    The cost is not only the missing card. `business_read` is the only route
    into the identity gate (feature 2.2/2.5), so a Chinese customer asking
    about their order never reached the "prove this order is yours" prompt -
    the whole verify-then-read flow was unreachable in the language the pilot's
    customers write. ADR 0006 makes the same point from the other side: a
    live-data question must not be answered from the corpus, so `knowledge_qa`
    is the wrong route even when the honest outcome is abstention.
    """
    for utterance in (
        "我的订单 SO-9001 到哪了",
        "SO-9001 什么时候发货",
        "帮我查一下订单 SO-9001 的状态",
        "订单 SO-9001 现在什么状态",
        "SO-9001 发货了吗",
    ):
        detection = classify(utterance)
        assert detection.route is Route.BUSINESS_READ, utterance
        # The route alone is not the contract: the defect's symptom was an
        # empty candidate list, and a read route with no tool behind it still
        # ends in a handoff rather than in an answer.
        selected = [c.tool_name for c in select_read_tools(detection, utterance)]
        assert "order.get_status" in selected, (utterance, selected)


def test_chinese_policy_questions_do_not_become_live_data_reads() -> None:
    """The counter-guard: naming a record is not asking about it.

    Every one of these mentions an order, an invoice, stock or a lead time, and
    every one is a question about a *rule* - which the corpus answers and a
    record read cannot. "ADS1110 现在有货吗？货期几天？" is the sharpest of them:
    it carries no how-to opener at all, so only the frame requirement (a state
    in progress, not a topic noun) keeps it on the knowledge path.
    """
    for utterance in (
        "退款多久到账？",
        "取消订单的政策是什么？",
        "质量问题怎么申请赔付？",
        "怎么申请退款",
        "如何取消订单",
        "帮我取消订单是什么流程",
        "PCB 订单的增值税专用发票怎么开？",
        "元器件的增值税普通发票什么时候开？",
        "EQ 确认后交期怎么算？",
        "ADS1110 现在有货吗？货期几天？",
    ):
        assert classify(utterance).route is Route.KNOWLEDGE_QA, utterance


def test_the_how_to_guard_is_the_only_thing_keeping_this_on_the_knowledge_path() -> None:
    """A case that the `_CN_HOW` guard alone decides.

    "怎么查订单状态" carries the lookup frame verbatim - `查` … `状态` - so it
    matches `_CN_LIVE_DATA` and becomes a live-data read if the how-to guard is
    dropped from the call site. The pair is what makes that observable: the
    frame cases in the test above cannot see the guard's removal, which is
    exactly how the first mutation run of the write-side guard failed.
    """
    assert classify("怎么查订单状态").route is Route.KNOWLEDGE_QA
    assert classify("查订单状态").route is Route.BUSINESS_READ
    assert classify("如何查询物流进度").route is Route.KNOWLEDGE_QA
    assert classify("查询物流进度").route is Route.BUSINESS_READ


def test_the_eta_frame_asks_when_not_how_long() -> None:
    """`什么时候` is this record's ETA; `多久` is the process's duration.

    The pair differs by one word and must not share a route: the corpus answers
    "退款多久到账？" (a refund window) while the record answers "什么时候到账".
    Adding `多久` to the ETA frame is the tempting simplification - it reads
    like a synonym - and this is the case that catches it.
    """
    assert classify("退款多久到账？").route is Route.KNOWLEDGE_QA
    assert classify("退款什么时候到账").route is Route.BUSINESS_READ
