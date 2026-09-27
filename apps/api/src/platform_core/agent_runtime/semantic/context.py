"""Server-built, redacted model input.

Everything the model sees is assembled here, on the server, from data the
server already holds. The model never receives: a tenant id, an actor id, a
credential, a channel webhook secret, a raw connector payload, a message
attachment, or a chain-of-thought channel from a previous call.

Two rules drive the truncation policy:

1. **The current turn is never dropped.** A semantic assessment without the
   customer's actual current words is worse than no assessment.
2. **Truncation is recorded, not silent.** `truncation_reason` travels with the
   assessment, so a degraded classification is distinguishable from a complete
   one in the evaluation report instead of quietly scoring as a miss.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from platform_core.agent_runtime.intent import BusinessLine, Scene
from platform_core.agent_runtime.semantic.contracts import (
    INPUT_TOKEN_BUDGET,
    MAX_HISTORY_TURNS,
    SemanticMode,
)
from platform_core.agent_runtime.semantic.validator import CapabilityView, TurnView

# Rough characters-per-token for mixed CJK/latin customer text. Deliberately
# conservative (low), because under-estimating the token count truncates less
# than the budget allows - the failure mode is a slightly smaller prompt, not a
# request the provider rejects for exceeding its context.
CHARS_PER_TOKEN = 2

REASON_HISTORY_DROPPED = "history_dropped_for_budget"
REASON_ATTACHMENTS_DROPPED = "attachments_omitted"
REASON_HISTORY_WINDOW = "history_window_trimmed"

SYSTEM_PROMPT_VERSION = "semantic-v7"

CANONICAL_SLOT_NAMES: tuple[tuple[str, str], ...] = (
    ("order_id", "sales order identifier"),
    ("shipment_id", "shipment or carrier tracking identifier"),
    ("invoice_id", "invoice identifier"),
    ("case_ref", "support case identifier"),
    ("part_number", "component or part identifier"),
    ("product_model", "product or equipment model identifier"),
    ("quantity", "requested item count"),
    ("destination_city", "customer-stated destination city"),
    ("full_address", "complete street or delivery address"),
    ("invoice_type", "invoice category or format"),
    ("annual_volume", "annual demand quantity for a quotation"),
)


@dataclass
class SemanticContext:
    """The assembled, redacted input plus what had to be left out."""

    current_turn: TurnView
    history: list[TurnView] = field(default_factory=list)
    verified_facts: list[dict[str, Any]] = field(default_factory=list)
    task_state: list[dict[str, Any]] = field(default_factory=list)
    capabilities: dict[str, CapabilityView] = field(default_factory=dict)
    mode: SemanticMode = SemanticMode.OFF
    truncated: bool = False
    truncation_reason: str = ""
    prompt_version: str = SYSTEM_PROMPT_VERSION

    def turns(self) -> list[TurnView]:
        """Every turn actually sent, current first. Used for evidence checks."""
        return [self.current_turn, *self.history]

    def estimated_tokens(self) -> int:
        total = sum(len(t.text) for t in self.turns())
        return total // CHARS_PER_TOKEN


def build_context(
    *,
    current_turn_id: str,
    current_text: str,
    history: list[tuple[str, str]],
    mode: SemanticMode,
    capabilities: dict[str, CapabilityView],
    verified_facts: list[dict[str, Any]] | None = None,
    task_state: list[dict[str, Any]] | None = None,
    max_history_turns: int = MAX_HISTORY_TURNS,
    token_budget: int = INPUT_TOKEN_BUDGET,
) -> SemanticContext:
    """Assemble the redacted input, newest-first, under a token budget.

    `history` is expected oldest-first; the result keeps that order so the model
    reads the conversation forwards. Only the *authorized* window reaches this
    function - the caller has already applied the tenant and identity filters,
    and nothing here widens it.
    """
    current = TurnView(turn_id=current_turn_id, text=current_text)
    ctx = SemanticContext(
        current_turn=current,
        mode=mode,
        capabilities=capabilities,
        verified_facts=list(verified_facts or []),
        task_state=list(task_state or []),
    )

    window = list(history)[-max_history_turns:]
    if len(history) > len(window):
        ctx.truncated = True
        ctx.truncation_reason = REASON_HISTORY_WINDOW

    used = len(current_text) // CHARS_PER_TOKEN
    for turn_id, text in window:
        cost = len(text) // CHARS_PER_TOKEN + 4  # role/framing overhead
        if used + cost > token_budget:
            ctx.truncated = True
            # The current turn wins budget contention; history is what gives.
            ctx.truncation_reason = REASON_HISTORY_DROPPED
            break
        used += cost
        ctx.history.append(TurnView(turn_id=turn_id, text=text))

    return ctx


def render_prompt(ctx: SemanticContext, *, condition_fields: Iterable[str] = ()) -> str:
    """The user-role payload.

    Structure is explicit and labelled because the model is being asked to
    separate what the customer said from what the platform already verified -
    an undifferentiated blob would let the two blend, and a "verified" fact
    that was actually an inference is the kind of error that reaches a
    customer.
    """
    lines: list[str] = []
    lines.append(f"MODE: {ctx.mode.value}")
    lines.append(f"SCHEMA_VERSION: {ctx.prompt_version}")
    lines.append("")

    lines.append("## CURRENT_TURN")
    lines.append(f"turn_id: {ctx.current_turn.turn_id}")
    lines.append(f"char_length: {len(ctx.current_turn.text)}")
    lines.append(ctx.current_turn.text)
    lines.append("")

    if ctx.history:
        lines.append("## AUTHORIZED_HISTORY")
        for turn in ctx.history:
            lines.append(f"- [{turn.turn_id}] char_length={len(turn.text)} {turn.text}")
        lines.append("")

    if ctx.verified_facts:
        lines.append("## VERIFIED_FACTS")
        lines.append("These came back from an authorized business read. They are facts.")
        for fact in ctx.verified_facts:
            lines.append(
                f"- {fact.get('ref', '?')}: {fact.get('label', '?')}={fact.get('value', '?')}"
            )
        lines.append("")

    if ctx.task_state:
        lines.append("## TASK_STATE")
        for task in ctx.task_state:
            lines.append(
                f"- {task.get('local_key', '?')}: {task.get('status', '?')}"
                f" missing={','.join(task.get('missing_slots', [])) or '-'}"
            )
        lines.append("")

    lines.append("## SCENE_AND_PRODUCT_LINE_REFERENCE")
    lines.append(
        "scene describes the business domain, not the requested action. Use exactly one of: "
        + ", ".join(scene.value for scene in Scene)
        + ". Use unspecified when no domain fits."
    )
    lines.append(
        "business_line describes the product family. Use exactly one of: "
        + ", ".join(line.value for line in BusinessLine)
        + ". PCB board fabrication is pcb, assembly is smt, electronic parts are component, "
        "design-for-manufacturing review is dfm; otherwise use unspecified."
    )
    lines.append("")

    lines.append("## CANONICAL_SLOT_NAMES")
    lines.append(
        "For human follow-up fields without a matching offered tool parameter, "
        "reuse these exact names:"
    )
    lines.extend(f"- {name}: {description}" for name, description in CANONICAL_SLOT_NAMES)
    lines.append("")

    if ctx.capabilities:
        lines.append("## AVAILABLE_CAPABILITIES")
        lines.append("Only these tool names may appear in tool_candidates.")
        for name, cap in sorted(ctx.capabilities.items()):
            parameters = ",".join(cap.parameter_names) or "-"
            required = ",".join(cap.required_parameters) or "-"
            lines.append(
                f"- {name} (risk={cap.risk_class}; parameters={parameters}; required={required})"
            )
    else:
        lines.append("## AVAILABLE_CAPABILITIES")
        lines.append("No tools are available in this request; tool_candidates must be [].")
    lines.append("")

    registered_fields = sorted(condition_fields)
    lines.append("## REGISTERED_CONDITION_FIELDS")
    if registered_fields:
        lines.append("A condition may use only one field from this list:")
        lines.extend(f"- {name}" for name in registered_fields)
    else:
        lines.append("No condition fields are available. Set every intent condition to null.")
    lines.append("")

    lines.append("## RULES")
    lines.append("- Return one JSON object matching the requested schema. No prose.")
    lines.append("- Every intent needs source_turn_id from the turns above.")
    lines.append(
        "- Evidence offsets count Unicode code points in the exact turn body, "
        "start at zero, and end is exclusive."
    )
    lines.append("- Every span must satisfy 0 <= start < end <= that turn's char_length.")
    lines.append("- Do not count turn labels, metadata, UTF-8 bytes, or tokens as offsets.")
    lines.append(
        "- Example: the body `Order A1` has char_length 8; citing it all is start 0, end 8."
    )
    lines.append("- If exact offsets are uncertain, use empty evidence arrays; never estimate.")
    lines.append("- Every non-null condition.field must exactly match REGISTERED_CONDITION_FIELDS.")
    lines.append(
        "- Do not invent slot-name aliases; use tool parameter names or CANONICAL_SLOT_NAMES."
    )
    lines.append(
        "- depends_on contains zero-based positions in intents[], encoded as strings "
        '(for example, ["0"]).'
    )
    lines.append(
        "- Use depends_on=[] when there is no dependency; never use task names as references."
    )
    lines.append("- Do not invent tools, values or identifiers. Leave a slot in")
    lines.append("  missing_slots instead of guessing it.")
    return "\n".join(lines)


def redact_for_log(ctx: SemanticContext) -> dict[str, Any]:
    """A log-safe projection of the context.

    Counts and hashes only. No customer text, no slot value, no turn body -
    this is what a debug line is allowed to contain (AGENTS.md rule 10).
    """
    import hashlib

    digest = hashlib.sha256(ctx.current_turn.text.encode()).hexdigest()[:16]
    return {
        "mode": ctx.mode.value,
        "turn_count": 1 + len(ctx.history),
        "current_turn_hash": digest,
        "fact_count": len(ctx.verified_facts),
        "task_count": len(ctx.task_state),
        "capability_count": len(ctx.capabilities),
        "estimated_tokens": ctx.estimated_tokens(),
        "truncated": ctx.truncated,
        "truncation_reason": ctx.truncation_reason,
    }


__all__ = [
    "CHARS_PER_TOKEN",
    "REASON_ATTACHMENTS_DROPPED",
    "REASON_HISTORY_DROPPED",
    "REASON_HISTORY_WINDOW",
    "SYSTEM_PROMPT_VERSION",
    "CANONICAL_SLOT_NAMES",
    "SemanticContext",
    "build_context",
    "redact_for_log",
    "render_prompt",
]
