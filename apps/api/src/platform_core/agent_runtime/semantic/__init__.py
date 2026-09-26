"""Semantic understanding: the model's suggestion, the server's decision.

Import surface. The internal split (contracts / validator / arbitration /
context / service) exists so each piece is testable on its own; callers should
come through here.

ADR 0007's restriction on generic multi-agent dispatch still holds, and this
package is not an exception to it: there is one assessment per turn, produced
by one bounded call, arbitrated deterministically. Nothing here routes by
itself.
"""

from platform_core.agent_runtime.semantic.arbitration import (
    ArbitrationInput,
    arbitrate,
)
from platform_core.agent_runtime.semantic.context import (
    SemanticContext,
    build_context,
    redact_for_log,
    render_prompt,
)
from platform_core.agent_runtime.semantic.contracts import (
    SCHEMA_VERSION,
    ConfidenceBand,
    ModelSemanticOutput,
    SemanticAssessment,
    SemanticCondition,
    SemanticIntent,
    SemanticInvalidOutput,
    SemanticMode,
    SemanticSlot,
    SemanticTaskKind,
    SemanticToolCandidate,
    SlotOrigin,
)
from platform_core.agent_runtime.semantic.modes import (
    ModeResolution,
    resolve_mode,
    weakest_mode,
)
from platform_core.agent_runtime.semantic.service import (
    SemanticBudget,
    SemanticUnderstandingService,
    analyze,
)
from platform_core.agent_runtime.semantic.validator import (
    CapabilityView,
    TurnView,
    extract_json_object,
    parse_model_output,
    validate_semantics,
)

__all__ = [
    "SCHEMA_VERSION",
    "ArbitrationInput",
    "CapabilityView",
    "ConfidenceBand",
    "ModeResolution",
    "ModelSemanticOutput",
    "SemanticAssessment",
    "SemanticBudget",
    "SemanticCondition",
    "SemanticContext",
    "SemanticIntent",
    "SemanticInvalidOutput",
    "SemanticMode",
    "SemanticSlot",
    "SemanticTaskKind",
    "SemanticToolCandidate",
    "SemanticUnderstandingService",
    "SlotOrigin",
    "TurnView",
    "analyze",
    "arbitrate",
    "build_context",
    "extract_json_object",
    "parse_model_output",
    "redact_for_log",
    "render_prompt",
    "resolve_mode",
    "validate_semantics",
    "weakest_mode",
]
