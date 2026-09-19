"""Deterministic read-tool selection (iteration plan 3.1).

The selector answers ONE question: given what the customer asked and what
they mentioned, which registered read tools are candidates? It deliberately
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
"""

from dataclasses import dataclass

from platform_core.agent_runtime.intent import IntentDetection, Scene


@dataclass(frozen=True)
class ToolCandidate:
    """One plausible read tool, with the score that ordered it."""

    tool_name: str
    score: float
    reason: str


# Which scene makes a tool plausible, and how strongly. Scene evidence is
# weaker than an explicit noun ("where is order 881?" names the tool's
# subject outright), hence the lower weight.
_SCENE_AFFINITY: dict[str, tuple[Scene, ...]] = {
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
_SUBJECT_NOUNS: dict[str, tuple[str, ...]] = {
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

# Tools a caller cannot name a subject for directly (the case number is the
# subject) still need their noun for scene-less routing.
SCENE_WEIGHT = 0.4
SUBJECT_WEIGHT = 1.0


_CJK_RANGES = ((0x3040, 0x30FF), (0x3400, 0x4DBF), (0x4E00, 0x9FFF), (0xAC00, 0xD7AF))


def _is_cjk(text: str) -> bool:
    """True when any char is CJK/kana/hangul — the scripts that whitespace
    tokenisation cannot split."""
    return any(any(lo <= ord(ch) <= hi for lo, hi in _CJK_RANGES) for ch in text)


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
    for tool_name, scenes in _SCENE_AFFINITY.items():
        if available is not None and tool_name not in available:
            continue
        score = 0.0
        reasons: list[str] = []
        if detection.scene in scenes:
            score += SCENE_WEIGHT
            reasons.append(f"scene:{detection.scene.value}")
        noun_hits = [n for n in _SUBJECT_NOUNS[tool_name] if _noun_hit(n)]
        if noun_hits:
            score += SUBJECT_WEIGHT
            reasons.append("subject:" + ",".join(sorted(noun_hits)[:2]))
        if score > 0:
            candidates.append(
                ToolCandidate(tool_name=tool_name, score=score, reason=";".join(reasons))
            )
    candidates.sort(key=lambda c: (-c.score, c.tool_name))
    return candidates
