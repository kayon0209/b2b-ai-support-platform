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

from platform_core.agent_runtime.homophones import corrections_in, normalize_for_matching
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


class BusinessLine(StrEnum):
    """Which product line the question belongs to (feature list 3.2).

    Distinct from `Scene`, which is *what the customer wants* (a complaint,
    a pre-sales question). This is *what they are talking about* - a PCB, a
    reel of components, an SMT run. The two are independent: "my PCB order
    is late" is COMPLAINT on the scene axis and PCB here.

    It exists so two downstream consumers stop guessing independently:
    7.3 routes a handoff to the team that owns this line, and 8.7 counts
    demand per line. Both previously had nothing to key on.

    `UNSPECIFIED` is a real answer, not a failure: plenty of questions
    (an invoice query, a password reset) belong to no line, and inventing
    one to avoid the empty case would put a routing decision on noise.
    """

    COMPONENT = "component"
    PCB = "pcb"
    SMT = "smt"
    DFM = "dfm"
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

# Chinese alternatives are appended to each pattern as a second top-level
# alternative (`\b(?:en)\b|(?:cn)`), because `\b` never matches between two CJK
# characters - wrapping CJK in the English `\b` group matches nothing, which is
# how this file's other CJK patterns are written too.
#
# They were missing entirely: `docs/research/chinese-intent-measurement.md`
# measured every scene pattern as English-only, so **no Chinese message got a
# scene at all** and every one fell to UNSPECIFIED. The scenes do not decide the
# route, but they decide which tools are candidates and how wide retrieval
# reaches (`_top_k_for_scene`), and the pilot's customers write Chinese - so the
# gap degraded tool ranking and evidence breadth for every conversation the
# pilot would actually have.
_SCENE_PATTERNS: tuple[tuple[Scene, float, re.Pattern[str]], ...] = (
    (
        Scene.ACCOUNT_SECURITY,
        2.0,
        re.compile(
            r"\b(?:breach\w*|compromi[sz]\w*|locked out|hijack\w*|stolen|"
            r"unauthori[sz]ed access)\b"
            r"|(?:被盗|泄露|泄漏|未授权|被人登录|账号异常|安全漏洞)",
            re.IGNORECASE,
        ),
    ),
    (
        Scene.ACCOUNT_SECURITY,
        1.0,
        re.compile(
            r"\b(?:password|passphrase|mfa|2fa|sso|log ?in|sign ?in|"
            r"api ?key|token|credential|secret|permission\w*|access rights)\b"
            r"|(?:密码|验证码|登录|登入|密钥|凭证|令牌|权限)",
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
            r"disappoint\w*|disgrace|worst)\b"
            r"|(?:投诉|抱怨|太差|很差|不满意|无法接受|不能接受|生气|愤怒|"
            r"还是不行|又出问题|第三次|找经理|找主管|找领导|起诉|律师|"
            r"走法律|失望|太糟糕|最差|什么态度)",
            re.IGNORECASE,
        ),
    ),
    (
        Scene.PRE_SALES,
        2.0,
        re.compile(
            r"\b(?:do you support|does your product|is it possible to|"
            r"before (?:i|we) buy|can your (?:platform|product))\b"
            r"|(?:你们支持|能否支持|是否支持|能支持|买之前|采购前|下单前)",
            re.IGNORECASE,
        ),
    ),
    (
        Scene.PRE_SALES,
        1.0,
        re.compile(
            r"\b(?:pricing|price list|quote|quotation|trial|demo|evaluate|"
            r"considering|compar(?:e|ing)|which plan)\b"
            r"|(?:报价|价格|多少钱|单价|试用|演示|对比|哪个套餐|怎么收费)",
            re.IGNORECASE,
        ),
    ),
    (
        Scene.ORDER_FULFILMENT,
        1.0,
        re.compile(
            r"\b(?:order|shipment|delivery|deliver\w*|track(?:ing)?|parcel|"
            r"dispatch|shipping|eta|where is my|has (?:it|my order) (?:arrived|shipped)|"
            r"invoice|package|courier)\b"
            r"|(?:订单|发货|交期|物流|快递|到货|运单|出货|什么时候发|包裹|"
            r"寄出|签收)",
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
            r"log file|stack trace|exception|restart|reboot)\b"
            r"|(?:报错|故障|失败|崩溃|卡住|超时|不工作|无法使用|打不开|坏了|"
            r"不良|缺陷|短路|开路|虚焊|焊点|固件|硬件版本|型号|序列号|"
            r"日志|异常|重启|死机)",
            re.IGNORECASE,
        ),
    ),
    (
        Scene.BILLING,
        1.0,
        re.compile(
            r"\b(?:refund|charge[ds]?|billing|invoice|payment|credit(?:ed)?|"
            r"subscription|renewal|overcharged|double charged|cancel(?:lation)?|"
            r"downgrade|upgrade|proration)\b"
            r"|(?:退款|退费|退货|扣费|收费|账单|发票|付款|支付|订阅|续费|"
            r"多扣|重复扣|取消|降级|升级|赔付|赔偿)",
            re.IGNORECASE,
        ),
    ),
)

