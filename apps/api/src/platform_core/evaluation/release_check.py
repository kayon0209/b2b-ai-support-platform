"""Evaluate every release gate from real evidence. Exit non-zero to block.

This is the CI entry point docs/development-plan.md Phase 4 describes. It
exists so the gates have a caller: before it, `evaluate_release_gates` was
only ever invoked from tests, which means the documented release process was
a function nobody ran.

Inputs, in order of how easy they are to fake:

1. the evaluation report   - produced by the eval dataset through the real
                             retrieval -> abstention -> citation pipeline;
2. zero-tolerance counts   - read from the test session's evidence file, so
                             a suite that did not run is a blocking failure
                             rather than an assumed zero;
3. read-tool outcomes      - aggregated from `tool_executions` rows when a
                             database is reachable, otherwise reported as
                             unmeasured (which fails the gate by design).

Usage:
    python -m platform_core.evaluation.release_check            # all inputs
    python -m platform_core.evaluation.release_check --skip-db  # no telemetry

`--skip-db` still runs the read-tool gate and still fails it. It is there to
make an offline run possible and honest, not to make it pass.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import asdict
from typing import Any

from platform_core.evaluation.evidence import (
    REQUIRED_INVARIANTS,
    EvidenceUnavailable,
    load_evidence,
)
from platform_core.evaluation.gates import (
    DEFAULT_THRESHOLDS,
    GateResult,
    ReadToolOutcome,
    evaluate_release_gates,
    release_allowed,
)
from platform_core.evaluation.runner import EvalReport


def _report_from_artifact(path: str) -> EvalReport | None:
    """Load a previously produced eval report, if one is on disk."""
    import pathlib

    target = pathlib.Path(path)
    if not target.exists():
        return None
    raw = json.loads(target.read_text(encoding="utf-8"))
    return EvalReport(
        run_id=str(raw.get("run_id", "artifact")),
        started_at=int(raw.get("started_at", 0)),
        finished_at=int(raw.get("finished_at", 0)),
        total=int(raw.get("total", 0)),
        passed=int(raw.get("passed", 0)),
        failed=int(raw.get("failed", 0)),
        abstention_correct=int(raw.get("abstention_correct", 0)),
        abstention_false=int(raw.get("abstention_false", 0)),
        citation_violations=int(raw.get("citation_violations", 0)),
        forbidden_claim_hits=int(raw.get("forbidden_claim_hits", 0)),
    )


def _empty_report(reason: str) -> EvalReport:
    """A report that fails every quality floor rather than passing vacuously.

    `total=0` would make `citation_rate = violations / max(total, 1) = 0` and
    read as perfect coverage. So an unrun evaluation is represented as one
    that answered nothing and violated nothing - which the thresholds only
    catch if the caller knows. `main()` refuses to run without a real report
    unless `--allow-missing-eval` says the operator understands the hole.
    """
    print(f"WARNING: no evaluation report available ({reason})", file=sys.stderr)
    return EvalReport(
        run_id="absent",
        started_at=0,
        finished_at=0,
        total=0,
        passed=0,
        failed=0,
        abstention_correct=0,
        abstention_false=0,
        citation_violations=0,
        forbidden_claim_hits=0,
    )


async def _read_tool_outcomes(tenant_id: str | None) -> ReadToolOutcome | None:
    from platform_core.config import get_settings
    from platform_core.db import session_scope_with_url

    settings = get_settings()
    url = settings.database_url.replace("platform:platform@", "platform_app:platform_app@")
    if tenant_id is None:
        return None
    import uuid as _uuid

    from platform_core.evaluation.metrics import aggregate_read_tool_outcomes

    async with session_scope_with_url(url) as session:
        return await aggregate_read_tool_outcomes(
            session, tenant_id=_uuid.UUID(tenant_id), window_seconds=7 * 24 * 3600
        )


def _run_async(coro: Any) -> Any:
    """Run a DB-touching coroutine on a loop psycopg accepts.

    A bare `asyncio.run` selects `ProactorEventLoop` on Windows, and psycopg's
    async driver refuses it outright - so the read-tool gate could never be
    measured on a Windows checkout, and the operator would see
    "no read-tool telemetry supplied" for a reason that has nothing to do
    with the telemetry. `db.ensure_async_db_loop` turns that into a loud
    error; this is the fix at the call site.

    `SelectorEventLoop` is also the default on Linux/macOS, so selecting it
    unconditionally keeps one code path rather than a platform branch that
    only ever gets exercised on one platform.
    """
    return asyncio.run(coro, loop_factory=asyncio.SelectorEventLoop)


def _evaluate_p0_gate(path: str | None) -> GateResult | None:
    """Fold the prompt-release P0 check into this report.

    The check itself is `prompt_release.check_release_gate`; this adapter
    turns its pass/raise into a `GateResult` so it appears in the same table
    as everything else. Returns None when no candidate is being promoted, so
    a plain platform release is not blocked on evidence for a change it does
    not contain.
    """
    if not path:
        return None
    import json as _json
    import pathlib

    from platform_core.agent_runtime.prompt_release import (
        CategoryScore,
        EvaluationEvidence,
        Regression,
        ReleaseError,
        check_release_gate,
    )

    target = pathlib.Path(path)
    if not target.exists():
        return GateResult(
            gate="p0_no_regression",
            passed=False,
            observed=None,
            threshold="no P0 category regressions",
            detail=f"candidate evidence not found at {path}",
        )
    raw = _json.loads(target.read_text(encoding="utf-8"))
    evidence = EvaluationEvidence(
        eval_run_id=str(raw.get("eval_run_id", "unknown")),
        scores=[
            CategoryScore(
                category=str(s["category"]),
                passed=int(s.get("passed", 0)),
                total=int(s.get("total", 0)),
            )
            for s in raw.get("scores", [])
        ],
        regressions=[
            Regression(
                category=str(r["category"]),
                baseline_rate=float(r.get("baseline_rate", 0.0)),
                candidate_rate=float(r.get("candidate_rate", 0.0)),
                p0=bool(r.get("p0", False)),
            )
            for r in raw.get("regressions", [])
        ],
    )
    try:
        check_release_gate(evidence)
    except ReleaseError as exc:
        return GateResult(
            gate="p0_no_regression",
            passed=False,
            observed=exc.code,
            threshold="no P0 category regressions",
            detail=exc.detail,
        )
    return GateResult(
        gate="p0_no_regression",
        passed=True,
        observed=f"{len(evidence.scores)} categories",
        threshold="no P0 category regressions",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Evaluate release gates.")
    parser.add_argument("--report", default="tests/artifacts/eval_report.json")
    parser.add_argument("--tenant-id", default=None)
    parser.add_argument(
        "--p0-evidence",
        default=None,
        help="JSON with {eval_run_id, scores[], regressions[]} for the candidate "
        "prompt; enables the P0 no-regression gate",
    )
    parser.add_argument(
        "--skip-db",
        action="store_true",
        help="do not read read-tool telemetry; the gate will fail, by design",
    )
    parser.add_argument(
        "--allow-missing-eval",
        action="store_true",
        help="proceed without an evaluation report (quality gates will fail)",
    )
    parser.add_argument(
        "--evidence-only",
        action="store_true",
        help="verify the zero-tolerance evidence is complete and usable, then "
        "exit; the CI check for 'a backing suite stopped running'",
    )
    parser.add_argument("--json", action="store_true", help="emit machine-readable output")
    args = parser.parse_args(argv)

    try:
        evidence = load_evidence()
        security = evidence.as_security_kwargs()
    except EvidenceUnavailable as exc:
        print(f"FATAL: release-gate evidence unusable: {exc}", file=sys.stderr)
        return 2

    if args.evidence_only:
        # The one thing CI can prove about a release without production
        # telemetry: every invariant still has a passing backing suite, and
        # the session that produced it was a full run. It deliberately does
        # not claim the release is allowed - the eval report and the read-tool
        # telemetry are release-time inputs that CI cannot have.
        print(f"evidence usable; {evidence.tests_collected} tests collected")
        for name in REQUIRED_INVARIANTS:
            print(f"  {name}: {evidence.backing_tests.get(name, 0)} passing test(s)")
        return 0

    report = _report_from_artifact(args.report)
    if report is None:
        if not args.allow_missing_eval:
            print(
                f"FATAL: no evaluation report at {args.report}. That file is a "
                "release-time artifact: it is produced by running the evaluation "
                "dataset against a real tenant corpus and the live model, which "
                "CI cannot do. Pass --allow-missing-eval to see the full gate "
                "picture knowing the quality gates will fail, or --evidence-only "
                "to check just the zero-tolerance evidence.",
                file=sys.stderr,
            )
            return 2
        report = _empty_report(f"{args.report} not found")

    read_tools: ReadToolOutcome | None = None
    if not args.skip_db:
        try:
            read_tools = _run_async(_read_tool_outcomes(args.tenant_id))
        except Exception as exc:  # noqa: BLE001 - reported, then treated as absent
            print(f"WARNING: could not read tool telemetry: {exc}", file=sys.stderr)

    gates = evaluate_release_gates(
        report, security=security, read_tools=read_tools, thresholds=DEFAULT_THRESHOLDS
    )
    allowed = release_allowed(gates)

    # The P0 category gate lives in the prompt-release module because it
    # guards a different action (promoting a prompt version). Running it
    # here too is what makes this command the single release decision: a
    # candidate that regresses a P0 category fails the release even when all
    # six platform gates pass.
    p0_gate = _evaluate_p0_gate(args.p0_evidence)
    if p0_gate is not None:
        gates = [*gates, p0_gate]
        allowed = allowed and p0_gate.passed

    if args.json:
        print(
            json.dumps(
                {
                    "release_allowed": allowed,
                    "gates": [asdict(g) for g in gates],
                    "security_counts": security,
                    "backing_tests": evidence.backing_tests,
                },
                indent=2,
                sort_keys=True,
            )
        )
    else:
        print(f"release_allowed: {allowed}\n")
        for gate in gates:
            mark = "PASS" if gate.passed else "FAIL"
            detail = f"  ({gate.detail})" if gate.detail else ""
            print(f"  [{mark}] {gate.gate}: {gate.observed} vs {gate.threshold}{detail}")
        print("\nzero-tolerance counts back to:")
        for name, n in sorted(evidence.backing_tests.items()):
            print(f"  {name}: {n} passing test(s)")

    return 0 if allowed else 1


if __name__ == "__main__":
    raise SystemExit(main())
