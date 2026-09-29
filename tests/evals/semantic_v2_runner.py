"""Run semantic-v2 against one frozen dataset split and write a redacted report.

This opt-in evaluation harness uses the production prompt, context builder,
strict validator, arbitration and Gitee retry/error boundary. The temporary
``enable_thinking=false`` request option is applied only by this test adapter;
it is not a production-provider configuration. The input corpus is synthetic,
and the report stores no text, history or slot values.

Example (run from the repository root so ``.env`` is loaded):

    export PYTHONPATH="apps/api/src:packages/contracts/src:packages/policy/src:"\
    "packages/observability/src:."
    .venv/bin/python tests/evals/semantic_v2_runner.py --split holdout
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import re
import time
from collections import Counter, defaultdict
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

if __package__:
    from .semantic_v2_dataset import cases_for_split, semantic_v2_cases
    from .semantic_v2_manifest import FROZEN_DATASET_HASH
else:
    from semantic_v2_dataset import cases_for_split, semantic_v2_cases
    from semantic_v2_manifest import FROZEN_DATASET_HASH

from platform_core.agent_runtime.intent import classify
from platform_core.agent_runtime.semantic.context import SYSTEM_PROMPT_VERSION, build_context
from platform_core.agent_runtime.semantic.contracts import (
    SemanticInvalidOutput,
    SemanticMode,
    SlotOrigin,
)
from platform_core.agent_runtime.semantic.service import (
    REGISTERED_CONDITION_FIELDS,
    AnalysisRequest,
    SemanticBudget,
    analyze,
)
from platform_core.agent_runtime.semantic.validator import (
    CapabilityView,
    TurnView,
    parse_model_output,
    validate_semantics,
)
from platform_core.config import get_settings
from platform_core.evaluation.semantic_eval import (
    EvalCase,
    Split,
    assign_split,
    compare,
    dataset_hash,
    validate_dataset,
)
from platform_core.llm.factory import ChatTask, chat_model_for
from platform_core.llm.gitee_ai import GiteeAiClient
from platform_core.llm.provider import ChatMessage, ChatResult

_LAST_MODEL_RESPONSE: ContextVar[str | None] = ContextVar(
    "semantic_v2_last_model_response", default=None
)


class NoThinkingGiteeProvider:
    """Evaluation-only Gitee adapter for the already-probed no-thinking mode."""

    def __init__(self, client: GiteeAiClient) -> None:
        self._client = client

    async def complete(
        self,
        messages: list[ChatMessage],
        *,
        max_tokens: int = 1024,
        temperature: float = 0.0,
        model: str | None = None,
    ) -> ChatResult:
        started = time.monotonic()
        selected_model = model or self._client._chat_model
        payload: dict[str, Any] = {
            "model": selected_model,
            "messages": [
                {"role": str(message.role), "content": message.content} for message in messages
            ],
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": False,
            "chat_template_kwargs": {"enable_thinking": False},
        }
        body = await self._client._post("/chat/completions", payload)
        choices = body.get("choices") or []
        if not choices:
            raise RuntimeError("provider returned no choices")
        message = choices[0].get("message") or {}
        usage = body.get("usage") or {}
        result = ChatResult(
            text=str(message.get("content") or ""),
            model=str(body.get("model") or selected_model),
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
            reasoning=str(message.get("reasoning_content") or ""),
            latency_ms=int((time.monotonic() - started) * 1000),
            raw_usage=usage if isinstance(usage, dict) else {},
        )
        _LAST_MODEL_RESPONSE.set(result.text)
        return result


@dataclass(frozen=True)
class CaseRun:
    case_id: str
    intents: list[str]
    scene: str
    business_line: str
    tool_candidates: tuple[str, ...]
    unavailable_tool_candidate_count: int
    slots: dict[str, Any]
    missing_slots: list[str]
    validation_status: str
    reason_codes: tuple[str, ...]
    validation_bucket: str | None
    latency_ms: int
    queue_wait_ms: int
    prompt_tokens: int
    completion_tokens: int


def _percentile(values: list[int], percentile: float) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(percentile * len(ordered)) - 1)]


def tool_selection_report(cases: list[EvalCase], results: list[CaseRun]) -> dict[str, Any]:
    """Score unique-tool top-1 cases and count unavailable suggestions."""
    case_by_id = {case.case_id: case for case in cases}
    eligible = [
        result
        for result in results
        if len(case_by_id[result.case_id].expected_tools) == 1
        and len(case_by_id[result.case_id].available_tools) == 1
    ]
    correct = sum(
        bool(result.tool_candidates)
        and result.tool_candidates[0] == case_by_id[result.case_id].expected_tools[0]
        for result in eligible
    )
    unavailable_by_slice: Counter[str] = Counter()
    for result in results:
        if not result.unavailable_tool_candidate_count:
            continue
        for slice_name in case_by_id[result.case_id].slices:
            unavailable_by_slice[slice_name] += result.unavailable_tool_candidate_count
    return {
        "top1_eligible_cases": len(eligible),
        "top1_correct": correct,
        "top1_accuracy": round(correct / len(eligible), 4) if eligible else None,
        "candidate_count": sum(len(result.tool_candidates) for result in results),
        "unavailable_candidate_count": sum(
            result.unavailable_tool_candidate_count for result in results
        ),
        "unavailable_candidate_by_slice": dict(sorted(unavailable_by_slice.items())),
        "execution_count": 0,
        "capability_source": "synthetic allowlist from platform read schemas; no tool execution",
    }


def _confirmed_slots(output: Any) -> dict[str, Any]:
    result: dict[str, Any] = {}
    conflicting: set[str] = set()
    allowed_origins = {SlotOrigin.CUSTOMER_STATED, SlotOrigin.VERIFIED_RECEIPT}
    for intent in output.intents:
        for slot in intent.slots:
            if not slot.confirmed or slot.origin not in allowed_origins:
                continue
            if slot.name in result and result[slot.name] != slot.value:
                conflicting.add(slot.name)
            else:
                result[slot.name] = slot.value
    for name in conflicting:
        result.pop(name, None)
    return result


def _capabilities_for_case(case: Any) -> dict[str, CapabilityView]:
    """Build a synthetic allowlist from the registered platform read schemas."""
    from platform_core.tool_gateway.registry import TOOL_CATALOG

    capabilities: dict[str, CapabilityView] = {}
    for name in case.available_tools:
        definition = TOOL_CATALOG.get(name)
        if definition is None:
            raise ValueError(f"evaluation case references unregistered tool {name!r}")
        risk, schema, _permissions, _confirmation = definition
        properties = schema.get("properties", {})
        required = schema.get("required", [])
        if risk not in {"read", "low_risk"}:
            raise ValueError(f"evaluation case may only offer read tools: {name!r}")
        capabilities[name] = CapabilityView(
            tool_name=name,
            risk_class=risk,
            allowed_task_kinds=frozenset({"read"}),
            parameter_names=tuple(sorted(properties)) if isinstance(properties, dict) else (),
            required_parameters=tuple(sorted(required)) if isinstance(required, list) else (),
        )
    return capabilities


def _safe_validation_bucket(
    raw: str,
    *,
    turns: list[TurnView],
    capabilities: dict[str, Any],
) -> str:
    """Reduce a rejected output to a safe field/reason category.

    The raw response and exception detail are held only in memory. The returned
    value is a closed diagnostic label; it never contains model text, slot
    values, or exception fragments that could echo customer content.
    """
    try:
        output = parse_model_output(raw)
        validate_semantics(
            output,
            turns=turns,
            capabilities=capabilities,
            condition_fields=REGISTERED_CONDITION_FIELDS,
        )
    except SemanticInvalidOutput as exc:
        detail = exc.detail.lower()
        if "no json object" in detail:
            return "json_missing"
        if "unterminated" in detail:
            return "json_unterminated"
        if "invalid json" in detail:
            return "json_malformed"
        if "top level" in detail:
            return "json_top_level"
        if "evidence references an unsent turn" in detail:
            return "evidence_unknown_turn"
        if "evidence range exceeds" in detail:
            return "evidence_out_of_bounds"
        if "unregistered field" in detail:
            return "condition_unregistered_field"
        if "in` needs a non-empty list" in detail:
            return "condition_empty_membership_list"
        if "depends_on must reference" in detail:
            return "dependency_invalid_reference"
        if "depends_on references an unknown" in detail:
            return "dependency_unknown_reference"
        if "dependency cycle" in detail:
            return "dependency_cycle"
        if "evidence" in detail:
            return "evidence_invalid"
        if "condition" in detail:
            return "condition_invalid"
        if "depends_on" in detail or "dependency" in detail:
            return "invalid_dependency"
        if ":" in detail:
            field_path = detail.split(":", 1)[0].strip()
            if re.fullmatch(r"[a-z0-9_.<>-]{1,64}", field_path):
                return f"schema_field:{field_path}"
        if "too many intents" in detail:
            return "schema_intent_limit"
        if "too many slots" in detail:
            return "schema_slot_limit"
        return "schema_constraint"
    return "validated"


_DIAGNOSTIC_GROUPS = ("human", "sensitive", "injection", "negation", "multi", "coreference")


def select_dev_diagnostic_sample(cases: list[Any]) -> list[Any]:
    """Select two phrase families from each of six stress groups, only from dev."""
    grouped: dict[str, dict[str, list[Any]]] = defaultdict(lambda: defaultdict(list))
    for case in cases:
        if assign_split(case.family) is not Split.DEV:
            continue
        parts = case.case_id.split("-")
        group = parts[2] if len(parts) >= 4 else "unknown"
        grouped[group][case.family].append(case)

    selected: list[Any] = []
    for group in _DIAGNOSTIC_GROUPS:
        if group not in grouped:
            raise ValueError(f"missing dev diagnostic group: {group}")
        families = sorted(grouped[group])
        for family in families[:2]:
            selected.append(min(grouped[group][family], key=lambda case: case.case_id))
    if len(selected) != 12:
        raise ValueError(f"expected 12 dev diagnostic cases across six slices, got {len(selected)}")
    return sorted(selected, key=lambda case: case.case_id)


async def _run_split(
    *,
    split: Split,
    max_concurrency: int,
    deadline_seconds: float,
    completion_tokens: int,
    diagnostic_sample: bool = False,
) -> dict[str, Any]:
    all_cases = semantic_v2_cases()
    digest = dataset_hash(all_cases)
    if digest != FROZEN_DATASET_HASH:
        raise SystemExit(
            "semantic-v2 dataset hash changed; re-review and update its manifest before running"
        )
    problems = validate_dataset(all_cases)
    if problems:
        raise SystemExit("semantic-v2 dataset is invalid: " + "; ".join(problems))

    settings = get_settings()
    if settings.llm_api_key is None:
        raise SystemExit("APP_LLM_API_KEY is not configured; no model request was sent")

    cases = cases_for_split(split)
    if diagnostic_sample:
        if split is not Split.DEV:
            raise ValueError("diagnostic samples may only come from the dev split")
        cases = select_dev_diagnostic_sample(cases)
    selected_case_ids = [case.case_id for case in cases] if diagnostic_sample else None
    model_name = chat_model_for(ChatTask.CLASSIFY)
    client = GiteeAiClient(chat_model=model_name, timeout_seconds=deadline_seconds, max_retries=0)
    provider = NoThinkingGiteeProvider(client)
    budget = SemanticBudget(
        deadline_seconds=deadline_seconds,
        max_retries=0,
        max_completion_tokens=completion_tokens,
    )
    semaphore = asyncio.Semaphore(max_concurrency)
    run_started_at = datetime.now(UTC)

    async def run_one(case: Any) -> CaseRun:
        queued_at = time.monotonic()
        async with semaphore:
            started = time.monotonic()
            history = list(case.history)
            capabilities = _capabilities_for_case(case)
            context = build_context(
                current_turn_id=f"current-{case.case_id}",
                current_text=case.text,
                history=history,
                mode=SemanticMode.SHADOW,
                capabilities=capabilities,
            )
            detection = classify(case.text)
            response_token = _LAST_MODEL_RESPONSE.set(None)
            try:
                assessment = await analyze(
                    AnalysisRequest(
                        context=context,
                        lease_owner_type="ai",
                        detection=detection,
                    ),
                    provider=provider,
                    capabilities=capabilities,
                    budget=budget,
                )
                raw_response = _LAST_MODEL_RESPONSE.get()
            finally:
                _LAST_MODEL_RESPONSE.reset(response_token)
            output = assessment.model_output
            intents = (
                [output.primary_intent.value, *(item.value for item in output.secondary_intents)]
                if output
                else []
            )
            tool_candidates = (
                tuple(candidate.tool_name for candidate in output.tool_candidates) if output else ()
            )
            slots = _confirmed_slots(output) if output else {}
            missing = (
                sorted({name for intent in output.intents for name in intent.missing_slots})
                if output
                else []
            )
            validation_bucket = None
            if assessment.validation_status != "valid":
                validation_bucket = (
                    _safe_validation_bucket(
                        raw_response,
                        turns=context.turns(),
                        capabilities=capabilities,
                    )
                    if raw_response is not None
                    else "provider_unavailable"
                )
            return CaseRun(
                case_id=case.case_id,
                intents=intents,
                scene=output.scene.value if output else "",
                business_line=output.business_line.value if output else "",
                tool_candidates=tool_candidates,
                unavailable_tool_candidate_count=sum(
                    name not in capabilities for name in tool_candidates
                ),
                slots=slots,
                missing_slots=missing,
                validation_status=assessment.validation_status,
                reason_codes=assessment.reason_codes,
                validation_bucket=validation_bucket,
                latency_ms=assessment.latency_ms or int((time.monotonic() - started) * 1000),
                queue_wait_ms=int((started - queued_at) * 1000),
                prompt_tokens=assessment.prompt_tokens,
                completion_tokens=assessment.completion_tokens,
            )

    started = time.monotonic()
    results = await asyncio.gather(*(run_one(case) for case in cases))
    elapsed_ms = int((time.monotonic() - started) * 1000)
    rule_detections = {case.case_id: classify(case.text) for case in cases}
    rules_predictions = {
        case_id: [detection.primary_kind.value, *(kind.value for kind in detection.secondary_kinds)]
        for case_id, detection in rule_detections.items()
    }
    rules_scenes = {
        case_id: detection.scene.value for case_id, detection in rule_detections.items()
    }
    rules_business_lines = {
        case_id: detection.business_line.value for case_id, detection in rule_detections.items()
    }
    model_predictions = {result.case_id: result.intents for result in results}
    model_scenes = {result.case_id: result.scene for result in results}
    model_business_lines = {result.case_id: result.business_line for result in results}
    slot_predictions = {result.case_id: result.slots for result in results}
    missing_predictions = {result.case_id: result.missing_slots for result in results}
    tool_selection = tool_selection_report(cases, results)
    comparison = compare(
        all_cases,
        split=split,
        case_ids=selected_case_ids,
        rules_predictions=rules_predictions,
        model_predictions=model_predictions,
        rules_scene_predictions=rules_scenes,
        model_scene_predictions=model_scenes,
        rules_business_line_predictions=rules_business_lines,
        model_business_line_predictions=model_business_lines,
        model_slot_predictions=slot_predictions,
        model_missing_slot_predictions=missing_predictions,
        model_name=model_name,
        prompt_version=SYSTEM_PROMPT_VERSION,
    )
    valid = sum(result.validation_status == "valid" for result in results)
    provider_failures = [
        {
            "case_id": result.case_id,
            "validation_status": result.validation_status,
            "reason_codes": list(result.reason_codes),
            "validation_bucket": result.validation_bucket,
        }
        for result in results
        if result.validation_status != "valid"
    ]
    validation_buckets = Counter(
        result.validation_bucket for result in results if result.validation_bucket is not None
    )
    reason_code_counts = Counter(code for result in results for code in result.reason_codes)
    latencies = [result.latency_ms for result in results]
    queue_waits = [result.queue_wait_ms for result in results]
    return {
        "dataset_id": "semantic-v2-r1-balanced-highrisk-synthetic-2026-09-27",
        "dataset_hash": digest,
        "split": split.value,
        "case_count": len(cases),
        "diagnostic_sample": diagnostic_sample,
        "provenance": "synthetic; template-authored, not independently human-reviewed",
        "production_quality_claim_eligible": False,
        "model": model_name,
        "prompt_version": SYSTEM_PROMPT_VERSION,
        "request_options": {"enable_thinking": False, "max_completion_tokens": completion_tokens},
        "run_started_at": run_started_at.isoformat(),
        "run_finished_at": datetime.now(UTC).isoformat(),
        "elapsed_ms": elapsed_ms,
        "max_concurrency": max_concurrency,
        "deadline_seconds": deadline_seconds,
        "valid_output_count": valid,
        "invalid_or_unavailable_count": len(results) - valid,
        "validation_failure_buckets": dict(sorted(validation_buckets.items())),
        "reason_code_counts": dict(sorted(reason_code_counts.items())),
        "total_prompt_tokens": sum(result.prompt_tokens for result in results),
        "total_completion_tokens": sum(result.completion_tokens for result in results),
        "tool_selection": tool_selection,
        "latency_ms": {
            "p50": _percentile(latencies, 0.50),
            "p95": _percentile(latencies, 0.95),
            "p99": _percentile(latencies, 0.99),
            "queue_wait_p95": _percentile(queue_waits, 0.95),
        },
        "comparison": comparison.as_dict(),
        "provider_failures": provider_failures,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--split", choices=[item.value for item in Split], default="holdout")
    parser.add_argument("--concurrency", type=int, choices=range(1, 9), default=4)
    parser.add_argument("--deadline-seconds", type=float, default=30.0)
    parser.add_argument("--completion-tokens", type=int, default=900)
    parser.add_argument(
        "--diagnostic-sample",
        action="store_true",
        help="Select 12 dev-only cases (two phrase families per slice); never samples holdout.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Report path; defaults to tests/artifacts/semantic_v2_<split>.json",
    )
    args = parser.parse_args()
    if args.deadline_seconds < 1:
        parser.error("deadline-seconds must be at least 1")
    if args.completion_tokens < 1:
        parser.error("completion-tokens must be at least 1")
    if args.diagnostic_sample and args.split != Split.DEV.value:
        parser.error("--diagnostic-sample requires --split dev")

    report = asyncio.run(
        _run_split(
            split=Split(args.split),
            max_concurrency=args.concurrency,
            deadline_seconds=args.deadline_seconds,
            completion_tokens=args.completion_tokens,
            diagnostic_sample=args.diagnostic_sample,
        )
    )
    output_path = args.output or Path("tests/artifacts") / f"semantic_v2_{args.split}.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    output_path.chmod(0o600)
    print(
        json.dumps(
            {
                "report_path": str(output_path),
                "dataset_hash": report["dataset_hash"],
                "split": report["split"],
                "case_count": report["case_count"],
                "valid_output_count": report["valid_output_count"],
                "invalid_or_unavailable_count": report["invalid_or_unavailable_count"],
                "p95_ms": report["latency_ms"]["p95"],
                "total_prompt_tokens": report["total_prompt_tokens"],
                "total_completion_tokens": report["total_completion_tokens"],
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
