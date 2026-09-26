"""The semantic service: provider call, budget, timeout, degrade.

One function does the work (`analyze`); `SemanticUnderstandingService` is the
thin injectable form the spec names, used where a test or the worker needs to
pass a stub provider.

Three properties this module is responsible for:

- **Fail closed on the model, open on the product.** No credential, no budget,
  an open circuit or a timeout all produce an assessment with
  `model_output=None` and a reason code. The caller keeps the rules' behaviour.
  Nothing here ever raises into the customer request path.
- **The CLASSIFY model is the one used.** `chat_model_for(ChatTask.CLASSIFY)`
  resolves `llm_model_classify`, falling back to `llm_model` exactly as the
  existing helper already documents. This is the first production call site of
  that helper (T00 §3.1).
- **One retry, transport errors only.** A retry after a 4xx would re-send a
  request the provider already refused; a retry loop on 5xx burns the deadline
  that the degrade path needs.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any

from platform_core.agent_runtime.intent import IntentDetection, classify
from platform_core.agent_runtime.semantic.arbitration import (
    REASON_INVALID_OUTPUT,
    REASON_MODEL_TIMEOUT,
    REASON_MODEL_UNAVAILABLE,
    REASON_NO_PROVIDER,
    ArbitrationInput,
    arbitrate,
)
from platform_core.agent_runtime.semantic.context import (
    SemanticContext,
    build_context,
    render_prompt,
)
from platform_core.agent_runtime.semantic.contracts import (
    SemanticAssessment,
    SemanticInvalidOutput,
    SemanticMode,
)
from platform_core.agent_runtime.semantic.validator import (
    CapabilityView,
    ValidationOutcome,
    parse_model_output,
    validate_semantics,
)
from platform_core.llm.provider import ChatProvider

# Design targets from spec 4, to be measured with a real provider before they
# are treated as verified. Classification runs on every message and must not be
# the reason a customer waits.
CLASSIFY_DEADLINE_SECONDS = 2.0
COPILOT_DEADLINE_SECONDS = 8.0
MAX_CLASSIFY_RETRIES = 1

# Condition fields the server registers. A model naming anything else is
# refused: conditions are evaluated against data the platform owns, and the
# registry is what keeps that from becoming an arbitrary read.
REGISTERED_CONDITION_FIELDS: frozenset[str] = frozenset(
    {
        "order.status",
        "order.shipped",
        "invoice.exists",
        "case.status",
        "account.contract_status",
    }
)

SEMANTIC_SYSTEM_PROMPT = (
    "You classify enterprise B2B support messages. Return exactly one JSON "
    "object and no other text. Propose intents, slots with their source, "
    "evidence positions and read-only tool candidates. Never decide a route, "
    "never grant permission, never claim an action succeeded. If a value is "
    "not present in the customer's own words, list it in missing_slots."
)


@dataclass(frozen=True)
class SemanticBudget:
    """Per-request model limits. Enforced before the call, not after."""

    deadline_seconds: float = CLASSIFY_DEADLINE_SECONDS
    max_retries: int = MAX_CLASSIFY_RETRIES
    max_completion_tokens: int = 900

    def remaining(self, started: float) -> float:
        return max(0.0, self.deadline_seconds - (time.monotonic() - started))


@dataclass(frozen=True)
class AnalysisRequest:
    """What the caller assembles. Server-derived fields only."""

    context: SemanticContext
    lease_owner_type: str
    detection: IntentDetection | None = None


async def analyze(
    request: AnalysisRequest,
    *,
    provider: ChatProvider | None,
    capabilities: dict[str, CapabilityView],
    budget: SemanticBudget | None = None,
) -> SemanticAssessment:
    """Run the semantic analysis and return the server's decision.

    Never raises for a model-side problem. A caller on the customer request
    path can await this without a try/except and still be correct.
    """
    budget = budget or SemanticBudget()
    ctx = request.context
    # The rules always run, in every mode, including `off`: they are the
    # platform's behaviour, not an optional extra.
    detection = request.detection or classify(ctx.current_turn.text)

    if ctx.mode is SemanticMode.OFF:
        return arbitrate(
            ArbitrationInput(
                detection=detection,
                mode=SemanticMode.OFF,
                lease_owner_type=request.lease_owner_type,
                capabilities=capabilities,
                model_output=None,
                outcome=None,
                truncated=ctx.truncated,
                truncation_reason=ctx.truncation_reason,
            )
        )

    if provider is None:
        # An unconfigured model boundary fails closed. The mode is preserved so
        # the record says "assist was requested but unavailable" rather than
        # claiming the tenant is on `off` - the two need different operator
        # responses (configure a credential vs. enable the flag).
        result = arbitrate(
            ArbitrationInput(
                detection=detection,
                mode=ctx.mode,
                lease_owner_type=request.lease_owner_type,
                capabilities=capabilities,
                model_output=None,
                outcome=None,
                truncated=ctx.truncated,
                truncation_reason=ctx.truncation_reason,
            )
        )
        if REASON_NO_PROVIDER not in result.reason_codes:
            result = result.model_copy(
                update={
                    "reason_codes": (*result.reason_codes, REASON_NO_PROVIDER),
                    "validation_status": "rejected",
                }
            )
        return result

    started = time.monotonic()
    model_out: Any = None
    outcome: ValidationOutcome | None = None
    failure_reason: str | None = None
    model_name = ""
    latency_ms = 0
    prompt_tokens = 0
    completion_tokens = 0

    from platform_core.llm.factory import ChatTask, chat_model_for
    from platform_core.llm.provider import ChatMessage, ModelError, ProviderRole

    assert provider is not None  # narrowed by the guard above

    model_name = chat_model_for(ChatTask.CLASSIFY)
    prompt = render_prompt(ctx)
    messages = [
        ChatMessage(role=ProviderRole.SYSTEM, content=SEMANTIC_SYSTEM_PROMPT),
        ChatMessage(role=ProviderRole.USER, content=prompt),
    ]

    attempt = 0
    while True:
        try:
            completion = await asyncio.wait_for(
                provider.complete(
                    messages,
                    max_tokens=budget.max_completion_tokens,
                    temperature=0.0,
                    model=model_name,
                ),
                timeout=budget.remaining(started) or 0.001,
            )
            latency_ms = int(completion.latency_ms or (time.monotonic() - started) * 1000)
            prompt_tokens = int(completion.prompt_tokens or 0)
            completion_tokens = int(completion.completion_tokens or 0)
            model_out = parse_model_output(completion.text)
            break
        except TimeoutError:
            failure_reason = REASON_MODEL_TIMEOUT
            break
        except SemanticInvalidOutput:
            failure_reason = REASON_INVALID_OUTPUT
            break
        except ModelError as exc:
            # Retry only what a retry can fix. A rejected request (4xx) will be
            # rejected again; the deadline is the scarce resource.
            if exc.retryable and attempt < budget.max_retries and budget.remaining(started) > 0:
                attempt += 1
                continue
            failure_reason = REASON_MODEL_UNAVAILABLE if exc.retryable else REASON_INVALID_OUTPUT
            break
        except Exception:  # noqa: BLE001 - a provider must not break the request
            failure_reason = REASON_MODEL_UNAVAILABLE
            break

    if model_out is not None:
        try:
            outcome = validate_semantics(
                model_out,
                turns=ctx.turns(),
                capabilities=capabilities,
                condition_fields=REGISTERED_CONDITION_FIELDS,
            )
        except SemanticInvalidOutput:
            failure_reason = REASON_INVALID_OUTPUT
            outcome = None
            model_out = None

    inp = ArbitrationInput(
        detection=detection,
        mode=ctx.mode,
        lease_owner_type=request.lease_owner_type,
        capabilities=capabilities,
        model_output=model_out,
        outcome=outcome,
        prompt_version=ctx.prompt_version,
        model_name=model_name if model_out is not None else "",
        latency_ms=latency_ms,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        truncated=ctx.truncated,
        truncation_reason=ctx.truncation_reason,
    )
    assessment = arbitrate(inp)
    if failure_reason is not None and failure_reason not in assessment.reason_codes:
        assessment = assessment.model_copy(
            update={
                "reason_codes": (*assessment.reason_codes, failure_reason),
                "validation_status": "rejected",
                "model_output": None,
            }
        )
    return assessment


class SemanticUnderstandingService:
    """The injectable form named in spec 3.

    Holds no session and no tenant state: it exists so a caller (the worker, a
    test) can supply a provider and a budget once and reuse them. Every
    argument that matters is still passed per call, so a service instance
    cannot leak one conversation's context into another's.
    """

    def __init__(
        self,
        provider: Any | None,
        *,
        budget: SemanticBudget | None = None,
    ) -> None:
        self._provider = provider
        self._budget = budget or SemanticBudget()

    async def analyze(
        self,
        *,
        current_turn_id: str,
        current_text: str,
        history: list[tuple[str, str]],
        mode: SemanticMode,
        capabilities: dict[str, CapabilityView],
        lease_owner_type: str,
        verified_facts: list[dict[str, Any]] | None = None,
        task_state: list[dict[str, Any]] | None = None,
    ) -> SemanticAssessment:
        ctx = build_context(
            current_turn_id=current_turn_id,
            current_text=current_text,
            history=history,
            mode=mode,
            capabilities=capabilities,
            verified_facts=verified_facts,
            task_state=task_state,
        )
        return await analyze(
            AnalysisRequest(context=ctx, lease_owner_type=lease_owner_type),
            provider=self._provider,
            capabilities=capabilities,
            budget=self._budget,
        )


__all__ = [
    "CLASSIFY_DEADLINE_SECONDS",
    "COPILOT_DEADLINE_SECONDS",
    "MAX_CLASSIFY_RETRIES",
    "REGISTERED_CONDITION_FIELDS",
    "SEMANTIC_SYSTEM_PROMPT",
    "AnalysisRequest",
    "SemanticBudget",
    "SemanticUnderstandingService",
    "analyze",
]