# "售后" is the default for a customer who already has an account and is asking
# about service, but it must not fire on a pre-sales question. BILLING and
# ORDER_FULFILMENT are more specific than AFTER_SALES, so they win when both
# match; AFTER_SALES is only assigned when nothing more specific did.
# Feature list 3.2: which product line the question is about. Weighted like
# the scene patterns - a specific term outranks a generic one - with DFM
# weighted highest because its vocabulary ("拼板", "工艺边") overlaps PCB's
# and the DFM reading is the more specific one: a customer asking about
# panelisation wants the manufacturability review, not a board quote.
#
# `\bpcb\b` deliberately does not match "pcba": the word boundary fails
# between b and a, which is what keeps an assembly question out of the bare
# board line. That distinction is the whole point of having SMT as its own
# line - PCBA is assembly work, PCB is fabrication, and they are routed to
# different teams.
_BUSINESS_LINE_PATTERNS: tuple[tuple[BusinessLine, float, re.Pattern[str]], ...] = (
    (
        BusinessLine.DFM,
        1.2,
        re.compile(
            r"dfm|可制造性|制造性设计|工艺评审|工程确认|工程问题确认|\beq\b|拼板|工艺边"
            r"|邮票孔|钢网开口|可焊性|器件间距|过孔设计",
            re.I,
        ),
    ),
    (
        BusinessLine.SMT,
        1.0,
        re.compile(
            r"smt|pcba|贴片|贴装|回流焊|波峰焊|锡膏|钢网|上锡|焊接|炉温|过炉|元件偏移"
            r"|立碑|虚焊|连锡",
            re.I,
        ),
    ),
    (
        BusinessLine.COMPONENT,
        1.0,
        re.compile(
            r"元器件|电子料|元件|芯片|电阻|电容|电感|晶振|连接器|\bic\b|bom|料号|物料"
            r"|替代料|国产替代|原厂|授权代理|批次|丝印|封装",
            re.I,
        ),
    ),
    (
        BusinessLine.PCB,
        0.8,
        re.compile(
            r"\bpcb\b|电路板|线路板|印制板|印制电路|覆铜板|打样|fr-?4|阻抗|沉金|喷锡"
            r"|层压|板材|孔铜|绿油|阻焊|字符层|板厚|铜厚",
            re.I,
        ),
    ),
)

_BUSINESS_LINE_CONFIDENCE = 0.6

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
    # Feature list 3.2. Defaulted so callers and tests that construct a
    # detection without a line keep working; `classify` always sets it.
    business_line: BusinessLine = BusinessLine.UNSPECIFIED
    # Feature list 3.8: homophone substitutions applied before matching. Kept
    # so "why was this routed by 订单 when the customer wrote 定单" has an
    # answer; the customer's own words stay in the audit log untouched.
    corrections: tuple[tuple[str, str], ...] = ()

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
            "business_line": self.business_line.value,
            # Count, not the substitutions: a correction is a decision about
            # the customer's words, and this snapshot is read back without
            # re-exposing what they typed.
            "spelling_corrections": len(self.corrections),
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


