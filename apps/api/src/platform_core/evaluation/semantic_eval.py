"""Evaluation dataset and comparison runner (T03).

EVAL-01 asks for a documented provenance, an annotation guide, a frozen split
and a hash. EVAL-02 asks for measured quality against a fixed holdout, per
slice, with failures redacted. This module provides the machinery for both and
refuses to provide numbers for neither:

- **No real model, no quality number.** `compare()` returns
  `model_unavailable` rather than scoring an empty prediction set as a
  failure - a missing model is a blocked gate, not a score of zero, and
  reporting it as zero would make "the model is terrible" and "the model was
  never called" indistinguishable in the report.
- **The rules baseline is always computable.** It needs no model and no
  credentials, so the report always has something to compare against, and the
  "improvement over baseline" figure EVAL-02 requires has a denominator.

Three splits, assigned by conversation family rather than by row, because the
failure this prevents is specific: two paraphrases of the same customer message
landing in different splits turns the holdout into a training set and every
number downstream becomes optimistic. `_family_of` derives the family from the
scenario id, so the split is a pure function of the input and cannot drift
between runs.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

# The split proportions EVAL-01 fixes. 60/20/20 by conversation family.
DEV_FRACTION = 0.6
VALIDATION_FRACTION = 0.2
# The remainder is the holdout.

# Slices the report must break down by. EVAL-02 requires per-intent,
# multi-intent, slot and sarcasm/negation numbers, and a single macro figure
# hides exactly the regressions that matter.
REQUIRED_SLICES = (
    "single_intent",
    "multi_intent",
    "missing_slots",
    "coreference",
    "negation_or_quotation",
    "explicit_human_request",
    "sensitive_or_privileged",
    "injection_attempt",
    "chinese",
    "english",
)


class Split(StrEnum):
    DEV = "dev"
    VALIDATION = "validation"
    HOLDOUT = "holdout"


@dataclass(frozen=True)
class EvalCase:
    """One labelled scenario.

    `text` is synthetic or redacted. The provenance rules are in
    `EVAL_PROVENANCE`, and a case whose provenance is unknown is refused by
    `validate_dataset` rather than scored - a dataset whose origin cannot be
    explained cannot be used to justify enabling a feature.
    """

    case_id: str
    family: str
    text: str
    expected_intents: tuple[str, ...]
    expected_scene: str | None = None
    expected_slots: dict[str, Any] = field(default_factory=dict)
    missing_slots: tuple[str, ...] = ()
    slices: tuple[str, ...] = ()
    provenance: str = "synthetic"
    # Never populated from a real conversation. A dataset that carries customer
    # text cannot be committed, so the field exists to make the absence
    # explicit and checkable rather than implicit.
    redacted_from: str | None = None
    # Oldest-first and synthetic/redacted only. Keeping authorized history in
    # the case lets coreference examples exercise the same input builder as
    # production without reading a live conversation.
    history: tuple[tuple[str, str], ...] = ()

    def as_row(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "family": self.family,
            "text": self.text,
            "expected_intents": list(self.expected_intents),
            "expected_scene": self.expected_scene,
            "expected_slots": self.expected_slots,
            "missing_slots": list(self.missing_slots),
            "slices": list(self.slices),
            "provenance": self.provenance,
            "history": [list(turn) for turn in self.history],
        }


# What may appear in `provenance`, and what each one obliges.
EVAL_PROVENANCE = {
    # Written for this evaluation. No customer data, no production traffic.
    "synthetic": "authored for this evaluation; no customer data",
    # Derived from a real conversation with the text replaced. `redacted_from`
    # must then name the source id, so the redaction is auditable.
    "redacted": "real conversation, text replaced; source id required in redacted_from",
}


def dataset_hash(cases: Iterable[EvalCase]) -> str:
    """A stable hash of the dataset.

    The exact wording is part of the measured input: changing a paraphrase can
    change model behavior even when its labels stay fixed. The report stores
    only this digest, never the source text.
    """
    canonical = json.dumps(
        sorted(
            (
                {
                    "case_id": c.case_id,
                    "family": c.family,
                    "text": c.text,
                    "history": c.history,
                    "intents": sorted(c.expected_intents),
                    "scene": c.expected_scene,
                    "slots": c.expected_slots,
                    "missing": sorted(c.missing_slots),
                    "slices": sorted(c.slices),
                    "provenance": c.provenance,
                }
                for c in cases
            ),
            key=lambda r: r["case_id"],
        ),
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def assign_split(family: str) -> Split:
    """Deterministic split by conversation family.

    Hash-based rather than a counter or a shuffle, so the same family lands in
    the same split on every machine, in every process, forever - the property
    that makes a holdout a holdout.
    """
    bucket = int(hashlib.sha256(family.encode()).hexdigest()[:8], 16) % 10_000
    if bucket < int(DEV_FRACTION * 10_000):
        return Split.DEV
    if bucket < int((DEV_FRACTION + VALIDATION_FRACTION) * 10_000):
        return Split.VALIDATION
    return Split.HOLDOUT


def split_dataset(cases: list[EvalCase]) -> dict[Split, list[EvalCase]]:
    out: dict[Split, list[EvalCase]] = {s: [] for s in Split}
    for case in cases:
        out[assign_split(case.family)].append(case)
    return out


def validate_dataset(cases: list[EvalCase]) -> list[str]:
    """Refuse a dataset that cannot support the claims EVAL-01 makes.

    Returns the problems found rather than raising, so a report can show all
    of them at once. An empty list means the dataset is usable.
    """
    problems: list[str] = []

    if not cases:
        problems.append("dataset is empty")
        return problems

    seen_ids: set[str] = set()
    for case in cases:
        if case.case_id in seen_ids:
            problems.append(f"{case.case_id}: duplicate case id")
        seen_ids.add(case.case_id)

        if case.provenance not in EVAL_PROVENANCE:
            problems.append(
                f"{case.case_id}: unknown provenance {case.provenance!r}; "
                f"expected one of {sorted(EVAL_PROVENANCE)}"
            )
        if case.provenance == "redacted" and not case.redacted_from:
            # A "redacted" case with no source id cannot be audited, which is
            # the entire point of declaring it redacted.
            problems.append(f"{case.case_id}: redacted case has no redacted_from source id")

        if not case.text.strip():
            problems.append(f"{case.case_id}: empty text")
        if not case.expected_intents:
            problems.append(f"{case.case_id}: no expected intent")
        if not case.family:
            problems.append(f"{case.case_id}: no conversation family")

    families: dict[str, set[str]] = {}
    for case in cases:
        families.setdefault(case.family, set()).add(case.case_id)
    for family, members in families.items():
        splits = {assign_split(family) for _ in members}
        if len(splits) > 1:
            # Cannot happen with a pure function of the family, and asserted
            # anyway because it is the property the whole split rests on.
            problems.append(f"family {family} spans multiple splits")

    problems.extend(missing_slice_problems(cases))
    return problems


def missing_slice_problems(cases: list[EvalCase]) -> list[str]:
    """Required slices no case covers.

    Separate from `validate_dataset` because it is a property of the dataset
    as a whole, not of any one case: a perfectly valid single case still
    leaves the required slices uncovered, and reporting that alongside "case
    c1 has no expected intent" would read as if the case were at fault.
    """
    missing = [s for s in REQUIRED_SLICES if not any(s in c.slices for c in cases)]
    if not missing:
        return []
    return [f"no cases cover required slices: {', '.join(missing)}"]


# --- comparison -------------------------------------------------------------


@dataclass
class SliceScore:
    """Per-slice numbers. Reported, never thresholded here."""

    slice_name: str
    total: int = 0
    correct: int = 0

    @property
    def accuracy(self) -> float:
        return self.correct / self.total if self.total else 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "slice": self.slice_name,
            "total": self.total,
            "correct": self.correct,
            "accuracy": round(self.accuracy, 4),
        }


@dataclass
class IntentClassScore:
    """One-vs-rest class counts for a multilabel intent evaluation."""

    intent: str
    true_positive: int = 0
    false_positive: int = 0
    false_negative: int = 0

    @property
    def support(self) -> int:
        return self.true_positive + self.false_negative

    @property
    def precision(self) -> float:
        denominator = self.true_positive + self.false_positive
        return self.true_positive / denominator if denominator else 0.0

    @property
    def recall(self) -> float:
        return self.true_positive / self.support if self.support else 0.0

    @property
    def f1(self) -> float:
        denominator = self.precision + self.recall
        return 2 * self.precision * self.recall / denominator if denominator else 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "intent": self.intent,
            "support": self.support,
            "true_positive": self.true_positive,
            "false_positive": self.false_positive,
            "false_negative": self.false_negative,
            "precision": round(self.precision, 4),
            "recall": round(self.recall, 4),
            "f1": round(self.f1, 4),
        }


@dataclass
class SlotNameScore:
    """One-vs-rest counts for a required-but-missing slot name."""

    slot_name: str
    true_positive: int = 0
    false_positive: int = 0
    false_negative: int = 0

    @property
    def precision(self) -> float:
        denominator = self.true_positive + self.false_positive
        return self.true_positive / denominator if denominator else 0.0

    @property
    def recall(self) -> float:
        denominator = self.true_positive + self.false_negative
        return self.true_positive / denominator if denominator else 0.0

    @property
    def f1(self) -> float:
        denominator = self.precision + self.recall
        return 2 * self.precision * self.recall / denominator if denominator else 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "slot_name": self.slot_name,
            "true_positive": self.true_positive,
            "false_positive": self.false_positive,
            "false_negative": self.false_negative,
            "precision": round(self.precision, 4),
            "recall": round(self.recall, 4),
            "f1": round(self.f1, 4),
        }


@dataclass
class ComparisonReport:
    """The result of one rules-vs-model comparison.

    `model_available` exists so the report can say "blocked" instead of
    reporting zeros. Every accuracy here is computed from real predictions;
    when the model was unavailable there are none, and the report says so.
    """

    dataset_hash: str
    split: Split
    case_count: int
    model_available: bool
    model_name: str = ""
    prompt_version: str = ""
    rules: dict[str, float] = field(default_factory=dict)
    model: dict[str, float] = field(default_factory=dict)
    rules_intents: list[IntentClassScore] = field(default_factory=list)
    model_intents: list[IntentClassScore] = field(default_factory=list)
    model_slot_exact_match_rate: float | None = None
    model_slot_case_count: int = 0
    model_missing_slots: dict[str, Any] = field(default_factory=dict)
    model_missing_slot_scores: list[SlotNameScore] = field(default_factory=list)
    rules_slices: list[SliceScore] = field(default_factory=list)
    model_slices: list[SliceScore] = field(default_factory=list)
    failures: list[dict[str, Any]] = field(default_factory=list)
    slot_failures: list[dict[str, Any]] = field(default_factory=list)
    missing_slot_failures: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def blocked(self) -> bool:
        return not self.model_available

    def as_dict(self) -> dict[str, Any]:
        return {
            "dataset_hash": self.dataset_hash,
            "split": self.split.value,
            "case_count": self.case_count,
            "model_available": self.model_available,
            "model_name": self.model_name,
            "prompt_version": self.prompt_version,
            "status": "blocked" if self.blocked else "measured",
            "rules": self.rules,
            "model": self.model,
            "rules_intents": [score.as_dict() for score in self.rules_intents],
            "model_intents": [score.as_dict() for score in self.model_intents],
            "model_slot_exact_match_rate": self.model_slot_exact_match_rate,
            "model_slot_case_count": self.model_slot_case_count,
            "model_missing_slots": self.model_missing_slots,
            "model_missing_slot_scores": [
                score.as_dict() for score in self.model_missing_slot_scores
            ],
            "rules_slices": [s.as_dict() for s in self.rules_slices],
            "model_slices": [s.as_dict() for s in self.model_slices],
            "failure_count": len(self.failures),
            "slot_failure_count": len(self.slot_failures),
            "missing_slot_failure_count": len(self.missing_slot_failures),
            # Case ids and reason codes only. A failure row carrying the text
            # would put customer-shaped content in a report that gets shared.
            "failures": self.failures,
            "slot_failures": self.slot_failures,
            "missing_slot_failures": self.missing_slot_failures,
            "notes": self.notes,
        }


def score_intent_set(expected: tuple[str, ...], predicted: Iterable[str]) -> bool:
    """Exact set equality.

    Order-insensitive, because the order intents appear in is not part of what
    the customer asked. A missed secondary intent counts as a failure, which
    EVAL-02 requires: the whole point of the multi-intent work is that a
    customer who asked three things did not get two of them dropped.
    """
    return set(expected) == set(predicted)


def score_intents(
    cases: list[EvalCase], predictions: dict[str, list[str]]
) -> tuple[dict[str, float], list[IntentClassScore]]:
    """Return multilabel micro/macro F1, exact-set match, and per-intent counts.

    F1 is calculated one-vs-rest for each label in the gold/prediction union;
    exact-set match remains separate because missing a secondary intent is a
    full-case failure even when the per-class F1 stays high.
    """
    labels = sorted(
        {label for case in cases for label in case.expected_intents}
        | {label for case in cases for label in predictions.get(case.case_id, [])}
    )
    scores: list[IntentClassScore] = []
    for label in labels:
        score = IntentClassScore(intent=label)
        for case in cases:
            expected = set(case.expected_intents)
            predicted = set(predictions.get(case.case_id, []))
            if label in expected and label in predicted:
                score.true_positive += 1
            elif label in predicted:
                score.false_positive += 1
            elif label in expected:
                score.false_negative += 1
        scores.append(score)

    total_tp = sum(score.true_positive for score in scores)
    total_fp = sum(score.false_positive for score in scores)
    total_fn = sum(score.false_negative for score in scores)
    micro_denominator = 2 * total_tp + total_fp + total_fn
    exact_matches = sum(
        score_intent_set(case.expected_intents, predictions.get(case.case_id, [])) for case in cases
    )
    macro_f1 = sum(score.f1 for score in scores) / len(scores) if scores else 0.0
    return (
        {
            "macro_f1": round(macro_f1, 4),
            "micro_f1": round(2 * total_tp / micro_denominator, 4) if micro_denominator else 0.0,
            "exact_match_rate": round(exact_matches / len(cases), 4) if cases else 0.0,
        },
        scores,
    )


def score_missing_slots(
    cases: list[EvalCase], predictions: dict[str, list[str]]
) -> tuple[dict[str, float], list[SlotNameScore]]:
    """Score required missing-slot names without including any slot values."""
    labels = sorted(
        {label for case in cases for label in case.missing_slots}
        | {label for case in cases for label in predictions.get(case.case_id, [])}
    )
    scores: list[SlotNameScore] = []
    for label in labels:
        score = SlotNameScore(slot_name=label)
        for case in cases:
            expected = set(case.missing_slots)
            predicted = set(predictions.get(case.case_id, []))
            if label in expected and label in predicted:
                score.true_positive += 1
            elif label in predicted:
                score.false_positive += 1
            elif label in expected:
                score.false_negative += 1
        scores.append(score)

    total_tp = sum(score.true_positive for score in scores)
    total_fp = sum(score.false_positive for score in scores)
    total_fn = sum(score.false_negative for score in scores)
    micro_denominator = 2 * total_tp + total_fp + total_fn
    exact_matches = sum(
        set(case.missing_slots) == set(predictions.get(case.case_id, [])) for case in cases
    )
    macro_f1 = sum(score.f1 for score in scores) / len(scores) if scores else 0.0
    return (
        {
            "macro_f1": round(macro_f1, 4),
            "micro_f1": round(2 * total_tp / micro_denominator, 4) if micro_denominator else 0.0,
            "exact_match_rate": round(exact_matches / len(cases), 4) if cases else 0.0,
        },
        scores,
    )


def compare(
    cases: list[EvalCase],
    *,
    split: Split,
    rules_predictions: dict[str, list[str]] | None = None,
    model_predictions: dict[str, list[str]] | None = None,
    model_slot_predictions: dict[str, dict[str, Any]] | None = None,
    model_missing_slot_predictions: dict[str, list[str]] | None = None,
    model_name: str = "",
    prompt_version: str = "",
) -> ComparisonReport:
    """Compare the rules baseline with a model, per slice.

    `model_predictions` of None means the model was not run - no credential, no
    budget, an open circuit. That is a blocked gate, and the report says
    `model_available: false` with empty model scores rather than zeros.
    """
    subset = [c for c in cases if assign_split(c.family) is split]
    digest = dataset_hash(cases)
    rules = rules_predictions or {}
    model = model_predictions

    report = ComparisonReport(
        dataset_hash=digest,
        split=split,
        case_count=len(subset),
        model_available=model is not None,
        model_name=model_name,
        prompt_version=prompt_version,
    )

    rules_slices = {s: SliceScore(s) for s in REQUIRED_SLICES}
    overall_rules = SliceScore("overall")

    for case in subset:
        for slice_name in case.slices:
            bucket = rules_slices.get(slice_name)
            if bucket is None:
                bucket = rules_slices[slice_name] = SliceScore(slice_name)
        predicted = rules.get(case.case_id)
        hit = predicted is not None and score_intent_set(case.expected_intents, predicted)
        overall_rules.total += 1
        overall_rules.correct += int(hit)
        for slice_name in case.slices:
            rules_slices[slice_name].total += 1
            rules_slices[slice_name].correct += int(hit)

    report.rules, report.rules_intents = score_intents(subset, rules)
    report.rules_slices = sorted(rules_slices.values(), key=lambda s: s.slice_name)

    if model is None:
        report.notes.append(
            "model predictions unavailable: quality gate is BLOCKED, not failed. "
            "No model accuracy is reported because none was measured."
        )
        return report

    model_slices = {s: SliceScore(s) for s in REQUIRED_SLICES}
    overall_model = SliceScore("overall")
    for case in subset:
        predicted = model.get(case.case_id)
        hit = predicted is not None and score_intent_set(case.expected_intents, predicted)
        overall_model.total += 1
        overall_model.correct += int(hit)
        for slice_name in case.slices:
            model_slices[slice_name].total += 1
            model_slices[slice_name].correct += int(hit)
        if not hit:
            # Case id and the expected/actual label sets. The text is not
            # included: this list is what gets pasted into a review, and a
            # customer utterance in it would be a copy of customer data
            # outside the access-controlled transcript.
            report.failures.append(
                {
                    "case_id": case.case_id,
                    "family": case.family,
                    "slices": list(case.slices),
                    "expected": sorted(case.expected_intents),
                    "predicted": sorted(predicted) if predicted is not None else None,
                    "reason": "intent_set_mismatch",
                }
            )

    report.model, report.model_intents = score_intents(subset, model)
    if model_slot_predictions is not None:
        report.model_slot_case_count = len(subset)
        slot_exact = 0
        for case in subset:
            expected_slots = case.expected_slots
            predicted_slots = model_slot_predictions.get(case.case_id, {})
            if predicted_slots == expected_slots:
                slot_exact += 1
                continue
            report.slot_failures.append(
                {
                    "case_id": case.case_id,
                    "family": case.family,
                    "slices": list(case.slices),
                    "expected_slot_names": sorted(expected_slots),
                    "predicted_slot_names": sorted(predicted_slots),
                    "reason": "slot_exact_match_mismatch",
                }
            )
        report.model_slot_exact_match_rate = round(slot_exact / len(subset), 4) if subset else 0.0

    if model_missing_slot_predictions is not None:
        report.model_missing_slots, report.model_missing_slot_scores = score_missing_slots(
            subset, model_missing_slot_predictions
        )
        for case in subset:
            expected_missing = set(case.missing_slots)
            predicted_missing = set(model_missing_slot_predictions.get(case.case_id, []))
            if expected_missing != predicted_missing:
                report.missing_slot_failures.append(
                    {
                        "case_id": case.case_id,
                        "family": case.family,
                        "slices": list(case.slices),
                        "expected": sorted(expected_missing),
                        "predicted": sorted(predicted_missing),
                        "reason": "missing_slot_set_mismatch",
                    }
                )
    report.model_slices = sorted(model_slices.values(), key=lambda s: s.slice_name)
    return report


__all__ = [
    "DEV_FRACTION",
    "EVAL_PROVENANCE",
    "REQUIRED_SLICES",
    "VALIDATION_FRACTION",
    "ComparisonReport",
    "EvalCase",
    "IntentClassScore",
    "SliceScore",
    "Split",
    "SlotNameScore",
    "assign_split",
    "compare",
    "dataset_hash",
    "missing_slice_problems",
    "score_intent_set",
    "score_intents",
    "score_missing_slots",
    "split_dataset",
    "validate_dataset",
]
