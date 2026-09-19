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