def _detect_business_line(question: str) -> tuple[BusinessLine, float, list[IntentSignal]]:
    """Which product line the question is about, with its evidence.

    Same scoring shape as `_detect_scene`, and deliberately no fallback
    heuristic: unlike a scene, where an existing-customer hint is enough to
    say "after sales", there is no textual hint that makes a question about
    a line it never mentions. Guessing a line would misroute the handoff to
    a team that cannot help, which is worse than saying UNSPECIFIED and
    letting the general queue take it.
    """
    signals: list[IntentSignal] = []
    scores: dict[BusinessLine, float] = {}
    for line, weight, pattern in _BUSINESS_LINE_PATTERNS:
        hits = len(pattern.findall(question))
        if hits:
            signals.append(IntentSignal("business_line", line.value, f"{hits} match(es)"))
            scores[line] = scores.get(line, 0.0) + hits * weight
    best: BusinessLine = BusinessLine.UNSPECIFIED
    best_score = 0.0
    for line, _weight, _pattern in _BUSINESS_LINE_PATTERNS:
        score = scores.get(line, 0.0)
        if score > best_score:
            best, best_score = line, score
    confidence = _BUSINESS_LINE_CONFIDENCE if best is not BusinessLine.UNSPECIFIED else 0.0
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

    if _HUMAN_REQUEST.search(question) or _is_cn_human_request(question):
        # Checked before the write signal and before the quote frame, because
        # a customer asking for a person has made a routing decision, and
        # answering instead - by any machinery - overrules them. The Chinese
        # pattern was missing until the measurement in
        # `docs/research/chinese-intent-measurement.md` showed "把这张单转给人工"
        # and "我要投诉" both landing on the knowledge path.
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
    elif _is_cn_action_request(question):
        # The Chinese frame, evaluated last so an utterance that already
        # matched an English form keeps the sharper rationale. There is no
        # ambiguity in practice - the two vocabularies do not overlap - and
        # ordering it here means the English signals stay byte-identical for
        # every existing case, which is what the evaluation baseline
        # (16/16 -> 24 cases) depends on.
        kinds.append(IntentKind.BUSINESS_ACTION)
        signals.append(
            IntentSignal("kind", IntentKind.BUSINESS_ACTION.value, "chinese request frame")
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


# The words that open a question about *information* rather than a request for
# action, and the auxiliaries the request frame is built from. A wh-word
# directly in front of the frame ("where can I report a bug") makes it the
# wh-word's question; the same frame in a later clause ("...and can I change
# the delivery address") is still a request.
_WH_OPENERS = frozenset({"how", "what", "when", "where", "why", "which", "who", "whose"})
_REQUEST_AUXILIARIES = frozenset({"can", "could", "will", "would"})


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

    **A wh-word immediately followed by the frame is not a request.** "Where
    can I report a bug?" asks *where*, and routing it to the write gateway
    hands a knowledge question to a human. The test is adjacency rather than
    the opener alone, because the sentence above is exactly a wh-opener whose
    request lives in a later clause — rejecting wh-openers outright would
    break the case this function was written for. The defect predates the
    verbs that made it visible ("where can I change my address?" already
    misfired); it was found by a guard case written for `report`, which is
    the argument for writing guard cases rather than reasoning about risk.
    """
    tokens = re.findall(r"[a-z']+", question.lower())
    if len(tokens) > 1 and tokens[0] in _WH_OPENERS and tokens[1] in _REQUEST_AUXILIARIES:
        return False
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


# --- Chinese action detection ----------------------------------------------
#
# Chinese needs its own mechanism rather than more entries in `ACTION_VERBS`,
# and that is not a style preference - the English path *cannot* fire on
# Chinese even with the verbs present:
#
# - `is_action_request` requires the token after the verb to be in
#   `_OBJECT_MARKERS`, which is `{the, a, my, our, this...}`. Chinese has no
#   such determiners, so `帮我取消这个订单` would fail that check however many
#   verbs were added;
# - `_looks_like_a_write` splits on `[a-z']+`, which finds **zero** tokens in a
#   Chinese sentence, so it returns False before looking at anything;
# - `\b` word boundaries never match between two CJK characters, so every
#   `\b...\b` pattern in this module is inapplicable by construction.
#
# The evidence for all three is measured in
# `docs/research/chinese-intent-measurement.md`; the short version is that
# all 14 Chinese utterances tested routed to `knowledge_qa`, including
# "我要退款" and "我要投诉".
#
# So the request frame is expressed directly, in the shape Chinese actually
# uses: a first-person desire or an explicit request for help, followed by a
# write verb, followed by an object.
#
# **The object requirement is carried over deliberately.** English needs it
# (`_looks_like_a_write`'s docstring: without it, 8 of 23 cases moved to
# business_write because a policy question's *topic* is a write verb), and
# Chinese needs it more, because Chinese has no word boundaries and a regex
# can only substring-match. `退款` appears in both "我要退款" (a request) and
# "退款多久到账？" (a policy question). The frame is what separates them:
# `我要` + `退款` is a request, while `退款` followed by `多久` is a topic.

# Verbs the platform *performs*. Mirrors the intent of `ACTION_VERBS` for the
# actions this tenant's flows actually support, rather than translating the
# whole English list - a verb with no tool behind it would only ever produce a
# handoff, and the quote vocabulary's lesson (below) is that a term is worth
# adding when something downstream can act on it.
_CN_ACTION_VERBS: tuple[str, ...] = (
    "退款",
    "退货",
    "退单",
    "取消",
    "退订",
    "修改",
    "更改",
    "变更",
    "改",
    "换",
    "投诉",
    "举报",
    "开票",
    "提交",
    "申请",
    "升级",
    "转人工",
    "转给人工",
)

# Raising a ticket. Kept as patterns rather than verbs because Chinese inserts
# a measure word between the verb and the noun - "建**一张**工单", "开**个**单" -
# so a verb list would match "建单" but miss the way customers actually write it.
# `\S{0,3}` is the measure-word gap and is bounded on purpose: an unbounded gap
# would let "建" and "单" come from different clauses.
_CN_TICKET_REQUEST = re.compile(
    r"(?:建|开|创建|提|发起|生成)\S{0,3}(?:工单|单|问题单|ticket)",
    re.IGNORECASE,
)

# The `把` construction, which puts the object *before* the verb:
# "把这张单转给人工" (take this ticket and transfer it), "请把这个订单取消".
# This is ordinary Chinese word order, not an edge case, so the frame+verb
# scan above cannot see it: there the verb follows the frame directly.
#
# `\S{1,12}` is the object span. Bounded because an unbounded gap would let a
# `把` in one clause pair with a verb in the next, and 12 characters covers
# the objects this vocabulary takes ("这个订单", "刚才那张问题工单").
_CN_BA_CONSTRUCTION = re.compile(r"把\S{1,12}?(?P<verb>" + "|".join(_CN_ACTION_VERBS) + r")")

# First-person desire or explicit request for help. These are the frame; the
# verb alone is a topic.
#
# `我想`/`我要`/`需要` are the desire form (the counterpart of the English
# `_DESIRE_VERBS`), and `帮我`/`麻烦`/`请` are the request form. `请` is
# last and is the weakest: "请说明退款政策" is a request for *information*,
# so it is only honoured when a verb of the platform-acting kind follows
# immediately.
_CN_REQUEST_FRAME = re.compile(
    r"(?:帮我|帮忙|麻烦|我要|我想|我需要|需要|请)"
    r"(?P<verb>" + "|".join(_CN_ACTION_VERBS) + r")"
)

# Objects that make a request concrete. Chinese does not need a determiner,
# but the verb being *followed by something* is what distinguishes an action
# from a compound noun: `申请退款` is a request, while `退款申请流程` (the
# refund-application process) is a topic.
#
# Matched as "any CJK character follows the verb", which is the closest
# available analogue to the English object marker. It is deliberately loose:
# a false positive here only promotes the utterance to the write gateway,
# where `select_write_tools` still has to find a candidate and the confirmation
# step still has to be satisfied by a person - whereas a false *negative*
# leaves a refund request being answered from a policy document.
_CN_OBJECT = re.compile(r"[\u4e00-\u9fff]")

# A procedure question asks to be told how something works, and must not be
# read as a request however many verbs and objects it carries - the Chinese
# counterpart of `_PROCEDURE_QUESTION`. `怎么`/`如何`/`怎样` are the
# interrogative openers, and `吗`/`呢` are sentence-final question particles
# that a request never carries.
_CN_PROCEDURE = re.compile(r"(?:怎么|如何|怎样|什么|哪些|哪|是否|能否|多久|多少|什么时候)")
_CN_QUESTION_PARTICLE = re.compile(r"[吗呢吧]\s*[?？]?\s*$")


def _is_cn_action_request(question: str) -> bool:
    """A Chinese request for the platform to act.

    Deliberately narrow, in the same spirit as `_is_action_request`: the frame
    and the verb must both be present, and a question shape disqualifies the
    utterance outright. The cost of a miss is that the abstention gate still
    decides on the knowledge path; the cost of a false positive is a policy
    question routed to the write gateway, which is worse - so the question
    guard is checked first.
    """
    stripped = question.strip()
    if _CN_PROCEDURE.search(stripped) or _CN_QUESTION_PARTICLE.search(stripped):
        return False
    if _CN_TICKET_REQUEST.search(stripped):
        # The measure-word form, which the frame+verb path cannot see because
        # "建一张工单" has the verb and the noun separated.
        return True
    if _CN_BA_CONSTRUCTION.search(stripped):
        # Object-before-verb, the way Chinese normally phrases an instruction
        # about a specific thing: "把这张单转给人工".
        return True
    match = _CN_REQUEST_FRAME.search(stripped)
    if match is None:
        return False
    # The verb must be followed by an object inside the same clause, so
    # "我要退款" fires but a bare `退款` used as a topic does not.
    rest = stripped[match.end() :]
    if not rest:
        # A bare "我要退款" ends at the verb and is still a request: the verb
        # IS the object ("I want a refund"). Accepted only for the desire
        # frame, where the customer's own want is the request.
        return match.group(0).startswith(("我要", "我想", "我需要"))
    return bool(_CN_OBJECT.match(rest))


# The Chinese counterpart of `_HUMAN_REQUEST`. Kept separate from the action
# vocabulary because asking for a person is a *routing decision the customer
# has already made*, and `_HUMAN_REQUEST` documents it as honoured
# "immediately and unconditionally" - which was true for English only.
_CN_HUMAN_REQUEST = re.compile(r"(?:人工客服|人工|客服|真人|专员|经理|转人工|转给人工|找个?人)")


def _is_cn_human_request(question: str) -> bool:
    """A Chinese request for a person, as opposed to a question about one.

    `转人工` appears in both "转人工客服" (transfer me) and "为什么要转人工"
    (why does it transfer). Treating the second as a request overrules the
    customer - the exact failure `_HUMAN_REQUEST`'s "immediately and
    unconditionally" promise exists to prevent - so the question shape vetoes
    the match, the same way it does on the write path.

    Kept as a function rather than an inline `and not` so the ordered
    precedence is local to the Chinese branch: English `_HUMAN_REQUEST` is
    left untouched, so no existing English case changes its signals.
    """
    if not _CN_HUMAN_REQUEST.search(question):
        return False
    stripped = question.strip()
    return not (_CN_PROCEDURE.search(stripped) or _CN_QUESTION_PARTICLE.search(stripped))


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
    # 3.8: classify against a homophone-corrected copy of the utterance. A
    # pinyin slip ("定单" for "订单") otherwise misses the vocabulary entirely
    # and the question is routed by whatever else it happens to contain - or by
    # nothing. The customer's own text is not rewritten anywhere: this copy
    # exists only for matching, and the substitutions are recorded so the
    # routing decision stays explainable.
    corrections = corrections_in(question)
    matching_text = normalize_for_matching(question)

    scene, scene_confidence, scene_signals = _detect_scene(matching_text)
    kinds, kind_confidence, kind_signals = _detect_kinds(matching_text)
    line, _line_confidence, line_signals = _detect_business_line(matching_text)
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
        signals=tuple(scene_signals + kind_signals + line_signals),
        business_line=line,
        corrections=corrections,
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
