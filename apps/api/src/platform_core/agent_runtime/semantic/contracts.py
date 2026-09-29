"""SemanticAssessment: the model's *suggestion* contract (spec 3).

Everything in this module is untrusted input. The field names, the enums and
the structure exist so a model output can be **rejected** precisely, not so a
model output can be trusted:

- `extra="forbid"` on every model, so an unknown field is a validation error
  rather than a silently ignored suggestion that later reads as a decision.
- Bounded arrays (`max_length`), because "intents" is where an unconstrained
  model produces 40 tasks and a downstream budget silently truncates whichever
  ones it likes.
- `rule_result` / `effective_decision` / `reason_codes` are declared here but
  populated by the **server**, never by the model. They are excluded from the
  model-facing schema below and set during arbitration.

Deliberately NOT here: anything that would let a model state a final outcome.
There is no `succeeded`, no `executed`, no `approved`, no `tool_result`. A model
that emits a field like that fails validation rather than being coerced.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from platform_core.agent_runtime.intent import BusinessLine, IntentKind, Scene

SCHEMA_VERSION = "v1"

# Hard bounds. These are contract values, not tunables: changing one changes
# the dataset's meaning and must bump SCHEMA_VERSION.
MAX_INTENTS = 5
MAX_CONDITION_DEPTH = 3
MAX_SLOT_NODES = 24
MAX_EVIDENCE_SPANS = 12
MAX_TOOL_CANDIDATES = 3
MAX_SCALAR_CHARS = 200
MAX_HISTORY_TURNS = 8
INPUT_TOKEN_BUDGET = 4096


class SemanticMode(StrEnum):
    """How much authority the semantic layer has (spec 4).

    Ordered by increasing authority, and the ordering is used to resolve a
    configuration conflict: the minimum of the requested and granted modes
    wins, so granting `assist` on a tenant that is only configured for
    `shadow` cannot escalate.
    """

    OFF = "off"
    SHADOW = "shadow"
    ASSIST = "assist"
    SEMANTIC_READ = "semantic_read"

    @property
    def rank(self) -> int:
        return _MODE_RANK[self]

    def allows(self, other: SemanticMode) -> bool:
        return self.rank >= other.rank


_MODE_RANK: dict[SemanticMode, int] = {
    SemanticMode.OFF: 0,
    SemanticMode.SHADOW: 1,
    SemanticMode.ASSIST: 2,
    SemanticMode.SEMANTIC_READ: 3,
}


class SemanticTaskKind(StrEnum):
    """What a proposed task is.

    `read` may eventually execute (in `semantic_read`, through the gateway);
    `write` never executes from the semantic layer - it becomes a proposal or a
    human task. `clarify` is the ask-the-customer task.
    """

    READ = "read"
    WRITE = "write"
    CLARIFY = "clarify"


class ConfidenceBand(StrEnum):
    """The model's own signal strength.

    Recorded for evaluation and UI display only. Never an input to an
    authorization, routing or rollout decision - a model that reports `high`
    on a wrong answer is exactly the failure this band must not be able to
    cause.
    """

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class SlotOrigin(StrEnum):
    """Where a slot value came from. This distinction is the whole point.

    `customer_stated` is in the customer's words. `verified_receipt` came back
    from an authorized business read. `inferred` is the model's guess and is
    **not** a value to act on. A slot with no origin is invalid, because
    "有来源" is a gate in the eval contract (EVAL-02) and an unlabelled value
    would pass the syntax while failing the requirement.
    """

    CUSTOMER_STATED = "customer_stated"
    VERIFIED_RECEIPT = "verified_receipt"
    INFERRED = "inferred"


class ConditionOperator(StrEnum):
    """The complete set of condition operators the server registers.

    An unregistered operator is a validation error, not a fallback to
    evaluation. Nothing here can execute code, reach a URL, or run a template
    expression - the value is compared against a literal, in Python, on data
    the server already owns.
    """

    EQUALS = "eq"
    NOT_EQUALS = "ne"
    IN = "in"


class SemanticInvalidOutput(Exception):
    """Model output failed validation. Always degrades; never partially applies."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


