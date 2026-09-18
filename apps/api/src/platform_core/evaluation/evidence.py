"""Read release-gate evidence produced by the test session.

The gate needs three zero-tolerance counts and the counts must come from
the suites that prove them, not from a hand-typed literal. The
`pytest_plugins_release.gate_evidence` plugin writes them to
`tests/artifacts/release_gate_evidence.json` at session end; this module is
the only sanctioned reader, so a caller cannot invent its own parsing and
silently accept a file that is missing the invariant it cares about.

The writer is a pytest plugin rather than `tests/conftest.py` because the
suite has two test roots (`apps/api/tests` and `tests/`), so no single
conftest is an ancestor of both and a conftest would miss one root's tests.

The failure mode this guards against is specific and was real: before this
existed, `evaluate_release_gates(report, security={"cross_tenant_violations":
0, ...})` was the documented usage, and that zero was a constant. If the
negative suite was deselected (`pytest -k "not integration"`), the release
gate still reported zero violations, because nothing connected the two.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


def _find_repo_root(start: Path) -> Path:
    """Walk up until the directory that holds `pyproject.toml`.

    Hardcoding `parents[N]` would break the moment this module moves; the
    marker file is what actually defines the root, so search for it.
    """
    for candidate in (start, *start.parents):
        if (candidate / "pyproject.toml").is_file():
            return candidate
    # Fall back to the deepest ancestor rather than raising: an installed
    # (non-checkout) layout has no pyproject, and the caller's error message
    # about a missing evidence file is more useful than a path error here.
    return start.parents[-1]


REPO_ROOT = _find_repo_root(Path(__file__).resolve())
EVIDENCE_PATH = REPO_ROOT / "tests" / "artifacts" / "release_gate_evidence.json"

REQUIRED_INVARIANTS = (
    "cross_tenant_violations",
    "unauthorized_writes",
    "duplicate_replies",
)


class EvidenceUnavailable(RuntimeError):
    """No usable evidence file. Never degrade this into zeros."""


@dataclass(frozen=True)
class GateEvidence:
    counts: dict[str, int]
    measured: dict[str, bool]
    backing_tests: dict[str, int]
    not_run: dict[str, list[str]]
    tests_collected: int = 0

    def as_security_kwargs(self) -> dict[str, int]:
        """Shape expected by `evaluate_release_gates(security=...)`.

        Refuses to return a partial mapping: every required invariant must be
        present and measured, so the caller cannot accidentally pass a dict
        with the key it happens to remember.
        """
        missing = [n for n in REQUIRED_INVARIANTS if not self.measured.get(n)]
        if missing:
            raise EvidenceUnavailable(
                "these invariants have no passing backing tests: "
                + ", ".join(missing)
                + f" (evidence: {EVIDENCE_PATH})"
            )
        return {name: self.counts[name] for name in REQUIRED_INVARIANTS}


# A full session here collects >1000 tests. The threshold is a floor rather
# than the exact count so adding or removing tests does not break the gate,
# while a targeted run (`pytest apps/api/tests/unit/`) stays well under it
# and is refused.
MIN_PLAUSIBLE_TESTS = 500


def load_evidence(path: Path | None = None) -> GateEvidence:
    target = path or EVIDENCE_PATH
    if not target.exists():
        raise EvidenceUnavailable(
            f"no release-gate evidence at {target}; run the test suite first "
            "(the file is written at session end)"
        )
    raw = json.loads(target.read_text(encoding="utf-8"))
    invariants = raw.get("invariants")
    if not isinstance(invariants, dict):
        raise EvidenceUnavailable(f"{target} has no `invariants` mapping")

    counts: dict[str, int] = {}
    measured: dict[str, bool] = {}
    backing: dict[str, int] = {}
    not_run: dict[str, list[str]] = {}
    for name, payload in invariants.items():
        if not isinstance(payload, dict):
            continue
        counts[name] = int(payload.get("violation_count", 0))
        measured[name] = bool(payload.get("measured", False))
        backing[name] = int(payload.get("backing_tests_passed", 0))
        skipped = payload.get("not_run") or []
        not_run[name] = [str(s) for s in skipped] if isinstance(skipped, list) else []

    unmarked = [n for n in REQUIRED_INVARIANTS if n not in counts]
    if unmarked:
        raise EvidenceUnavailable(
            "the test session produced no evidence for: "
            + ", ".join(unmarked)
            + " - either the suites did not run, or their tests lost their "
            "@pytest.mark.zero_tolerance tag"
        )

    collection = raw.get("collection") or {}
    collected = int(collection.get("tests_collected", 0)) if isinstance(collection, dict) else 0
    if collected < MIN_PLAUSIBLE_TESTS:
        raise EvidenceUnavailable(
            f"evidence came from a partial run ({collected} tests collected, "
            f"expected at least {MIN_PLAUSIBLE_TESTS}). A targeted `pytest "
            "<path>` proves nothing about the suites it did not run; re-run the "
            "full suite before evaluating the release gates."
        )

    return GateEvidence(
        counts=counts,
        measured=measured,
        backing_tests=backing,
        not_run=not_run,
        tests_collected=collected,
    )


__all__ = [
    "EVIDENCE_PATH",
    "REQUIRED_INVARIANTS",
    "EvidenceUnavailable",
    "GateEvidence",
    "load_evidence",
]
