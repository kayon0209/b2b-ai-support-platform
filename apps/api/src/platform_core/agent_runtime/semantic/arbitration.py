"""Arbitration: the deterministic decision, with the model as an advisor.

The ordering in this file is the whole security argument of the feature, so it
is worth stating plainly. For each input, in this order:

1. **Existing hard rules run first and are not overridable.** An explicit
   human request, a sensitive-data request and a claim dispute are decided by
   `intent.classify` and the existing complaint/emotion paths. A model that
   returns `business_read` for "I want to speak to a person about a refund" does
   not get to talk the router out of a human.
2. **Existing lease state wins.** If a human owns the conversation, no semantic
   task is dispatched. Ownership is not a classification problem.
3. **The model's contribution is additive and bounded.** In `shadow` it changes
   nothing at all. In `assist` it gives an agent candidates to look at. In
   `semantic_read` it may select among *read* tools that survived the tenant
   filter. It never produces an authorization, a final route, or a success.

What this module will not do, deliberately:
- It does not compare `confidence_band` against a threshold to decide anything.
  A model that says `high` on a wrong answer must not be able to act on that.
- It does not let `needs_clarification=False` suppress the rules' own
  clarification path.
- It does not accept a model-introduced tool name, a tenant, an actor, or a
  risk class. Those are server-owned inputs, not suggestions.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from platform_core.agent_runtime.intent import (
    IntentAction,
    IntentDetection,
    IntentKind,
    Route,
)
from platform_core.agent_runtime.semantic.contracts import (
    SCHEMA_VERSION,
    ConfidenceBand,
    ModelSemanticOutput,
    SemanticAssessment,
    SemanticInvalidOutput,
    SemanticMode,
    SemanticTaskKind,
)
from platform_core.agent_runtime.semantic.validator import (
    CapabilityView,
    TurnView,
    ValidationOutcome,
)

# The outcome vocabulary. Kept small and closed so the UI, the metrics and the
# eval report can switch on it without string matching.
DECISION_RULE_ONLY = "rule_only"
DECISION_SHADOW_RECORDED = "shadow_recorded"
DECISION_ASSIST_CANDIDATES = "assist_candidates"
DECISION_SEMANTIC_READ_ALLOWED = "semantic_read_allowed"
DECISION_CLARIFY = "clarify"
DECISION_HANDOFF = "handoff"
DECISION_DEGRADED = "degraded"

# Reason codes. Stable strings: they appear in metrics labels, audit metadata
# and the acceptance report, so renaming one invalidates recorded evidence.
REASON_MODE_OFF = "SEMANTIC_MODE_OFF"
REASON_NO_PROVIDER = "SEMANTIC_NO_PROVIDER"
REASON_INVALID_OUTPUT = "SEMANTIC_INVALID_OUTPUT"
REASON_MODEL_UNAVAILABLE = "SEMANTIC_MODEL_UNAVAILABLE"
REASON_MODEL_TIMEOUT = "SEMANTIC_MODEL_TIMEOUT"
REASON_BUDGET_EXHAUSTED = "SEMANTIC_BUDGET_EXHAUSTED"
REASON_DISAGREE = "SEMANTIC_DISAGREES_WITH_RULES"
REASON_TOOL_NOT_AVAILABLE = "SEMANTIC_TOOL_NOT_AVAILABLE"
REASON_NO_WRITE_CAPABILITY = "SEMANTIC_NO_WRITE_CAPABILITY"
REASON_NO_READ_CAPABILITY = "SEMANTIC_NO_READ_CAPABILITY"
REASON_MISSING_SLOTS = "SEMANTIC_MISSING_SLOTS"
REASON_HUMAN_OWNED = "SEMANTIC_HUMAN_OWNED"
REASON_TRUNCATED = "SEMANTIC_INPUT_TRUNCATED"
REASON_EVIDENCE_EMPTY = "SEMANTIC_EVIDENCE_EMPTY"
REASON_DEPENDENCY_TOO_DEEP = "SEMANTIC_DEPENDENCY_TOO_DEEP"

# Intents that a model may not talk its way out of. The rule classifier owns
# these; the semantic layer records agreement and stops.
NON_OVERRIDABLE_KINDS = frozenset(
    {
        IntentKind.HUMAN_REQUEST,
        IntentKind.SENSITIVE_REQUEST,
        IntentKind.OUT_OF_DOMAIN,
    }
)

# Actions a human owner forbids the AI from taking, regardless of the model.
HANDOFF_ACTIONS = frozenset({IntentAction.HANDOFF})


@dataclass(frozen=True)
class ArbitrationInput:
    """Everything arbitration is allowed to see.

    Note the absence of a `tenant_id` parameter: arbitration does not derive or
    check tenancy, because the caller's session already did, under RLS. Adding
    it here would be an invitation to compare it against something.
    """

    detection: IntentDetection
    mode: SemanticMode
    lease_owner_type: str
    capabilities: dict[str, CapabilityView]
    model_output: ModelSemanticOutput | None
    outcome: ValidationOutcome | None
    assessment_id: str | None = None
    prompt_version: str = ""
    model_name: str = ""
    latency_ms: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    truncated: bool = False
    truncation_reason: str = ""


def arbitrate(inp: ArbitrationInput) -> SemanticAssessment:
    """Produce the server's decision. Pure: no I/O, no model, no clock."""
    det = inp.detection
    reasons: list[str] = []
    assessment_id = inp.assessment_id or str(uuid.uuid4())

    # 1. Mode gate. `off` means no semantic call was made at all, so there is
    #    nothing to arbitrate and the record says so explicitly.
    if inp.mode is SemanticMode.OFF:
        reasons.append(REASON_MODE_OFF)
        return _finalize(
            inp,
            assessment_id=assessment_id,
            decision=DECISION_RULE_ONLY,
            agreement="rules_only",
            reasons=reasons,
            validation_status="not_attempted",
        )

    # 2. Existing human ownership. Recorded as a reason, not an error: this is
    #    the normal state for an assisted conversation.
    if inp.lease_owner_type == "human":
        reasons.append(REASON_HUMAN_OWNED)

    agreement = _agreement(det, inp.model_output)

    # 3. Model failure. Degrade to the rules and say why. A missing model must
    #    never look like a model that agreed.
    if inp.model_output is None:
        code = REASON_INVALID_OUTPUT if inp.outcome is None else REASON_MODEL_UNAVAILABLE
        reasons.append(code)
        decision = (
            DECISION_SHADOW_RECORDED if inp.mode is SemanticMode.SHADOW else DECISION_DEGRADED
        )
        return _finalize(
            inp,
            assessment_id=assessment_id,
            decision=decision,
            agreement=agreement,
            reasons=reasons,
            validation_status="rejected",
        )

    reasons.extend(inp.outcome.reason_codes if inp.outcome else [])

    if agreement == "disagree":
        reasons.append(REASON_DISAGREE)

    if inp.truncated:
        reasons.append(REASON_TRUNCATED)

    # 4. Non-overridable rule outcomes.
    if det.primary_kind in NON_OVERRIDABLE_KINDS or det.action in HANDOFF_ACTIONS:
        decision = DECISION_SHADOW_RECORDED if inp.mode is SemanticMode.SHADOW else DECISION_HANDOFF
        return _finalize(
            inp,
            assessment_id=assessment_id,
            decision=decision,
            agreement=agreement,
            reasons=reasons,
            validation_status="valid" if inp.outcome else "rejected",
        )

    # 5. The rules already want clarification. The model asking for it is
    #    agreement, not news; the model *not* asking does not suppress it.
    if det.action is IntentAction.CLARIFY:
        decision = DECISION_SHADOW_RECORDED if inp.mode is SemanticMode.SHADOW else DECISION_CLARIFY
        return _finalize(
            inp,
            assessment_id=assessment_id,
            decision=decision,
            agreement=agreement,
            reasons=reasons,
            validation_status="valid" if inp.outcome else "rejected",
        )

    # 6. Mode-specific effect.
    if inp.mode is SemanticMode.SHADOW:
        # Shadow may not create business state. The comparison record is the
        # entire product of this branch.
        return _finalize(
            inp,
            assessment_id=assessment_id,
            decision=DECISION_SHADOW_RECORDED,
            agreement=agreement,
            reasons=reasons,
            validation_status="valid" if inp.outcome else "rejected",
        )

    if inp.mode is SemanticMode.ASSIST:
        return _finalize(
            inp,
            assessment_id=assessment_id,
            decision=DECISION_ASSIST_CANDIDATES,
            agreement=agreement,
            reasons=reasons,
            validation_status="valid" if inp.outcome else "rejected",
        )

    # semantic_read. The only mode that can change a routing outcome, and only
    # for reads, only among capabilities the server already granted, and only
    # when the rules did not already have a safe answer.
    if inp.mode is SemanticMode.SEMANTIC_READ and inp.outcome is not None:
        if det.route is Route.BUSINESS_READ and inp.outcome.accepted_tool_names:
            return _finalize(
                inp,
                assessment_id=assessment_id,
                decision=DECISION_SEMANTIC_READ_ALLOWED,
                agreement=agreement,
                reasons=reasons,
                validation_status="valid",
            )
        reasons.append(REASON_NO_READ_CAPABILITY)
        return _finalize(
            inp,
            assessment_id=assessment_id,
            decision=DECISION_RULE_ONLY,
            agreement=agreement,
            reasons=reasons,
            validation_status="valid",
        )

    return _finalize(
        inp,
        assessment_id=assessment_id,
        decision=DECISION_RULE_ONLY,
        agreement=agreement,
        reasons=reasons,
        validation_status="valid" if inp.outcome else "rejected",
    )