class _Strict(BaseModel):
    """Base for every model-facing schema. Unknown fields are errors."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class EvidenceSpan(_Strict):
    """A character range inside a turn that was actually sent to the model.

    Validated against the redacted payload the server built, so a model cannot
    cite a turn that was truncated away, a turn from another conversation, or
    an offset past the end of the text.
    """

    turn_id: str = Field(max_length=64)
    start: Annotated[int, Field(ge=0)]
    end: Annotated[int, Field(ge=0)]

    @field_validator("end")
    @classmethod
    def _end_after_start(cls, v: int, info: Any) -> int:
        start = info.data.get("start")
        if start is not None and v <= start:
            raise ValueError("evidence end must be greater than start")
        return v


class SemanticSlot(_Strict):
    """One extracted value, with its provenance and confirmation state.

    `value` is intentionally typed `JsonValue` and length-bounded: an address or
    an invoice number is a short string, and a model returning a nested object
    where a scalar belongs is a bug we want to reject, not store.
    """

    name: Annotated[str, Field(min_length=1, max_length=64)]
    value: Any = None
    origin: SlotOrigin
    confirmed: bool = False

    @field_validator("value")
    @classmethod
    def _bounded_scalar(cls, v: Any) -> Any:
        if v is None or isinstance(v, (bool, int, float)):
            return v
        if isinstance(v, str):
            if len(v) > MAX_SCALAR_CHARS:
                raise ValueError(f"slot value exceeds {MAX_SCALAR_CHARS} characters")
            return v
        if isinstance(v, list):
            if len(v) > 8:
                raise ValueError("slot list exceeds 8 entries")
            return [_scalar(v) for v in v]
        if isinstance(v, dict):
            return {str(k)[:32]: _scalar(val) for k, val in list(v.items())[:8]}
        raise ValueError("slot value must be a scalar, a short list, or a flat object")

    def as_snapshot(self) -> dict[str, Any]:
        """Audit-safe projection.

        Names, origin and confirmation only - never the value. `intent.py`'s
        `IntentDetection.as_dict` sets this precedent: the run already stores an
        input hash, and a classification is only worth recording if it can be
        read back without re-exposing what the customer typed. A real delivery
        address in an audit row would be a copy of customer data in a place
        with weaker access control than the task row itself.
        """
        return {
            "name": self.name,
            "origin": self.origin.value,
            "confirmed": self.confirmed,
        }


def _scalar(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value[:MAX_SCALAR_CHARS]
    return str(value)[:MAX_SCALAR_CHARS]


class SemanticCondition(_Strict):
    """A server-registered predicate over a field the server already knows.

    `field` is resolved against a server-side allowlist, not against model
    output, so this cannot become a read of an arbitrary field.
    """

    field: Annotated[str, Field(min_length=1, max_length=64)]
    operator: ConditionOperator
    value: Any = None

    @field_validator("value")
    @classmethod
    def _bounded(cls, v: Any) -> Any:
        if isinstance(v, list) and len(v) > 16:
            raise ValueError("condition list exceeds 16 entries")
        if isinstance(v, str) and len(v) > MAX_SCALAR_CHARS:
            raise ValueError("condition value too long")
        return v


class SemanticIntent(_Strict):
    """One customer need, as the model proposes it."""

    task_kind: SemanticTaskKind
    source_turn_id: Annotated[str, Field(min_length=1, max_length=64)]
    evidence: Annotated[list[EvidenceSpan], Field(max_length=4)] = Field(default_factory=list)
    slots: Annotated[list[SemanticSlot], Field(max_length=8)] = Field(default_factory=list)
    missing_slots: Annotated[list[str], Field(max_length=8)] = Field(default_factory=list)
    depends_on: Annotated[list[str], Field(max_length=4)] = Field(default_factory=list)
    condition: SemanticCondition | None = None


class SemanticToolCandidate(_Strict):
    """A tool the model suggests.

    `tool_name` is checked against the server-provided capability set *after*
    parsing, and `risk_class` is assigned by the server from the registry - a
    model claiming a read tool is `low_risk` cannot downgrade a write.
    """

    tool_name: Annotated[str, Field(min_length=1, max_length=127)]
    reason: Annotated[str, Field(max_length=200)] = ""


class ModelSemanticOutput(_Strict):
    """Exactly what the model is allowed to return.

    Note what is absent: no tenant, no actor, no permission, no risk class, no
    final decision, no execution result. Those are server-owned, and their
    absence from this schema is the enforcement.
    """

    primary_intent: IntentKind
    secondary_intents: Annotated[list[IntentKind], Field(max_length=4)] = Field(
        default_factory=list
    )
    scene: Scene = Scene.UNSPECIFIED
    business_line: BusinessLine = BusinessLine.UNSPECIFIED
    intents: Annotated[list[SemanticIntent], Field(max_length=MAX_INTENTS)] = Field(
        default_factory=list
    )
    evidence_spans: Annotated[list[EvidenceSpan], Field(max_length=MAX_EVIDENCE_SPANS)] = Field(
        default_factory=list
    )
    confidence_band: ConfidenceBand = ConfidenceBand.LOW
    needs_clarification: bool = False
    emotion_signal: Annotated[str, Field(max_length=32)] | None = None
    tool_candidates: Annotated[
        list[SemanticToolCandidate], Field(max_length=MAX_TOOL_CANDIDATES)
    ] = Field(default_factory=list)

    def slot_nodes(self) -> int:
        return sum(len(i.slots) for i in self.intents)


class SemanticAssessment(BaseModel):
    """The server's record: model suggestion + rule result + decision.

    Not a model-facing schema. Built by `arbitration.arbitrate` from a validated
    `ModelSemanticOutput` and the deterministic `IntentDetection`, so the two
    can be compared, replayed and reported side by side.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = SCHEMA_VERSION
    assessment_id: str
    # Snapshot of the deterministic classification for the same utterance.
    rule_scene: Scene
    rule_primary_intent: IntentKind
    rule_secondary_intents: tuple[IntentKind, ...]
    rule_route: str
    rule_action: str
    # The model's suggestion, or None when it was rejected/absent.
    model_output: ModelSemanticOutput | None
    # Whether the model and the rules agreed, disagreed, or only one exists.
    # Reported, never used to grant authority.
    agreement: Literal["agree", "disagree", "rules_only", "model_only"]
    mode: SemanticMode
    # Server-decided. Never from the model.
    effective_decision: str
    reason_codes: tuple[str, ...]
    validation_status: Literal["valid", "rejected", "not_attempted"]
    truncated: bool = False
    truncation_reason: str = ""
    prompt_version: str = ""
    model_name: str = ""
    latency_ms: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0

    def as_snapshot(self) -> dict[str, Any]:
        """Audit/metrics projection with no customer text and no slot values."""
        return {
            "schema_version": self.schema_version,
            "mode": self.mode.value,
            "agreement": self.agreement,
            "effective_decision": self.effective_decision,
            "reason_codes": list(self.reason_codes),
            "validation_status": self.validation_status,
            "rule_route": self.rule_route,
            "rule_action": self.rule_action,
            "rule_primary_intent": self.rule_primary_intent.value,
            "model_primary_intent": (
                self.model_output.primary_intent.value if self.model_output else None
            ),
            # Count, not content: how many slots and tasks were proposed.
            "intent_count": len(self.model_output.intents) if self.model_output else 0,
            "tool_candidate_count": (
                len(self.model_output.tool_candidates) if self.model_output else 0
            ),
            "confidence_band": (
                self.model_output.confidence_band.value if self.model_output else None
            ),
            "needs_clarification": (
                self.model_output.needs_clarification if self.model_output else None
            ),
            "truncated": self.truncated,
            "latency_ms": self.latency_ms,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
        }


__all__ = [
    "INPUT_TOKEN_BUDGET",
    "MAX_CONDITION_DEPTH",
    "MAX_EVIDENCE_SPANS",
    "MAX_HISTORY_TURNS",
    "MAX_INTENTS",
    "MAX_SCALAR_CHARS",
    "MAX_SLOT_NODES",
    "MAX_TOOL_CANDIDATES",
    "SCHEMA_VERSION",
    "ConditionOperator",
    "ConfidenceBand",
    "EvidenceSpan",
    "ModelSemanticOutput",
    "SemanticAssessment",
    "SemanticCondition",
    "SemanticInvalidOutput",
    "SemanticIntent",
    "SemanticMode",
    "SemanticSlot",
    "SemanticTaskKind",
    "SemanticToolCandidate",
    "SlotOrigin",
]
