"""Deterministic tool selection (iteration plan 3.1, 3.5).

The selector answers ONE question: given what the customer asked and what
they mentioned, which registered tools are candidates? It deliberately
answers nothing else:

- **Authorization stays with the policy engine.** A candidate the actor has
  no permission for is still proposed and still rejected by the gateway's
  own re-check - the selector's job ends at "plausible".
- **No LLM.** Tool selection decides which external system is queried; a
  non-deterministic chooser there would make every downstream audit trail
  rest on a coin flip. Same input, same candidates, every time.
- **No candidates is an answer.** A question that names no tool the tenant
  can serve hands off; it never degrades into a knowledge search over live
  data the corpus does not contain (plan 3.3).

Reads and writes share the scoring rule and nothing else. The rule is shared
because two copies of "scene evidence is weaker than an explicit noun" would
drift; what a caller may *do* with the result differs, and that difference
lives at the call site, not here. A read candidate may be executed. A write
candidate may only be proposed - the gateway's risk class and a second
party's confirmation decide whether it ever runs.
"""

from dataclasses import dataclass

from platform_core.agent_runtime.intent import IntentDetection, Scene


@dataclass(frozen=True)
class ToolCandidate:
    """One plausible tool, with the score that ordered it."""

    tool_name: str
    score: float
    reason: str


# Which scene makes a tool plausible, and how strongly. Scene evidence is
# weaker than an explicit noun ("where is order 881?" names the tool's
# subject outright), hence the lower weight.
_READ_SCENE_AFFINITY: dict[str, tuple[Scene, ...]] = {
    "order.get_status": (Scene.ORDER_FULFILMENT,),
    "shipment.track": (Scene.ORDER_FULFILMENT,),
    "billing.get_invoice": (Scene.BILLING, Scene.ORDER_FULFILMENT),
    "case.read": (Scene.COMPLAINT, Scene.TECHNICAL_SUPPORT, Scene.ACCOUNT_SECURITY),
    # Stock/lead-time questions sit in fulfilment and pre-sales alike: a
    # customer deciding what to buy asks "do you have X in stock".
    "inventory.check_stock": (Scene.ORDER_FULFILMENT, Scene.PRE_SALES),
}

# Nouns that name a tool's subject explicitly, per tool. Matched as whole
# lowercase tokens against the question.
_READ_SUBJECT_NOUNS: dict[str, tuple[str, ...]] = {
    "order.get_status": ("order", "purchase"),
    "shipment.track": ("shipment", "delivery", "parcel", "package", "tracking", "shipped"),
    "billing.get_invoice": ("invoice", "receipt", "credit note", "billing"),
    "case.read": ("case", "ticket"),
    # CJK nouns are matched as substrings of the lowered question (see
    # _noun_hit): whitespace tokenisation cannot split them.
    "inventory.check_stock": (
        "stock",
        "inventory",
        "lead time",
        "part",
        "component",
        "有货",
        "库存",
        "货期",
        "现货",
    ),
}

# Write tools (plan 3.5). Same shape, different consequence: selecting one
# of these only ever produces a proposal.
#
# `crm.update_account` is deliberately ABSENT, and the reason is not that a
# customer never asks for one - "please correct the delivery address on our
# account" is an ordinary after-sales request. It is that the tool takes a
# free-form `fields` patch, and deriving a field patch from an utterance needs
# a model. The platform's rule is that deterministic code decides what gets
# written to an external system, so the tool is proposable by a human through
# `POST /v1/tool-proposals` and is not offered to the agent. Listing it here
# would mean picking it, failing to build arguments, and handing off every
# time - a candidate that can never win, which is noise in the selection audit
# and indistinguishable from not shipping the tool at all.
_WRITE_SCENE_AFFINITY: dict[str, tuple[Scene, ...]] = {
    "jira.create_issue": (Scene.TECHNICAL_SUPPORT, Scene.COMPLAINT, Scene.AFTER_SALES),
    "linear.create_issue": (Scene.TECHNICAL_SUPPORT, Scene.COMPLAINT, Scene.AFTER_SALES),
    "im.send_notification": (Scene.COMPLAINT, Scene.ORDER_FULFILMENT),
}

_WRITE_SUBJECT_NOUNS: dict[str, tuple[str, ...]] = {
    "jira.create_issue": ("bug", "defect", "jira", "故障", "缺陷", "报障"),
    "linear.create_issue": ("bug", "defect", "linear", "故障", "缺陷", "报障"),
    "im.send_notification": ("notify", "notification", "alert", "escalate", "通知", "告警", "升级"),
}