def _agreement(det: IntentDetection, out: ModelSemanticOutput | None) -> str:
    """Compare model and rules on the primary intent only.

    Scene and business line are excluded on purpose: they are retrieval
    parameters, and calling a scene mismatch a "disagreement" would report
    noise as a model failure on every technical question.
    """
    if out is None:
        return "rules_only"
    if det.primary_kind is out.primary_intent:
        return "agree"
    return "disagree"


def _finalize(
    inp: ArbitrationInput,
    *,
    assessment_id: str,
    decision: str,
    agreement: str,
    reasons: list[str],
    validation_status: str,
) -> SemanticAssessment:
    det = inp.detection
    # Deduplicate, keep first-seen order: the metrics label and the audit row
    # must be stable for the same inputs, and a set would reorder them.
    seen: set[str] = set()
    ordered: list[str] = []
    for reason in reasons:
        if reason not in seen:
            seen.add(reason)
            ordered.append(reason)
    return SemanticAssessment(
        schema_version=SCHEMA_VERSION,
        assessment_id=assessment_id,
        rule_scene=det.scene,
        rule_primary_intent=det.primary_kind,
        rule_secondary_intents=det.secondary_kinds,
        rule_route=det.route.value,
        rule_action=det.action.value,
        model_output=inp.model_output,
        agreement=agreement,  # type: ignore[arg-type]
        mode=inp.mode,
        effective_decision=decision,
        reason_codes=tuple(ordered),
        validation_status=validation_status,  # type: ignore[arg-type]
        truncated=inp.truncated,
        truncation_reason=inp.truncation_reason,
        prompt_version=inp.prompt_version,
        model_name=inp.model_name,
        latency_ms=inp.latency_ms,
        prompt_tokens=inp.prompt_tokens,
        completion_tokens=inp.completion_tokens,
    )


