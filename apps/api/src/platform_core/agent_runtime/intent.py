"""Intent classification: scene, kind, route and the answer/clarify/handoff
decision.

Why this replaces a keyword list
--------------------------------
`orchestrator.classify_route` was a two-way test: does the question contain a
credential word, yes or no. Everything else was `knowledge_qa`. That is not an
intent taxonomy, and it fails the way a two-way test always does — by making
the platform unable to say what it was asked:

- a refund *request* and a refund *policy question* both contain "refund", and
  they need opposite handling (tool gateway with confirmation vs. retrieval);
- "where is my order" and "what is your returns policy" are both questions, but
  the first is a live query against an order system and the second is a
  document lookup — only the second can be answered from RAG;
- a complaint ("this is the third time I've been charged wrongly") routes to
  retrieval and comes back with a policy passage, which reads as the platform
  not having heard the customer.

`docs/agent.md` already specifies the target: seven routing classes
(`KNOWLEDGE_QA`, `CASE_STATUS`, `BUSINESS_READ`, `BUSINESS_WRITE`, `SENSITIVE`,
`OUT_OF_SCOPE`, `HUMAN_REQUIRED`). What was missing was anything that produced
them. This module is that thing.

Two axes, because one is not enough
-----------------------------------
Support questions are usually described along two axes that are independent:

- **scene** — what domain of the business this is about (pre-sales, after-sales,
  an order, a technical fault, a complaint). This is what decides *who* should
  own the conversation and which knowledge space is authoritative.
- **kind** — what the customer wants done (be told something, be shown live
  data, have something changed, or reach a person). This is what decides
  *which machinery* runs: retrieval, a read tool, the write gateway with
  confirmation, or a handoff.

Conflating them is the usual design error: "refund" names a scene and "cancel"
names a kind, so a flat label set cannot express "a refund-scene question that
is a request to act". Keeping them separate also answers the "should this be
one agent or many" question with evidence instead of taste — the scene axis is
what a future per-scene agent would split on, and the kind axis is what must
stay centralized because it is where authorization lives.

Multi-intent
------------
A single query routinely carries several intents ("where is my order and can I
change the delivery address?"). Collapsing that to one label silently drops
half the request. `classify` returns **all** matched intents with a primary, and
`multi_intent` is reported rather than resolved: choosing which to act on is a
routing decision with business consequences (does the platform answer the
lookup and defer the change?), and the correct default is to surface it and
answer the primary, not to guess.

Determinism and scope
---------------------
No model call. Classification here decides *routing*, and a routing decision is
an authorization-adjacent one: a customer-visible answer must not depend on a
non-deterministic classifier's mood. Model-based classification belongs behind
an evaluation gate and its own ADR, and it should be measured against this one
rather than replacing it silently.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from platform_core.agent_runtime.qa_path import _content_terms, _stem

# --- Reused vocabulary -----------------------------------------------------
#
# The action-verb vocabulary is owned by `qa_path` (the abstention gate) and is
# imported rather than retyped. That is not tidiness: two lists of "words that
# mean the customer wants a write" drift, and then a question can be an action
# request for one gate and a knowledge question for the other — the platform
# would answer it and abstain from it in the same run.

# Terms that must never be answered by the AI regardless of evidence
# (docs/agent.md SENSITIVE routing class).
#
# Moved here from `orchestrator.py`, where this list was the entirety of the
# classifier. It is a *routing* vocabulary, so it belongs with the rest of the
# routing vocabulary; `orchestrator` re-exports it so existing call sites and
# tests keep resolving the same name.
RESTRICTED_TERMS: tuple[str, ...] = (
    "password",
    "credential",
    "api key",
    "social security",
    "credit card number",
    "bank account",
)


class Scene(StrEnum):
    """What domain of the business the question is about."""

    PRE_SALES = "pre_sales"
    AFTER_SALES = "after_sales"
    ORDER_FULFILMENT = "order_fulfilment"
    TECHNICAL_SUPPORT = "technical_support"
    COMPLAINT = "complaint"
    ACCOUNT_SECURITY = "account_security"
    BILLING = "billing"
    UNSPECIFIED = "unspecified"


class IntentKind(StrEnum):
    """What the customer wants done."""

    KNOWLEDGE_QUESTION = "knowledge_question"
    BUSINESS_QUERY = "business_query"
    BUSINESS_ACTION = "business_action"
    SALES_INQUIRY = "sales_inquiry"
    SENSITIVE_REQUEST = "sensitive_request"
    HUMAN_REQUEST = "human_request"
    SOCIAL = "social"
    OUT_OF_DOMAIN = "out_of_domain"


class Route(StrEnum):
    """The seven routing classes from docs/agent.md."""

    KNOWLEDGE_QA = "knowledge_qa"
    CASE_STATUS = "case_status"
    BUSINESS_READ = "business_read"
    BUSINESS_WRITE = "business_write"
    SENSITIVE = "sensitive"
    OUT_OF_SCOPE = "out_of_scope"
    HUMAN_REQUIRED = "human_required"


class IntentAction(StrEnum):
    """What the platform should do, independent of whether it succeeds."""

    ANSWER_FROM_KNOWLEDGE = "answer_from_knowledge"
    CALL_READ_TOOL = "call_read_tool"
    PROPOSE_WRITE = "propose_write"
    CLARIFY = "clarify"
    HANDOFF = "handoff"
    ACKNOWLEDGE = "acknowledge"


# --- Scene evidence --------------------------------------------------------
#
# Each entry is (scene, weight, pattern). Order matters only for tie-breaking,
# which is decided by accumulated score first and list position second.
#
# Weights exist because signals are not equally diagnostic. A pre-sales frame
# ("do you support X") declares what kind of conversation this is, and it
# outranks a topical token ("SSO") that can appear in any frame; a claim of
# account compromise ("we were breached") is always a security matter however
# politely phrased. A flat pattern list could not express that, and the tie it
# produced sent "Do you support SAML SSO?" to the security scene — the wrong
# corpus, confidently.
#
# Deliberately lexical and narrow. These set which knowledge space is
# authoritative, so a false positive here is worse than a miss: routing a
# billing question to the technical space retrieves the wrong corpus and the
# answer is confidently wrong, whereas UNSPECIFIED merely widens the search.
#
# Security terms are stem-shaped (`compromi[sz]\w*`) because "compromised"
# does not end where the stem does: a literal `compromis\b` matched nothing,
# and the scene the wording was asking for lost to whatever scene happened to
# be listed first.

_SCENE_PATTERNS: tuple[tuple[Scene, float, re.Pattern[str]], ...] = (
    (
        Scene.ACCOUNT_SECURITY,
        2.0,
        re.compile(
            r"\b(?:breach\w*|compromi[sz]\w*|locked out|hijack\w*|stolen|"
            r"unauthori[sz]ed access)\b",
            re.IGNORECASE,
        ),
    ),
    (
        Scene.ACCOUNT_SECURITY,
        1.0,
        re.compile(
            r"\b(?:password|passphrase|mfa|2fa|sso|log ?in|sign ?in|"
            r"api ?key|token|credential|secret|permission\w*|access rights)\b",
            re.IGNORECASE,
        ),
    ),
    (
        Scene.COMPLAINT,
        1.0,
        re.compile(
            r"\b(?:complain\w*|unacceptable|ridiculous|furious|angry|"
            r"fed up|again and again|third time|still not|escalat\w*|"
            r"manager|supervisor|legal action|lawyer|ombudsman|"
            r"disappoint\w*|disgrace|worst)\b",
            re.IGNORECASE,
        ),
    ),
    (
        Scene.PRE_SALES,
        2.0,
        re.compile(
            r"\b(?:do you support|does your product|is it possible to|"
            r"before (?:i|we) buy|can your (?:platform|product))\b",
            re.IGNORECASE,
        ),
    ),
    (
        Scene.PRE_SALES,
        1.0,
        re.compile(
            r"\b(?:pricing|price list|quote|quotation|trial|demo|evaluate|"
            r"considering|compar(?:e|ing)|which plan)\b",
            re.IGNORECASE,
        ),
    ),
    (
        Scene.ORDER_FULFILMENT,
        1.0,
        re.compile(
            r"\b(?:order|shipment|delivery|deliver\w*|track(?:ing)?|parcel|"
            r"dispatch|shipping|eta|where is my|has (?:it|my order) (?:arrived|shipped)|"
            r"invoice|package|courier)\b",
            re.IGNORECASE,
        ),
    ),
    (
        Scene.TECHNICAL_SUPPORT,
        1.0,
        re.compile(
            r"\b(?:error|fault|fail(?:ing|ed|ure)?|crash\w*|hang|timeout|time ?out|"
            r"error code|fault code|not working|broken|defect|"
            r"firmware|hardware version|model number|serial|"
            r"log file|stack trace|exception|restart|reboot)\b",
            re.IGNORECASE,
        ),
    ),
    (
        Scene.BILLING,
        1.0,
        re.compile(
            r"\b(?:refund|charge[ds]?|billing|invoice|payment|credit(?:ed)?|"
            r"subscription|renewal|overcharged|double charged|cancel(?:lation)?|"
            r"downgrade|upgrade|proration)\b",
            re.IGNORECASE,
        ),
    ),
)

# "售后" is the default for a customer who already has an account and is asking
# about service, but it must not fire on a pre-sales question. BILLING and
# ORDER_FULFILMENT are more specific than AFTER_SALES, so they win when both
# match; AFTER_SALES is only assigned when nothing more specific did.
_AFTER_SALES_HINT = re.compile(
    r"\b(?:my (?:account|plan|subscription|workspace)|existing customer|"
    r"since (?:i|we) (?:signed|upgraded|started)|support ticket|my case)\b",
    re.IGNORECASE,
)


# --- Kind evidence ---------------------------------------------------------

# Asking for live data that only a business system holds. These are the
# questions RAG must NOT answer: an order's status is not in the knowledge
# corpus, and if it were, it would be stale the moment it was indexed.
_LIVE_DATA = re.compile(
    r"\b(?:current status|status of|latest|right now|at the moment|"
    r"has (?:it|my order|the ticket) (?:been|arrived|shipped|resolved)|"
    r"where is|when will (?:it|my|the) (?:arrive|ship|be delivered)|"
    r"balance|entitlement|my (?:plan|tier|contract|quota|usage))\b",
    re.IGNORECASE,
)

# Asking for an internal record.
_CASE_RECORD = re.compile(
    r"\b(?:case\s*#?\s*[0-9]{3,12}|ticket\s*#?\s*[0-9]{3,12}|"
    r"my (?:case|ticket)|support (?:case|ticket))\b",
    re.IGNORECASE,
)

# Explicit request for a human. Honoured immediately and unconditionally: a
# customer who asks for a person has made a routing decision, and answering
# instead is the platform overruling them.
_HUMAN_REQUEST = re.compile(
    r"\b(?:human|real person|agent|representative|speak (?:to|with) (?:a |someone|somebody)|"
    r"talk to (?:a |someone|somebody)|connect me|transfer me|put me through|"
    r"customer service|live (?:agent|person)|manager)\b",
    re.IGNORECASE,
)

# A quote/price request. Deliberately broad on the price words but they only
# ever *route to a human* - the cost of over-matching is a handoff, while the
# cost of a missed quote answered from a stale corpus page is a commercial
# commitment the company never made.
_QUOTE_REQUEST = re.compile(
    r"多少钱|怎么收费|报个?价|价格是多少|给我报|quote|how much (?:is|would|does)|"
    r"price (?:for|of|quote)|can you (?:give|provide) (?:me )?a (?:price|quote)",
    re.IGNORECASE,
)

# Write verbs: the customer wants something changed. Reused from `qa_path`, so
# the action-request gate and this classifier cannot disagree about what counts
# as a request to act.
from platform_core.agent_runtime.qa_path import (  # noqa: E402 - documented above
    action_verbs,
    is_action_request,
    object_markers,
)

# Greetings and social turns. Answering these from the knowledge base is how a
# platform comes to reply to "thanks" with a policy excerpt.
_SOCIAL = re.compile(
    r"^\s*(?:hi|hey|hello|hiya|good (?:morning|afternoon|evening)|thanks|thank you|"
    r"ty|cheers|ok|okay|bye|goodbye|see you|no worries|perfect|great|"
    r"got it|understood|sounds good)\b[\s!.,?]*$",
    re.IGNORECASE,
)

# Personal / legal / HR data and anything about identifiable individuals.
# `docs/agent.md` puts these in SENSITIVE: the risk is not that the answer is
# wrong but that it is *right* — disclosing it is the harm.
_SENSITIVE = re.compile(
    r"\b(?:home address|personal address|salary|compensation package|"
    r"employee record|hr record|disciplinary|medical|health record|"
    r"which customers|customer list|other customers|who else|"
    r"churn|acquisition|stock price|share price|valuation|"
    r"roadmap|unreleased|internal only|confidential)\b",
    re.IGNORECASE,
)

# A question shape, as opposed to a statement or an instruction. Used to tell
# "how do I cancel" (a question about a procedure → knowledge) from "cancel my
# plan" (an instruction → write).
_INTERROGATIVE = re.compile(
    r"\b(?:what|when|where|why|how|which|who|whose|is|are|was|were|do|does|did|"
    r"can|could|would|should|shall|may|am)\b",
    re.IGNORECASE,
)

# Confidence: what fraction of the signal a single lexical match is worth. A
# classifier that reports 1.0 because one keyword matched is claiming a
# certainty it does not have, and a downstream reader cannot tell a strong
# match from a weak one.
_SCENE_MATCH_CONFIDENCE = 0.6
_KIND_MATCH_CONFIDENCE = 0.7
_ACTION_CONFIDENCE = 0.9  # an imperative with an object is a strong signal
# Below this, the classification is reported but treated as UNSPECIFIED when
# deciding the action: acting confidently on a weak signal is the failure mode
# that turns a knowledge question into a handoff.
CONFIDENT_THRESHOLD = 0.5


@dataclass(frozen=True)
class IntentSignal:
    """One piece of evidence behind a classification."""

    axis: str  # "scene" | "kind"
    label: str
    detail: str


@dataclass(frozen=True)
class IntentDetection:
    """The result of classifying one utterance.

    `primary_kind` and `secondary_kinds` together are the multi-intent answer:
    the platform acts on the primary and reports the rest rather than dropping
    them. `route` is the docs/agent.md routing class; `action` is what the
    platform should then do.
    """

    scene: Scene
    primary_kind: IntentKind
    secondary_kinds: tuple[IntentKind, ...]
    route: Route
    action: IntentAction
    confidence: float
    multi_intent: bool
    signals: tuple[IntentSignal, ...]

    def as_dict(self) -> dict[str, Any]:
        """Audit-safe snapshot for `AgentRun.model_config`.

        No question text: the run already stores `input_hash`, and a
        classification is only worth recording if it can be read back without
        re-exposing what the customer typed.
        """
        return {
            "scene": self.scene.value,
            "primary_kind": self.primary_kind.value,
            "secondary_kinds": [k.value for k in self.secondary_kinds],
            "route": self.route.value,
            "action": self.action.value,
            "confidence": round(self.confidence, 3),
            "multi_intent": self.multi_intent,
        }


def _detect_scene(question: str) -> tuple[Scene, float, list[IntentSignal]]:
    """Highest-scoring scene, its confidence, and the evidence.

    Score is the sum of `hits * weight` across a scene's patterns, so a
    diagnostic frame outranks a topical token and two weak hits still lose to
    one strong one. Ties fall back to list order, which is a stable
    documented choice, not a quality claim."""
    signals: list[IntentSignal] = []
    scores: dict[Scene, float] = {}
    for scene, weight, pattern in _SCENE_PATTERNS:
        hits = len(pattern.findall(question))
        if hits:
            signals.append(IntentSignal("scene", scene.value, f"{hits} match(es)"))
            scores[scene] = scores.get(scene, 0.0) + hits * weight
    best: Scene = Scene.UNSPECIFIED
    best_score = 0.0
    for scene, _weight, _pattern in _SCENE_PATTERNS:
        score = scores.get(scene, 0.0)
        if score > best_score:
            best, best_score = scene, score
    if best is Scene.UNSPECIFIED and _AFTER_SALES_HINT.search(question):
        best = Scene.AFTER_SALES
        signals.append(IntentSignal("scene", best.value, "existing-customer hint"))
    confidence = _SCENE_MATCH_CONFIDENCE if best is not Scene.UNSPECIFIED else 0.0
    return best, confidence, signals


def _detect_kinds(question: str) -> tuple[list[IntentKind], float, list[IntentSignal]]:
    """Every kind the utterance evidences, strongest first."""
    signals: list[IntentSignal] = []
    kinds: list[IntentKind] = []

    # Whether the utterance is shaped like a question. Decided once, at the
    # top, because it gates the *weak* write signal below: "cancel my plan" is
    # an instruction and "how do I cancel my plan?" is a question, and only the
    # punctuation and the opener distinguish them.
    is_question = bool(_INTERROGATIVE.search(question))

    lowered = question.lower()
    if any(term in lowered for term in RESTRICTED_TERMS) or _SENSITIVE.search(question):
        # Checked first and never overridden: a sensitive request is sensitive
        # even when it is also a question about policy. Disclosure is the harm,
        # and "it was phrased as a question" is not a defence.
        kinds.append(IntentKind.SENSITIVE_REQUEST)
        signals.append(IntentSignal("kind", IntentKind.SENSITIVE_REQUEST.value, "sensitive term"))
        return kinds, 0.95, signals

    if _HUMAN_REQUEST.search(question):
        kinds.append(IntentKind.HUMAN_REQUEST)
        signals.append(
            IntentSignal("kind", IntentKind.HUMAN_REQUEST.value, "explicit human request")
        )

    if _QUOTE_REQUEST.search(question):
        # B2B red line (huqiu research, L5): a quote is a multi-parameter
        # commercial commitment, not a question. The model never answers it -
        # not from the corpus, not from a tool. It is collected as a sales
        # inquiry and handed to a human with the parameters gathered so far.
        kinds.append(IntentKind.SALES_INQUIRY)
        signals.append(IntentSignal("kind", IntentKind.SALES_INQUIRY.value, "quote request"))

    if _SOCIAL.match(question):
        kinds.append(IntentKind.SOCIAL)
        signals.append(IntentSignal("kind", IntentKind.SOCIAL.value, "greeting or closing"))

    # An action request is the write path. `is_action_request` is the same
    # gate the QA path uses to refuse writes, so the two agree by construction.
    #
    # The weak signal is gated on `not is_question`: a question about a write
    # ("how do I cancel?") is a *procedure* question and belongs on the
    # knowledge path. Without that guard the weak signal outranked
    # KNOWLEDGE_QUESTION on precedence alone and every "how do I cancel" case
    # routed to the write gateway.
    #
    # The request frame is the one interrogative that still names a write:
    # "can I change the delivery address?" asks the platform to change it —
    # the customer cannot change their own delivery address, so the polite
    # form is the request. "can I get a refund?" stays a possibility question
    # because "get" is not a write verb; only verbs the platform *performs*
    # (the shared ACTION_VERBS set) fire here.
    if is_action_request(question):
        kinds.append(IntentKind.BUSINESS_ACTION)
        signals.append(
            IntentSignal("kind", IntentKind.BUSINESS_ACTION.value, "imperative with object")
        )
    elif _has_request_frame(question):
        kinds.append(IntentKind.BUSINESS_ACTION)
        signals.append(
            IntentSignal("kind", IntentKind.BUSINESS_ACTION.value, "polite request frame")
        )
    elif not is_question and _looks_like_a_write(question):
        kinds.append(IntentKind.BUSINESS_ACTION)
        signals.append(
            IntentSignal("kind", IntentKind.BUSINESS_ACTION.value, "write verb with object")
        )

    if _CASE_RECORD.search(question):
        kinds.append(IntentKind.BUSINESS_QUERY)
        signals.append(IntentSignal("kind", IntentKind.BUSINESS_QUERY.value, "case reference"))
    elif _LIVE_DATA.search(question):
        kinds.append(IntentKind.BUSINESS_QUERY)
        signals.append(IntentSignal("kind", IntentKind.BUSINESS_QUERY.value, "live data requested"))

    if is_question or _content_terms(question):
        kinds.append(IntentKind.KNOWLEDGE_QUESTION)
        signals.append(IntentSignal("kind", IntentKind.KNOWLEDGE_QUESTION.value, "question shape"))

    if not kinds:
        kinds.append(IntentKind.OUT_OF_DOMAIN)
        signals.append(IntentSignal("kind", IntentKind.OUT_OF_DOMAIN.value, "no signal"))

    return _ordered_kinds(kinds), _kind_confidence(kinds, question), signals


def _looks_like_a_write(question: str) -> bool:
    """A write verb with an object, but not in imperative form.

    "please cancel my plan" and "I need the invoice refunded" are requests to
    act that `is_action_request` does not catch (it is deliberately narrow), so
    this is the weaker second net — reported at lower confidence.

    **The object requirement is the whole point, and it was the bug.** The
    first version matched a bare write verb anywhere in the sentence, which
    classified "How long do I have to request a refund?" and "How quickly are
    new workspaces provisioned?" as *write requests* — because `refund` and
    `provision` are both nouns and write verbs, and a policy question is mostly
    made of the noun. Measured over the evaluation dataset, that sent 8 of 23
    cases to `business_write`, including five that must be answered from the
    knowledge base. A write signal that fires on the *topic* of a question is
    not a write signal.

    Requiring an object marker after the verb separates "refund **the** last
    invoice" (act) from "refund **window**" (topic). It misses passive
    constructions ("my subscription needs to be cancelled"), which is the right
    way to be wrong: a missed weak signal leaves the question on the knowledge
    path, where the abstention gate still decides, whereas a false one would
    route a policy question toward the write gateway.
    """
    tokens = re.findall(r"[a-z']+", question.lower())
    for index, token in enumerate(tokens):
        if _stem(token) in action_verbs:
            rest = tokens[index + 1 :]
            if rest and rest[0] in object_markers:
                return True
    return False


def _has_request_frame(question: str) -> bool:
    """`can|could|will|would + I|we + <write verb> + <object marker>`.

    A polite first-person request for the platform to act. Distinct from
    `is_action_request`'s agent-pronoun form ("can **you** refund") and from
    the possibility reading ("can I **get** a refund"): the verb must be one
    the platform performs, and it must carry an object, so entitlement
    questions never fire this.

    Exists because the weak write net is gated off inside questions — the
    gate that protects "how do I cancel?" — and that same gate hid the write
    half of "Where is my order, and can I change the delivery address?", a
    request the platform must not silently drop.
    """
    tokens = re.findall(r"[a-z']+", question.lower())
    for index, token in enumerate(tokens[:-2]):
        if token not in {"can", "could", "will", "would"}:
            continue
        if tokens[index + 1] not in {"i", "we"}:
            continue
        verb = tokens[index + 2]
        obj = tokens[index + 3] if index + 3 < len(tokens) else None
        if _stem(verb) in action_verbs and obj in object_markers:
            return True
    return False


# A procedure question asks to be *told* how something works. This — not any
# interrogative — is the shape the write discount exists for: "how do I
# cancel?" is procedure, while "can I change the delivery address?" is a
# request and must not be discounted into a clarification.
_PROCEDURE_QUESTION = re.compile(r"^\s*how\b", re.IGNORECASE)


# Precedence when several kinds match. The ordering encodes a safety rule, not
# a frequency estimate: disclosure beats everything, then a human request, then
# a write, then a read, then the default knowledge question. A customer cannot
# accidentally be routed *out* of a safety class by also asking a question.
_KIND_PRECEDENCE: tuple[IntentKind, ...] = (
    IntentKind.SENSITIVE_REQUEST,
    IntentKind.HUMAN_REQUEST,
    IntentKind.SALES_INQUIRY,
    IntentKind.BUSINESS_ACTION,
    IntentKind.BUSINESS_QUERY,
    IntentKind.SOCIAL,
    IntentKind.OUT_OF_DOMAIN,
    IntentKind.KNOWLEDGE_QUESTION,
)


def _ordered_kinds(kinds: list[IntentKind]) -> list[IntentKind]:
    seen: list[IntentKind] = []
    for kind in _KIND_PRECEDENCE:
        if kind in kinds and kind not in seen:
            seen.append(kind)
    return seen


def _kind_confidence(kinds: list[IntentKind], question: str) -> float:
    """Confidence in the primary kind.

    Three deductions, all because a lexical match is weaker than it looks:

    - a bare imperative with an object is the strongest write signal; the
      polite prefix and the request frame are graded below it, because a
      softened request is more often negotiable than a command;
    - an action detected only by a bare write verb is weaker still;
    - a *procedure* question ("how do I cancel?") discounts its write signal
      heavily — this is the single most common false positive in the
      taxonomy. The discount is scoped to "how" questions: applying it to
      every interrogative turned "can I change the delivery address?" into a
      clarification, which is not an answer to what was asked.
    """
    if not kinds:
        return 0.0
    primary = kinds[0]
    if primary is IntentKind.SENSITIVE_REQUEST:
        return 0.95
    if primary is IntentKind.HUMAN_REQUEST:
        return 0.9
    if primary is IntentKind.SALES_INQUIRY:
        return 0.85
    if primary is IntentKind.BUSINESS_ACTION:
        if is_action_request(question):
            head = re.findall(r"[a-z']+", question.lower())[0]
            base = (
                _ACTION_CONFIDENCE
                if head not in {"please", "kindly"} and not _has_request_frame(question)
                else _KIND_MATCH_CONFIDENCE + 0.05
            )
        elif _has_request_frame(question):
            base = _KIND_MATCH_CONFIDENCE + 0.05
        else:
            base = _KIND_MATCH_CONFIDENCE
        if _PROCEDURE_QUESTION.match(question.strip()):
            base *= 0.4
        return base
    if primary is IntentKind.BUSINESS_QUERY:
        return _KIND_MATCH_CONFIDENCE
    if primary is IntentKind.SOCIAL:
        return 0.85
    if primary is IntentKind.OUT_OF_DOMAIN:
        return 0.3
    return _KIND_MATCH_CONFIDENCE


_ROUTE_FOR_KIND: dict[IntentKind, Route] = {
    IntentKind.SALES_INQUIRY: Route.HUMAN_REQUIRED,
    IntentKind.KNOWLEDGE_QUESTION: Route.KNOWLEDGE_QA,
    IntentKind.BUSINESS_QUERY: Route.BUSINESS_READ,
    IntentKind.BUSINESS_ACTION: Route.BUSINESS_WRITE,
    IntentKind.SENSITIVE_REQUEST: Route.SENSITIVE,
    IntentKind.HUMAN_REQUEST: Route.HUMAN_REQUIRED,
    IntentKind.SOCIAL: Route.OUT_OF_SCOPE,
    IntentKind.OUT_OF_DOMAIN: Route.OUT_OF_SCOPE,
}

_ACTION_FOR_KIND: dict[IntentKind, IntentAction] = {
    IntentKind.SALES_INQUIRY: IntentAction.HANDOFF,
    IntentKind.KNOWLEDGE_QUESTION: IntentAction.ANSWER_FROM_KNOWLEDGE,
    IntentKind.BUSINESS_QUERY: IntentAction.CALL_READ_TOOL,
    IntentKind.BUSINESS_ACTION: IntentAction.PROPOSE_WRITE,
    IntentKind.SENSITIVE_REQUEST: IntentAction.HANDOFF,
    IntentKind.HUMAN_REQUEST: IntentAction.HANDOFF,
    IntentKind.SOCIAL: IntentAction.ACKNOWLEDGE,
    IntentKind.OUT_OF_DOMAIN: IntentAction.HANDOFF,
}


def classify(question: str) -> IntentDetection:
    """Classify one utterance on both axes.

    The returned `action` is a recommendation about *machinery*, never a
    verdict about the answer. `ANSWER_FROM_KNOWLEDGE` does not mean "there is
    an answer" — it means "the knowledge path is the right one to try", and the
    abstention gate still decides whether there is anything to say. Collapsing
    those two is how a router becomes a confidence model, which
    `docs/agent.md` forbids ("similarity scores are ranking signals, not
    confidence probabilities").

    Below `CONFIDENT_THRESHOLD` the action degrades to `CLARIFY` rather than to
    a guess: an uncertain routing decision is cheaper to resolve with one
    question than to discover from a wrong answer.
    """
    scene, scene_confidence, scene_signals = _detect_scene(question)
    kinds, kind_confidence, kind_signals = _detect_kinds(question)
    primary = kinds[0]
    secondary = tuple(kinds[1:])

    confidence = max(scene_confidence, kind_confidence)
    action = _ACTION_FOR_KIND[primary]
    # Below the threshold the action degrades to CLARIFY, with one exception:
    # the knowledge path is allowed to proceed unconfidently because it has an
    # abstention gate behind it that will refuse if the evidence is not there.
    # A write or a handoff made on a weak signal has no such gate, so those are
    # the ones that must be confirmed with the customer first.
    if kind_confidence < CONFIDENT_THRESHOLD and action is not IntentAction.ANSWER_FROM_KNOWLEDGE:
        action = IntentAction.CLARIFY

    return IntentDetection(
        scene=scene,
        primary_kind=primary,
        secondary_kinds=secondary,
        route=_ROUTE_FOR_KIND[primary],
        action=action,
        confidence=confidence,
        multi_intent=len(kinds) > 1,
        signals=tuple(scene_signals + kind_signals),
    )


def classify_route(question: str) -> str:
    """The routing class for a question.

    Kept as a function (and kept returning the docs/agent.md route string) so
    the orchestrator's call site reads the same as it did when this was a
    two-way keyword test. It now returns one of seven classes instead of two.
    """
    return classify(question).route.value


# Routes whose handling is settled before retrieval runs: there is nothing to
# retrieve for them, and spending a model call would only produce an answer
# that must then be suppressed. A human request and a sensitive request are
# decided by *who asked*, not by what the corpus says.
PRE_RETRIEVAL_ROUTES: frozenset[str] = frozenset(
    {Route.HUMAN_REQUIRED.value, Route.SENSITIVE.value}
)

# Routes that must never produce a knowledge answer even if retrieval finds
# something: a passage that happens to mention refunds does not authorise the
# platform to answer a refund request, and an out-of-scope question has no
# corpus to be answered from.
NON_ANSWERABLE_ROUTES: frozenset[str] = PRE_RETRIEVAL_ROUTES | {Route.OUT_OF_SCOPE.value}


__all__: list[str] = [
    "CONFIDENT_THRESHOLD",
    "IntentAction",
    "IntentDetection",
    "IntentKind",
    "IntentSignal",
    "NON_ANSWERABLE_ROUTES",
    "PRE_RETRIEVAL_ROUTES",
    "Route",
    "Scene",
    "classify",
    "classify_route",
]
