"""Integrity gates for the frozen semantic-v2 synthetic corpus."""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

import pytest

from platform_core.agent_runtime.intent import IntentKind
from platform_core.agent_runtime.semantic.context import SYSTEM_PROMPT_VERSION
from platform_core.evaluation.semantic_eval import (
    REQUIRED_SLICES,
    Split,
    assign_split,
    dataset_hash,
    split_dataset,
    validate_dataset,
)

from .semantic_v2_dataset import semantic_v2_cases
from .semantic_v2_manifest import FROZEN_DATASET_HASH

MANIFEST = Path(__file__).parent / "data" / "semantic_v2_manifest.json"
pytestmark = pytest.mark.eval


def test_frozen_corpus_matches_its_manifest() -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    cases = semantic_v2_cases()
    splits = split_dataset(cases)

    assert len(cases) == manifest["case_count"]
    assert dataset_hash(cases) == manifest["dataset_hash"]
    assert manifest["dataset_hash"] == FROZEN_DATASET_HASH
    assert manifest["prompt_version"] == SYSTEM_PROMPT_VERSION
    assert {split.value: len(rows) for split, rows in splits.items()} == manifest["split_counts"]
    assert validate_dataset(cases) == []
    assert all(case.provenance == "synthetic" for case in cases)
    assert all(case.redacted_from is None for case in cases)
    assert len({case.family for case in cases}) == 150


def test_holdout_keeps_required_and_high_risk_slices() -> None:
    cases = semantic_v2_cases()
    holdout = split_dataset(cases)[Split.HOLDOUT]
    for slice_name in REQUIRED_SLICES:
        assert sum(slice_name in case.slices for case in holdout) > 0, slice_name
    for slice_name, minimum in json.loads(MANIFEST.read_text(encoding="utf-8"))[
        "holdout_minimums"
    ].items():
        count = sum(slice_name in case.slices for case in holdout)
        assert count >= minimum, f"{slice_name} has {count}, needs {minimum}"
    for intent_name, minimum in json.loads(MANIFEST.read_text(encoding="utf-8"))[
        "primary_intent_holdout_minimums"
    ].items():
        count = sum(intent_name in case.expected_intents for case in holdout)
        assert count >= minimum, f"{intent_name} has {count}, needs {minimum}"
    for slice_name, minimum in json.loads(MANIFEST.read_text(encoding="utf-8"))[
        "high_risk_holdout_minimums"
    ].items():
        count = sum(slice_name in case.slices for case in holdout)
        assert count >= minimum, f"{slice_name} has {count}, needs {minimum}"
    assert sum("high_risk_boundary" in case.slices for case in holdout) == 100
    unique_tool_cases = sum(
        len(case.expected_tools) == 1 and len(case.available_tools) == 1 for case in holdout
    )
    assert (
        unique_tool_cases
        == json.loads(MANIFEST.read_text(encoding="utf-8"))["holdout_unique_tool_cases"]
    )


def test_near_duplicate_cases_share_a_split_family() -> None:
    cases = semantic_v2_cases()
    by_text: dict[str, set[str]] = defaultdict(set)
    by_family: dict[str, set[Split]] = defaultdict(set)
    for case in cases:
        by_text[case.text].add(case.family)
        by_family[case.family].add(assign_split(case.family))

    assert all(len(by_family[family]) == 1 for family in by_family)
    for families in by_text.values():
        assert len({assign_split(family) for family in families}) == 1


def test_gold_labels_use_the_closed_intent_enum() -> None:
    allowed = {intent.value for intent in IntentKind}
    assert all(set(case.expected_intents) <= allowed for case in semantic_v2_cases())