def write_tasks_need_human(outcome: ValidationOutcome | None) -> list[tuple[SemanticTaskKind, str]]:
    """Write tasks that must go to a human, with the reason.

    Exposed so the task layer and the UI agree on which tasks are blocked and
    why, instead of each deriving it from a different field.
    """
    if outcome is None:
        return []
    return [
        (intent.task_kind, reason)
        for intent, reason in outcome.unsupported_intents
        if intent.task_kind is SemanticTaskKind.WRITE
    ]


__all__ = [
    "DECISION_ASSIST_CANDIDATES",
    "DECISION_CLARIFY",
    "DECISION_DEGRADED",
    "DECISION_HANDOFF",
    "DECISION_RULE_ONLY",
    "DECISION_SEMANTIC_READ_ALLOWED",
    "DECISION_SHADOW_RECORDED",
    "REASON_BUDGET_EXHAUSTED",
    "REASON_DEPENDENCY_TOO_DEEP",
    "REASON_DISAGREE",
    "REASON_EVIDENCE_EMPTY",
    "REASON_HUMAN_OWNED",
    "REASON_INVALID_OUTPUT",
    "REASON_MISSING_SLOTS",
    "REASON_MODEL_TIMEOUT",
    "REASON_MODEL_UNAVAILABLE",
    "REASON_MODE_OFF",
    "REASON_NO_PROVIDER",
    "REASON_NO_READ_CAPABILITY",
    "REASON_NO_WRITE_CAPABILITY",
    "REASON_TOOL_NOT_AVAILABLE",
    "REASON_TRUNCATED",
    "ArbitrationInput",
    "arbitrate",
    "write_tasks_need_human",
]


# Referenced so the enum import is not dropped by a linter; the band is part
# of the public contract even though arbitration never branches on it.
_BANDS = tuple(ConfidenceBand)
_ = (SemanticInvalidOutput, TurnView)
