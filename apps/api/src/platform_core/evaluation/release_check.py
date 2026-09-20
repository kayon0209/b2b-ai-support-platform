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
from dataclasses import asdict, dataclass, field
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
        contradiction_candidates=int(raw.get("contradiction_candidates", 0)),
        # ADR 0009. These three are NOT defaulted like the others, and the
        # difference matters. `raw.get(field, 0)` made an artifact that predates
        # the field indistinguishable from one where the count really was zero -
        # so `cross_lingual_exclusions_match` compared `0 == 0` and passed
        # without having measured anything. Measured on the pre-ADR artifact
        # still on disk: `observed=0 threshold=0`, gate green. That is exactly
        # the "exemption with no live consumer" failure ADR 0009 exists to
        # prevent, reproduced inside the gate meant to prevent it.
        #
        # `-1` is the sentinel for "the artifact does not carry this field", and
        # `gates.py` refuses to pass the match gate on it. An artifact that
        # predates ADR 0009 must be regenerated, not silently accepted.
        cross_lingual_unreachable=int(raw.get("cross_lingual_unreachable", -1)),
        declared_cross_lingual=int(raw.get("declared_cross_lingual", -1)),
        exemptible_cross_lingual=int(raw.get("exemptible_cross_lingual", -1)),
    )


# How many passes over the case set a release decision should rest on. The
# model is stochastic: measured, the adversarial cases fail roughly one run in
# three, so a single run cannot tell a regression from a bad sample.
MIN_TRUSTED_SAMPLES = 3


@dataclass(frozen=True)
class ReportConfidence:
    """Whether a report's result is stable enough to act on.

    ADR 0005's finding: `forbidden_claim_rate` has a threshold of 0.02 over 23
    cases, so one hit fails the gate - and a hit occurs about a quarter of the
    time. Acting on one sample is a coin toss with a threshold. This makes the
    sampling explicit instead of leaving an operator to rediscover it from a
    flaky red build.
    """

    samples: int = 1
    flaky_cases: dict[str, int] = field(default_factory=dict)

    @property
    def trusted(self) -> bool:
        return self.samples >= MIN_TRUSTED_SAMPLES

    def as_dict(self) -> dict[str, object]:
        return {
            "samples": self.samples,
            "min_trusted_samples": MIN_TRUSTED_SAMPLES,
            "trusted": self.trusted,
            "flaky_cases": dict(self.flaky_cases),
        }


def _confidence_from_artifact(path: str) -> ReportConfidence:
    """Read the sampling metadata `run_eval.py --samples N` records."""
    import pathlib

    target = pathlib.Path(path)
    if not target.exists():
        return ReportConfidence()
    raw = json.loads(target.read_text(encoding="utf-8"))
    provenance = raw.get("provenance")
    if not isinstance(provenance, dict):
        return ReportConfidence()
    samples = provenance.get("samples", 1)
    flaky = provenance.get("flaky_cases", {})
    if not isinstance(samples, int):
        samples = 1
    if not isinstance(flaky, dict):
        flaky = {}
    return ReportConfidence(
        samples=max(1, samples),
        flaky_cases={str(k): int(v) for k, v in flaky.items()},
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
    """Read-tool success for one tenant, from real executions.

    **The tenant must be bound before the query.** `tool_executions` is FORCE
    RLS, so an unbound `platform_app` read returns zero rows with no error -
    measured, with a row committed in the same transaction: `unbound_rows=0`,
    `bound_rows=1`. Without the binding this function could never observe any
    telemetry, so the read-tool gate failed on every run for a reason that had
    nothing to do with read tools.

    The function it calls was never the problem: its integration tests bind the
    tenant themselves, so it is tested under conditions this caller never
    provided. That gap is the whole defect - the test proved the aggregation
    works, not that anything reaches it.
    """
    import uuid as _uuid

    from platform_core.db import app_role_url, session_scope_with_url
    from platform_core.evaluation.metrics import aggregate_read_tool_outcomes
    from platform_core.identity.tenant_context import TenantContext, apply_rls_tenant

    if tenant_id is None:
        return None
    tid = _uuid.UUID(tenant_id)

    async with session_scope_with_url(app_role_url()) as session:
        await apply_rls_tenant(
            session, TenantContext(tenant_id=tid, actor_id=None, actor_kind="system")
        )
        return await aggregate_read_tool_outcomes(
            session, tenant_id=tid, window_seconds=7 * 24 * 3600
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

    # Whether this result is stable enough to act on at all. A failing gate on
    # a single sample is not a verdict: the model is stochastic, and measured
    # over repeated runs the adversarial cases fail about one run in three.
    confidence = _confidence_from_artifact(args.report)

    if args.json:
        print(
            json.dumps(
                {
                    "release_allowed": allowed,
                    "gates": [asdict(g) for g in gates],
                    "security_counts": security,
                    "backing_tests": evidence.backing_tests,
                    "confidence": confidence.as_dict(),
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

        if not confidence.trusted and not allowed:
            _print_low_confidence(confidence, args.report)

    return 0 if allowed else 1


def _print_low_confidence(confidence: ReportConfidence, report_path: str) -> None:
    """Say plainly that a failing gate may be sampling noise.

    Without this an operator sees a red build and has no way to know whether
    to investigate or re-run. ADR 0005 asks for exactly this: "a gate that
    needs N runs to be stable should say so in the release process rather than
    be discovered by a flaky red build."
    """
    print(
        f"\nWARNING: this report comes from {confidence.samples} sample(s), below "
        f"the {MIN_TRUSTED_SAMPLES} needed to trust a failing result.",
        file=sys.stderr,
    )
    print(
        "The model is stochastic; measured, the adversarial cases fail about one "
        "run in three. Re-run with `run_eval.py --samples "
        f"{MIN_TRUSTED_SAMPLES}` before treating this as a regression."
        if confidence.samples == 1
        else "Re-run with a higher --samples count before treating this as a regression.",
        file=sys.stderr,
    )
    if confidence.flaky_cases:
        print("\nknown-flaky cases in this report:", file=sys.stderr)
        for case_id, failures in sorted(confidence.flaky_cases.items(), key=lambda kv: -kv[1]):
            print(
                f"  {case_id}: failed {failures} of {confidence.samples}",
                file=sys.stderr,
            )
    else:
        print(
            f"(no flakiness was measurable - {report_path} recorded a single "
            "run, so none could be.)",
            file=sys.stderr,
        )


if __name__ == "__main__":
    raise SystemExit(main())