SCENE_WEIGHT = 0.4
SUBJECT_WEIGHT = 1.0


_CJK_RANGES = ((0x3040, 0x30FF), (0x3400, 0x4DBF), (0x4E00, 0x9FFF), (0xAC00, 0xD7AF))


def _is_cjk(text: str) -> bool:
    """True when any char is CJK/kana/hangul — the scripts that whitespace
    tokenisation cannot split."""
    return any(any(lo <= ord(ch) <= hi for lo, hi in _CJK_RANGES) for ch in text)


def _rank(
    affinity: dict[str, tuple[Scene, ...]],
    nouns: dict[str, tuple[str, ...]],
    detection: IntentDetection,
    question: str,
    available: set[str] | None,
) -> list[ToolCandidate]:
    """Score and order the tools in one vocabulary.

    Shared by the read and write selectors so the weights, the CJK handling
    and the tie-break cannot drift apart between them.
    """
    lowered = question.lower()
    tokens = lowered.replace("-", " ").split()

    def _noun_hit(noun: str) -> bool:
        # CJK nouns carry no whitespace boundaries, so substring match; latin
        # nouns match whole tokens only ("part" must not fire inside
        # "particular").
        if _is_cjk(noun):
            return noun in lowered
        return noun in tokens

    candidates: list[ToolCandidate] = []
    for tool_name, scenes in affinity.items():
        if available is not None and tool_name not in available:
            continue
        score = 0.0
        reasons: list[str] = []
        if detection.scene in scenes:
            score += SCENE_WEIGHT
            reasons.append(f"scene:{detection.scene.value}")
        noun_hits = [n for n in nouns[tool_name] if _noun_hit(n)]
        if noun_hits:
            score += SUBJECT_WEIGHT
            reasons.append("subject:" + ",".join(sorted(noun_hits)[:2]))
        if score > 0:
            candidates.append(
                ToolCandidate(tool_name=tool_name, score=score, reason=";".join(reasons))
            )
    candidates.sort(key=lambda c: (-c.score, c.tool_name))
    return candidates


def select_read_tools(
    detection: IntentDetection, question: str, available: set[str] | None = None
) -> list[ToolCandidate]:
    """Rank plausible read tools for one utterance.

    `available` narrows to tools the tenant actually has connectors for;
    candidates that do not survive that filter are dropped, not downgraded -
    an unreachable tool is not a candidate (plan 3.1's "未连接的能力直接出局").
    Returns the scored, ordered remainder; empty means "no candidate".
    """
    # A knowledge question that happens to mention a noun ("is the invoice
    # VAT included?") is answered from the corpus; tools are for live-data
    # questions only (plan 3.3). The kind axis is the gate.
    if detection.route.value != "business_read":
        return []
    return _rank(_READ_SCENE_AFFINITY, _READ_SUBJECT_NOUNS, detection, question, available)


def select_write_tools(
    detection: IntentDetection, question: str, available: set[str] | None = None
) -> list[ToolCandidate]:
    """Rank plausible WRITE tools for one utterance.

    Identical contract to `select_read_tools` - deterministic, no LLM, empty
    means "no candidate" - with one difference in what the result authorises.
    A read candidate is executed and its receipt becomes evidence. A write
    candidate is only ever *proposed*: the gateway decides from the tool's
    risk class whether that proposal needs a second party's confirmation
    before it can run, and nothing here can skip that.

    **Selection is a function of the utterance alone, and that is deliberate.**
    A tool whose applicability depends on conversation state cannot be offered
    here: this function cannot see that state, and the one flow that needed it
    (the EQ confirmation) does not go through the write path at all - the agent
    relays and collects, and `case.eq_confirm` is `human_approval`, which the
    policy engine keeps unreachable by the agent at every stage including
    propose. An earlier version of this function took a `case_ref` to offer it
    anyway; the orchestrator now hands off instead.
    """
    # The route is the gate for the same reason it is on the read side: a
    # request phrased as a question ("can you update our address?") is not an
    # instruction to act, and treating it as one would propose a write the
    # customer never asked for.
    if detection.route.value != "business_write":
        return []
    return _rank(_WRITE_SCENE_AFFINITY, _WRITE_SUBJECT_NOUNS, detection, question, available)
