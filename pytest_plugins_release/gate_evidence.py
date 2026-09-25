"""Release-gate evidence collection.

`docs/development-plan.md` Phase 4 gates a release on six numbers. Three of
them are zero-tolerance counts that no single test can produce:

    cross-tenant leakage: 0
    unauthorized high-risk action: 0
    duplicate customer reply: 0

The naive approach is to write `security={"cross_tenant_violations": 0}`
into whatever calls the gate. That is a hand-typed zero: it asserts nothing,
and it cannot go red when the suites that would have caught a leak stop
running. It is a fact about the person filing the release, not about the
system.

So the counts are derived instead. A test that exists to prove one of these
invariants declares which one it backs:

    @pytest.mark.zero_tolerance("cross_tenant_violations")
    def test_direct_id_access_does_not_leak_across_tenants() -> None:
        ...

When the session ends this plugin writes the observed counts to
`tests/artifacts/release_gate_evidence.json`. The gate reads that file. The
consequences are the point:

- a failing zero-tolerance test makes the count non-zero, so the gate fails;
- a *skipped* or *deselected* zero-tolerance test is reported as absent, not
  as zero, so "we did not run the leak suite" cannot pass as "no leak";
- adding a new invariant requires tagging the test, which is a reviewable
  line in the diff.
"""

from __future__ import annotations

import json
import os
import time
from collections import defaultdict
from pathlib import Path

import pytest

EVIDENCE_PATH = (
    Path(__file__).resolve().parents[1] / "tests" / "artifacts" / "release_gate_evidence.json"
)
MARKER = "zero_tolerance"

# One id per session. Generated per *write* instead, two artifacts from the
# same run would disagree about which run it was, which defeats the field.
_RUN_ID = f"{os.getpid()}-{int(time.time())}"


class ZeroToleranceCollector:
    """Tracks, per invariant, how many backing tests passed or did not."""

    def __init__(self) -> None:
        # invariant -> {"passed": n, "failed": [node ids], "skipped": [node ids]}
        self._by_invariant: dict[str, dict[str, object]] = defaultdict(
            lambda: {"passed": 0, "failed": [], "skipped": []}
        )
        self._untagged_reason: str | None = None

    def record(self, invariant: str, nodeid: str, outcome: str) -> None:
        bucket = self._by_invariant[invariant]
        if outcome == "passed":
            bucket["passed"] = int(bucket["passed"]) + 1  # type: ignore[arg-type]
        elif outcome == "skipped":
            bucket["skipped"].append(nodeid)  # type: ignore[attr-defined]
        else:
            bucket["failed"].append(nodeid)  # type: ignore[attr-defined]

    def snapshot(self) -> dict[str, object]:
        invariants: dict[str, object] = {}
        for name, bucket in sorted(self._by_invariant.items()):
            failed = bucket["failed"]
            skipped = bucket["skipped"]
            passed = int(bucket["passed"])  # type: ignore[arg-type]
            assert isinstance(failed, list) and isinstance(skipped, list)
            # A violation is counted as 1 per test that did not pass while
            # claiming to prove the invariant. The gate compares against 0,
            # so any value here is a red gate; the count is kept >0 rather
            # than collapsed to a boolean so the failure output can name how
            # many suites are implicated.
            violation_count = len(failed) + len(skipped)
            invariants[name] = {
                "backing_tests_passed": passed,
                "violation_count": violation_count,
                "failed": failed,
                "not_run": skipped,
                "measured": passed > 0 and violation_count == 0,
            }
        return {
            "invariants": invariants,
            "collector_note": self._untagged_reason,
        }


_collector = ZeroToleranceCollector()
_collected_tests = 0


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    global _collected_tests
    _collected_tests = len(items)


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "zero_tolerance(name): test backs a zero-tolerance release gate count",
    )
    # Evidence from a previous run must never be mistaken for this run's,
    # because the gate reads the file rather than the pytest exit code.
    # Deleting is safe here: it lives under tests/artifacts/ and is rebuilt
    # by every session.
    if EVIDENCE_PATH.exists():
        try:
            EVIDENCE_PATH.unlink()
        except OSError:
            pass


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[object]):
    report = yield
    if report.when != "call" and not (report.when == "setup" and report.skipped):
        return report

    for marker in item.iter_markers(name=MARKER):
        invariant = marker.args[0] if marker.args else marker.kwargs.get("name")
        if not isinstance(invariant, str):
            _collector._untagged_reason = f"{item.nodeid}: marker needs a name argument"
            continue
        if report.skipped:
            _collector.record(invariant, item.nodeid, "skipped")
        elif report.failed:
            _collector.record(invariant, item.nodeid, "failed")
        elif report.when == "call" and report.passed:
            _collector.record(invariant, item.nodeid, "passed")
    return report


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    snapshot = _collector.snapshot()
    snapshot["exitstatus"] = int(exitstatus)
    # Record how much of the suite ran. A targeted run (`pytest path/to/one
    # /test.py`) writes real but partial evidence, and a release gate that
    # read it would conclude "no cross-tenant violations" from a session
    # that never ran the cross-tenant suite. The reader checks these numbers
    # so a partial run cannot be mistaken for a full one.
    snapshot["collection"] = {
        "tests_collected": _collected_tests,
        "invariants_covered": len(snapshot["invariants"]),  # type: ignore[arg-type]
    }
    # Stamped with provenance. `docs/research/chinese-intent-measurement.md`
    # already records that this file is overwritten by *any* pytest run, which
    # means a release gate can be reading whichever run finished last, on
    # whatever branch that was, with nothing in the file to say so. The
    # envelope turns "we believe this number" into "we believe this number,
    # from this code, in this run".
    from platform_core.evaluation.artifacts import stamp

    document = stamp("release_gate_evidence", snapshot, run_id=_RUN_ID)
    EVIDENCE_PATH.parent.mkdir(parents=True, exist_ok=True)
    EVIDENCE_PATH.write_text(json.dumps(document, indent=2, sort_keys=True), encoding="utf-8")
    if os.environ.get("RELEASE_GATE_EVIDENCE_VERBOSE"):
        print(f"\nrelease-gate evidence written to {EVIDENCE_PATH}")
