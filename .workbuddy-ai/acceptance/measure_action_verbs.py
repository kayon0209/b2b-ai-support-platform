"""Measure a candidate `ACTION_VERBS` widening against the whole eval dataset.

`ACTION_VERBS` decides which utterances the taxonomy calls action requests.
Adding a verb widens four syntactic forms at once (bare imperative, `please
<verb>`, `can you <verb>`, `I want to <verb>`), so the question "is this verb
safe?" is only answerable by running every question the project has a
documented expectation for and looking at what moved.

The repository's own rule, learned the hard way: a heuristic exercised only
through a harness must be tested at the production scale, and a new heuristic
must be validated against the whole eval dataset before it is wired in.

Usage: python measure_action_verbs.py [verb ...]
"""

from __future__ import annotations

import sys

from platform_core.agent_runtime import qa_path
from platform_core.agent_runtime.intent import classify

sys.path.insert(0, ".")
from tests.evals import dataset  # noqa: E402

# Collections of EvalCase the dataset exports. Discovered rather than listed so
# a new category is measured the day it is added, not the day someone
# remembers to add it here.
COLLECTIONS = (
    "ANSWERABLE",
    "UNANSWERABLE",
    "CONFLICTING",
    "EXPIRED",
    "UNAUTHORIZED",
    "AMBIGUOUS_IDENTITY",
    "POLICY_CONTRACT",
    "MULTILINGUAL",
    "INDIRECT_INJECTION",
    "BUSINESS_READ_WRITE",
    "REPEATED_ADVERSARIAL",
)


def cases() -> list[tuple[str, str, str, str | None]]:
    out: list[tuple[str, str, str, str | None]] = []
    for name in COLLECTIONS:
        for case in getattr(dataset, name):
            out.append((name, case.case_id, case.question, getattr(case, "expected_route", None)))
    return out


def routes() -> dict[str, str]:
    return {case_id: classify(question).route.value for _, case_id, question, _ in cases()}


def main(verbs: list[str]) -> None:
    all_cases = cases()
    baseline = routes()
    print(f"dataset: {len(all_cases)} cases\n")

    expected = {cid: exp for _, cid, _, exp in all_cases}
    wrong_baseline = [c for c, r in baseline.items() if expected[c] and r != expected[c]]
    print(f"baseline disagreements with expected_route: {len(wrong_baseline)} {wrong_baseline}\n")

    base_verbs = set(qa_path.ACTION_VERBS)
    for verb in verbs:
        qa_path.ACTION_VERBS = base_verbs | {qa_path._stem(verb)}
        after = routes()
        moved = {cid: (baseline[cid], after[cid]) for cid in baseline if baseline[cid] != after[cid]}
        into_write = {cid: v for cid, v in moved.items() if v[1] == "business_write"}
        print(f"--- +{verb}: {len(moved)} case(s) changed route, {len(into_write)} into business_write")
        for cid, (before, now) in sorted(moved.items()):
            exp = expected[cid] or "-"
            flag = "  <-- REGRESSION" if exp and exp != now else ""
            print(f"      {cid}: {before} -> {now} (expected {exp}){flag}")
        if not moved:
            print("      (no effect on any measured question)")
    qa_path.ACTION_VERBS = base_verbs


if __name__ == "__main__":
    main(sys.argv[1:] or ["create", "report", "file", "open", "raise", "submit"])
