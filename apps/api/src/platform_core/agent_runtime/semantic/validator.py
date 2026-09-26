"""Model-output validation: the only door a model's suggestion comes through.

Four independent classes of check, because they defend against different
things and Pydantic alone only covers the first:

1. **Schema** (Pydantic, `extra="forbid"`): unknown fields, wrong types,
   oversized arrays, invalid enums, over-long scalars.
2. **Evidence**: every span must point at a turn that was actually sent, and
   the offsets must be inside that turn's text. A model that cites a turn it
   was never shown, or an offset past the end, is refused - the offsets are
   the model's only claim about *where* in the customer's words it found
   something, and an unchecked one would let a fabricated span be displayed to
   an agent as a quotation.
3. **Capability**: `tool_candidates[].tool_name` must be in the capability set
   the server offered. Not "in the platform catalog" - in *this* set, which was
   already narrowed by tenant connectors and the actor's role. The ordering
   matters and is not interchangeable: capabilities are filtered first, then
   the model's suggestions are accepted against what survived.
4. **Conditions**: only registered fields, and a cycle/depth check over
   `depends_on`.

Failure is always a `SemanticInvalidOutput` carrying a stable code, never a
partial accept. A model that gets `intents[0..2]` right and `intents[3]` wrong
is not 75% usable; it is wrong about what the customer asked.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from platform_core.agent_runtime.semantic.contracts import (
    MAX_CONDITION_DEPTH,
    MAX_INTENTS,
    MAX_SLOT_NODES,
    SCHEMA_VERSION,
    EvidenceSpan,
    ModelSemanticOutput,
    SemanticCondition,
    SemanticIntent,
    SemanticInvalidOutput,
)

# A whole-JSON-object extractor, not a greedy `.*`.
#
# A model that answers with prose around the JSON ("Here is the analysis:
# {...}") is common enough that a bare `json.loads` would reject it, and a
# greedy regex would happily match from the first `{` to the last `}` - which
# on a prompt-injected message means parsing the attacker's trailing object as
# the model's answer. This matches balanced braces while respecting strings and
# escapes, so the extracted text is the first complete JSON object and nothing
# after it can join it.
_BRACE_RE = re.compile(r"\{")
_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


def extract_json_object(raw: str) -> str:
    """Pull the first balanced JSON object out of a model response.

    Brace counting is string-aware, so a `{` inside a quoted value does not
    open a nesting level and an escaped quote does not end a string.
    """
    text = raw.strip()
    fenced = _FENCE_RE.search(text)
    if fenced:
        text = fenced.group(1).strip()

    start = _BRACE_RE.search(text)
    if start is None:
        raise SemanticInvalidOutput("SEMANTIC_INVALID_OUTPUT", "no JSON object in model output")

    depth = 0
    in_string = False
    escaped = False
    for index in range(start.start(), len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start.start() : index + 1]
    raise SemanticInvalidOutput("SEMANTIC_INVALID_OUTPUT", "unterminated JSON object")


def parse_model_output(raw: str) -> ModelSemanticOutput:
    """Parse and schema-validate a raw model response.

    Schema only. Evidence, capability and condition checks happen in
    `validate_semantics`, because they need the server's context and a partial
    parse is not a usable object.
    """
    payload = extract_json_object(raw)
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise SemanticInvalidOutput("SEMANTIC_INVALID_OUTPUT", f"invalid JSON: {exc.msg}") from exc
    if not isinstance(data, dict):
        raise SemanticInvalidOutput("SEMANTIC_INVALID_OUTPUT", "top level must be an object")
    try:
        return ModelSemanticOutput.model_validate(data)
    except ValidationError as exc:
        # The message names fields and limits, never values: a rejected
        # payload may contain a customer's address, and the reason string
        # ends up in logs and reason codes.
        errors = exc.errors()
        first: dict[str, Any] = dict(errors[0]) if errors else {}
        where = ".".join(str(p) for p in first.get("loc", ())) or "<root>"
        msg = str(first.get("msg", "schema violation"))
        raise SemanticInvalidOutput("SEMANTIC_INVALID_OUTPUT", f"{where}: {msg}") from exc


@dataclass(frozen=True)
class TurnView:
    """The redacted turn text that was actually sent to the model.

    Only what validation needs: an id and the length of the text the model saw.
    """

    turn_id: str
    text: str


@dataclass(frozen=True)
class CapabilityView:
    """One tool the server offered, already filtered by tenant and role.

    `risk_class` is the registry's, never the model's.
    """

    tool_name: str
    risk_class: str
    allowed_task_kinds: frozenset[str] = frozenset()


@dataclass
class ValidationOutcome:
    """Result of the context-dependent checks."""

    output: ModelSemanticOutput | None
    reason_codes: list[str] = field(default_factory=list)
    # Tools that survived both the server's filter and the model's proposal.
    accepted_tool_names: list[str] = field(default_factory=list)
    # Intents whose `task_kind` the accepted tools cannot serve. Kept, with a
    # reason, because "this needs a human" is an answer the agent must see -
    # silently dropping the address-change request would lose the one thing
    # the customer most needs followed up.
    unsupported_intents: list[tuple[SemanticIntent, str]] = field(default_factory=list)


def validate_semantics(
    output: ModelSemanticOutput,
    *,
    turns: list[TurnView],
    capabilities: dict[str, CapabilityView],
    condition_fields: frozenset[str],
) -> ValidationOutcome:
    """Apply the checks Pydantic cannot: evidence, capability, conditions."""
    reason_codes: list[str] = []
    turn_by_id = {t.turn_id: t for t in turns}

    _check_evidence(output, turn_by_id, reason_codes)
    _check_conditions(output, condition_fields, reason_codes)

    if len(output.intents) > MAX_INTENTS:
        raise SemanticInvalidOutput("SEMANTIC_INVALID_OUTPUT", "too many intents")
    if output.slot_nodes() > MAX_SLOT_NODES:
        raise SemanticInvalidOutput("SEMANTIC_INVALID_OUTPUT", "too many slots")

    accepted: list[str] = []
    for candidate in output.tool_candidates:
        cap = capabilities.get(candidate.tool_name)
        if cap is None:
            # Not in the set we offered. Recorded, not raised: one bad tool
            # name does not invalidate a correct intent breakdown, and the
            # arbitration layer decides what a rejected candidate means.
            reason_codes.append("SEMANTIC_TOOL_NOT_AVAILABLE")
            continue
        accepted.append(cap.tool_name)

    unsupported = _unsupported_intents(output, capabilities, accepted)

    return ValidationOutcome(
        output=output,
        reason_codes=reason_codes,
        accepted_tool_names=accepted,
        unsupported_intents=unsupported,
    )


def _check_evidence(
    output: ModelSemanticOutput,
    turn_by_id: dict[str, TurnView],
    reason_codes: list[str],
) -> None:
    """Every cited span must land inside a turn that was actually sent."""
    spans: list[EvidenceSpan] = list(output.evidence_spans)
    for intent in output.intents:
        spans.extend(intent.evidence)

    for span in spans:
        turn = turn_by_id.get(span.turn_id)
        if turn is None:
            raise SemanticInvalidOutput(
                "SEMANTIC_INVALID_OUTPUT", "evidence references an unsent turn"
            )
        if span.end > len(turn.text):
            raise SemanticInvalidOutput(
                "SEMANTIC_INVALID_OUTPUT", "evidence range exceeds the turn"
            )
        if not turn.text[span.start : span.end].strip():
            # In-bounds but blank: a citation pointing at whitespace is a
            # citation pointing at nothing, and displaying it as a quotation
            # would be a fabricated quote with a real-looking position.
            reason_codes.append("SEMANTIC_EVIDENCE_EMPTY")


def _check_conditions(
    output: ModelSemanticOutput,
    condition_fields: frozenset[str],
    reason_codes: list[str],
) -> None:
    """Conditions may only reference registered fields, and may not cycle."""
    for intent in output.intents:
        condition = intent.condition
        if condition is not None and condition.field not in condition_fields:
            raise SemanticInvalidOutput(
                "SEMANTIC_INVALID_OUTPUT", "condition references an unregistered field"
            )
        if condition is not None and condition.operator.value == "in":
            if not isinstance(condition.value, list) or not condition.value:
                raise SemanticInvalidOutput(
                    "SEMANTIC_INVALID_OUTPUT", "`in` needs a non-empty list"
                )

    _check_dependency_graph(output.intents, reason_codes)


def _check_dependency_graph(intents: list[SemanticIntent], reason_codes: list[str]) -> None:
    """Depth limit, unknown dependency, and cycle detection.

    `depends_on` names a sibling by its zero-based position in the model's
    `intents` array. The spec journey puts every need in one turn, so
    `source_turn_id` is the same string for all of them and cannot key a
    dependency; the position is the only identifier stable within one plan. A
    non-numeric or out-of-range entry is refused rather than resolved, because
    attaching a write to the wrong read is worse than leaving it unconnected.
    """
    count = len(intents)
    edges: dict[int, list[int]] = {}
    for index, intent in enumerate(intents):
        ordinals: list[int] = []
        for raw in intent.depends_on:
            try:
                ordinal = int(str(raw).strip())
            except (TypeError, ValueError):
                raise SemanticInvalidOutput(
                    "SEMANTIC_INVALID_OUTPUT", "depends_on must reference an intent position"
                ) from None
            if not 0 <= ordinal < count:
                raise SemanticInvalidOutput(
                    "SEMANTIC_INVALID_OUTPUT", "depends_on references an unknown intent"
                )
            ordinals.append(ordinal)
        edges[index] = ordinals

    # Cycle detection and depth limiting are separate questions and are checked
    # separately. A two-node cycle (a -> b -> a) has depth 2, which is under
    # `MAX_CONDITION_DEPTH`, so a depth-only check walks it, exits on the
    # repeated node and reports nothing - a cyclic task graph would then reach
    # the scheduler and never become ready. Revisiting a node already on the
    # current path is the cycle signal; it is checked first, on every edge
    # rather than only the first, because a model can hang a cycle off a later
    # dependency.
    for start in edges:
        path: list[int] = []
        on_path: set[int] = set()
        node = start
        while node in edges:
            if node in on_path:
                raise SemanticInvalidOutput("SEMANTIC_INVALID_OUTPUT", "dependency cycle")
            on_path.add(node)
            path.append(node)
            if len(path) > MAX_CONDITION_DEPTH:
                reason_codes.append("SEMANTIC_DEPENDENCY_TOO_DEEP")
                break
            deps = edges[node]
            node = deps[0] if deps else -1


def _unsupported_intents(
    output: ModelSemanticOutput,
    capabilities: dict[str, CapabilityView],
    accepted_tools: list[str],
) -> list[tuple[SemanticIntent, str]]:
    """Intents no accepted tool can serve, each with the reason to show.

    This is where "改地址" and "补发票" end up. The platform has no write tool
    for either (`selector.py` excludes `crm.update_account` precisely because a
    free-form field patch cannot be derived deterministically), so the honest
    outcome is a task marked needs-human with the blocking reason attached - not
    a proposal for a tool that does not exist.
    """
    from platform_core.agent_runtime.semantic.contracts import SemanticTaskKind

    unsupported: list[tuple[SemanticIntent, str]] = []
    write_capable = [
        name
        for name, cap in capabilities.items()
        if cap.risk_class in ("confirmed_write", "human_approval")
        and SemanticTaskKind.WRITE.value in cap.allowed_task_kinds
    ]

    for intent in output.intents:
        if intent.task_kind is SemanticTaskKind.CLARIFY:
            continue
        if intent.task_kind is SemanticTaskKind.WRITE and not write_capable:
            unsupported.append((intent, "SEMANTIC_NO_WRITE_CAPABILITY"))
        elif intent.task_kind is SemanticTaskKind.READ and not accepted_tools:
            unsupported.append((intent, "SEMANTIC_NO_READ_CAPABILITY"))
        elif intent.missing_slots:
            unsupported.append((intent, "SEMANTIC_MISSING_SLOTS"))
    return unsupported


def strip_code_fence(raw: str) -> str:
    """Remove a markdown fence if present. Exposed for the prompt builder."""
    fenced = _FENCE_RE.search(raw.strip())
    return fenced.group(1).strip() if fenced else raw.strip()


def schema_version() -> str:
    return SCHEMA_VERSION


def describe_for_prompt() -> dict[str, Any]:
    """The schema description handed to the model.

    Derived from the same Pydantic models that validate the response, so the
    instructions and the enforcement cannot drift: a field added to
    `ModelSemanticOutput` is described here without a second edit.
    """
    schema = ModelSemanticOutput.model_json_schema()
    return {
        "schema_version": SCHEMA_VERSION,
        "json_schema": schema,
        "max_intents": MAX_INTENTS,
    }


__all__ = [
    "CapabilityView",
    "TurnView",
    "ValidationOutcome",
    "describe_for_prompt",
    "extract_json_object",
    "parse_model_output",
    "schema_version",
    "strip_code_fence",
    "validate_semantics",
]


# Re-exported for callers that catch a single error type.
_ = SemanticCondition
