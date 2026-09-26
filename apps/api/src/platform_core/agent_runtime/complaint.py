"""Is this turn a claim against the company rather than a question about it?

The research report (`docs/research/huaqiu-research.md` 2.1/2.2) puts quality
complaints and compensation at **L6 争议归责**: 必须转人工, and the red line is
explicit - 绝不可做归责表态或赔付承诺. A payout is an authority decision, not a
retrieval result, so the answer path must not answer it at all.

The tempting implementation is to gate on `Scene.COMPLAINT`, since that is what
the scene axis is for. **Measured, that is wrong**: `_SCENE_PATTERNS` counts
"still not" and "third time" as complaint signals, so

    "my order has still not arrived"   -> Scene.COMPLAINT, Route.KNOWLEDGE_QA
    "The shipment still not updated"   -> Scene.COMPLAINT, Route.BUSINESS_READ

A scene gate would hand an order-status question to a human queue and, in the
second case, would do it before the read tool that could simply answer it. So
the scene is deliberately **not** consulted here; this module detects the thing
the report actually describes - a customer asserting a claim and asking for a
remedy.

The other half of the design is what must stay answerable, because the same
report puts 赔付政策/退换货规则 at **L1** (检索 + 引用, 可直接回答). "怎么赔付"
asks how the policy works and is a knowledge question; "我要索赔" claims under
it and is not. The discriminator is therefore **question-about-the-process vs.
claim-under-it**, which is the same shape as `confirmation.py`'s question veto.

Deliberately lexical, for the same reason tool selection is: this decides
whether a person takes over, and a non-deterministic chooser would make the
audit trail describe a coin flip.
"""

from __future__ import annotations

import re

# --- The remedy being claimed -------------------------------------------------
#
# Money or remedy words: compensation, refund, return, replacement, escalation,
# legal. Substring matching for Chinese (no word boundaries); whole-token for
# Latin script, so "refundable" is not read as a claim for a refund - it is an
# adjective in a question about eligibility ("Are monthly plans refundable?",
# an evaluation case that must be *answered*).
_REMEDY_CJK = (
    "索赔",
    "赔偿",
    "赔付",
    "理赔",
    "退款",
    "退货",
    "换货",
    "投诉",
    "起诉",
    "律师",
    "法院",
    "找经理",
    "找主管",
    "找领导",
    "赔我",
    "赔钱",
    "补偿",
)

_REMEDY_EN = frozenset(
    {
        "refund",
        "refunds",
        "compensate",
        "compensation",
        "claim",
        "claims",
        "complaint",
        "complaints",
        "escalate",
        "escalation",
        "manager",
        "supervisor",
        "lawyer",
        "legal",
        "sue",
        "lawsuit",
        "redress",
    }
)

# --- Who is asking for it -----------------------------------------------------
#
# A remedy word on its own is a topic. What makes it a claim is agency: the
# customer wants it done, is demanding it, or is asking for it to happen.
# Phrases rather than single words, because single words here ("can", "i") are
# in half of all English questions.
_DEMAND_CJK = (
    "我要",
    "我要求",
    "我们要",
    "要求你们",
    "需要你们",
    "请给",
    "给我",
    "得给我",
    "必须给",
    "赔给我",
    "投诉你们",
    "申请退款",
    "申请赔偿",
)

_DEMAND_EN = (
    "i want",
    "i demand",
    "i need",
    "i insist",
    "i expect",
    "we want",
    "we demand",
    "we need",
    "i would like",
    "i'd like",
    "can i get",
    "can i have",
    "can you",
    "can we",
    "could i",
    "could you",
    "refund me",
    "refund us",
    "compensate me",
    "give me",
    "speak to a manager",
    "speak to your manager",
    "let me speak to",
    "file a complaint",
    "make a claim",
    "request a refund",
)

# --- What must stay answerable ------------------------------------------------
#
# A question about how the process works. Wh-words in English, the interrogative
# particles in Chinese, and the nouns that mean "the rules" rather than "my
# case". These win over everything except nothing: a customer asking how
# compensation works is asking for the L1 answer the corpus holds.
_PROCEDURE_CJK = re.compile(
    r"(?:怎么|如何|怎样|什么|为什么|为何|哪些|哪|谁|是否|能否|可否|多久|多少|吗|呢|"
    r"政策|流程|规定|标准|条款|条件)"
)

_PROCEDURE_EN = frozenset({"how", "what", "when", "where", "why", "which", "who", "whose"})

_PROCESS_NOUNS_EN = frozenset(
    {"policy", "policies", "process", "procedure", "terms", "rule", "rules"}
)

_LATIN_WORD = re.compile(r"[a-z']+")


def _has_remedy(text: str, words: set[str], collapsed: str) -> bool:
    if any(term in text for term in _REMEDY_CJK):
        return True
    if words & _REMEDY_EN:
        return True
    # Multi-word English forms are matched against the collapsed text, so
    # "refund me" is not split by tokenisation.
    return any(phrase in collapsed for phrase in ("refund me", "make a claim"))


def _has_agency(text: str, collapsed: str) -> bool:
    if any(term in text for term in _DEMAND_CJK):
        return True
    return any(phrase in collapsed for phrase in _DEMAND_EN)


def _is_procedure_question(text: str, words: set[str]) -> bool:
    if _PROCEDURE_CJK.search(text):
        return True
    if words & _PROCEDURE_EN:
        return True
    return bool(words & _PROCESS_NOUNS_EN)


def is_complaint_claim(question: str) -> bool:
    """True when the customer is claiming a remedy rather than asking about one.

    Three things must hold: a remedy is named, the customer is asking for it
    (agency), and the message is not a question about how it works. Returns
    False for what must not be hijacked - a question that merely mentions a
    remedy, a request that names a remedy without claiming one, and the
    documented L1 policy questions.
    """
    text = question.strip().lower()
    if not text:
        return False

    words = set(_LATIN_WORD.findall(text))
    collapsed = " ".join(_LATIN_WORD.findall(text))

    if not _has_remedy(text, words, collapsed):
        return False
    # The process question veto comes first: it is the documented L1 answer, and
    # a customer who asks "how do I file a complaint" is not filing one.
    if _is_procedure_question(text, words):
        return False
    # Agency is required, not merely a remedy word. Measured: "Please escalate
    # this defect to your engineering team" - the write path's canonical request
    # - names a remedy ("escalate") with no demand aimed at the company, and
    # reading it as a claim took a legitimate `jira.create_issue` request away
    # from the tool that acts on it. A topic is not a claim; the customer has to
    # be asking for something.
    return _has_agency(text, collapsed)
