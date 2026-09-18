"""Release-gate evidence, and the CLI that consumes it.

`tests/evals/test_release_gates.py` covers the gate *logic*. These two
modules are the half that feeds it, and until now neither had a test:

- `evaluation/evidence.py` exists to refuse missing evidence instead of
  degrading it to zeros;
- `evaluation/release_check.py` is the documented CI entry point.

The repo's own rule applies here more than anywhere: *a guard that cannot be
observed failing is not evidence*. `load_evidence` is a guard, so each way it
can refuse is pinned below - and the specific historical failure it was built
against (a hand-typed `{"cross_tenant_violations": 0}`) is asserted to be
impossible to express.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from platform_core.evaluation import release_check
from platform_core.evaluation.evidence import (
    MIN_PLAUSIBLE_TESTS,
    REQUIRED_INVARIANTS,
    EvidenceUnavailable,
    load_evidence,
)
from platform_core.evaluation.gates import ReadToolOutcome

# --- Building evidence files ---


def _evidence_doc(
    *,
    invariants: dict[str, dict[str, object]] | None = None,
    collected: int = MIN_PLAUSIBLE_TESTS + 100,
) -> dict[str, object]:
    """A well-formed evidence document, unless a test overrides a part."""
    if invariants is None:
        invariants = {
            name: {
                "backing_tests_passed": 3,
                "violation_count": 0,
                "failed": [],
                "not_run": [],
                "measured": True,
            }
            for name in REQUIRED_INVARIANTS
        }
    return {
        "invariants": invariants,
        "collector_note": None,
        "collection": {
            "tests_collected": collected,
            "invariants_covered": len(invariants),
        },
    }


def _write(tmp_path: Path, doc: dict[str, object]) -> Path:
    target = tmp_path / "release_gate_evidence.json"
    target.write_text(json.dumps(doc), encoding="utf-8")
    return target


# --- evidence.py: every refusal is a real refusal ---


def test_a_missing_file_is_refused_not_treated_as_zero_violations(tmp_path: Path) -> None:
    """The whole reason this module exists.

    Returning `{name: 0 for name in REQUIRED_INVARIANTS}` for an absent file
    would be the most dangerous possible default: a green gate produced by
    the evidence being unavailable.
    """
    with pytest.raises(EvidenceUnavailable) as excinfo:
        load_evidence(tmp_path / "nope.json")
    assert "run the test suite first" in str(excinfo.value)


def test_an_invariant_with_no_backing_suite_is_refused(tmp_path: Path) -> None:
    """Dropping a suite must not read as "that invariant is clean"."""
    doc = _evidence_doc()
    invariants = dict(doc["invariants"])  # type: ignore[arg-type]
    del invariants["duplicate_replies"]
    path = _write(tmp_path, _evidence_doc(invariants=invariants))

    with pytest.raises(EvidenceUnavailable) as excinfo:
        load_evidence(path)
    assert "duplicate_replies" in str(excinfo.value)


def test_a_partial_run_is_refused(tmp_path: Path) -> None:
    """A targeted `pytest path/to/one_test.py` writes real but partial
    evidence. Reading it would conclude "no cross-tenant violations" from a
    session that never ran the cross-tenant suite.
    """
    path = _write(tmp_path, _evidence_doc(collected=12))
    with pytest.raises(EvidenceUnavailable) as excinfo:
        load_evidence(path)
    assert "partial run" in str(excinfo.value)


def test_a_skipped_backing_test_is_not_measured(tmp_path: Path) -> None:
    """`measured` is what `as_security_kwargs` trusts, so a skip must clear
    it even when the invariant is otherwise present."""
    invariants = _evidence_doc()["invariants"]
    assert isinstance(invariants, dict)
    invariants["unauthorized_writes"] = {
        "backing_tests_passed": 0,
        "violation_count": 2,
        "failed": [],
        "not_run": ["test_a", "test_b"],
        "measured": False,
    }
    path = _write(tmp_path, _evidence_doc(invariants=invariants))

    evidence = load_evidence(path)
    assert evidence.counts["unauthorized_writes"] == 2
    assert evidence.not_run["unauthorized_writes"] == ["test_a", "test_b"]
    with pytest.raises(EvidenceUnavailable) as excinfo:
        evidence.as_security_kwargs()
    assert "unauthorized_writes" in str(excinfo.value)


def test_complete_evidence_yields_the_three_counts(tmp_path: Path) -> None:
    path = _write(tmp_path, _evidence_doc())
    evidence = load_evidence(path)
    assert evidence.as_security_kwargs() == dict.fromkeys(REQUIRED_INVARIANTS, 0)
    assert evidence.backing_tests == dict.fromkeys(REQUIRED_INVARIANTS, 3)
    assert evidence.tests_collected >= MIN_PLAUSIBLE_TESTS


def test_a_hand_typed_zero_cannot_be_expressed() -> None:
    """The failure this replaced, stated as a test.

    `as_security_kwargs()` takes no arguments and reads only measured
    invariants, so there is no way to call the gate with a dict the caller
    assembled from memory.
    """
    import inspect

    from platform_core.evaluation.evidence import GateEvidence

    assert set(inspect.signature(load_evidence).parameters) <= {"path"}, (
        "load_evidence must not accept counts"
    )
    assert list(inspect.signature(GateEvidence.as_security_kwargs).parameters) == ["self"], (
        "as_security_kwargs must derive its dict, not accept one"
    )


# --- release_check.py: the CLI ---


def _no_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(*_args: object, **_kwargs: object) -> object:
        raise EvidenceUnavailable("no release-gate evidence at all")

    monkeypatch.setattr(release_check, "load_evidence", _raise)


def test_the_cli_refuses_when_evidence_is_unusable(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Exit 2, distinct from 1. "Could not evaluate" and "evaluated and
    failed" are different operator actions: one is re-run the suite, the
    other is fix the regression."""
    _no_evidence(monkeypatch)
    code = release_check.main(["--allow-missing-eval"])
    assert code == 2
    assert "evidence unusable" in capsys.readouterr().err


def test_evidence_only_mode_does_not_require_the_eval_report(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The CI check. It must not be blocked on the eval report or the
    read-tool telemetry - neither exists in CI, and demanding them would
    make the one check CI *can* perform permanently red."""
    import platform_core.evaluation.evidence as evidence_module

    monkeypatch.setattr(
        release_check,
        "load_evidence",
        lambda: evidence_module.GateEvidence(
            counts=dict.fromkeys(REQUIRED_INVARIANTS, 0),
            measured=dict.fromkeys(REQUIRED_INVARIANTS, True),
            backing_tests=dict.fromkeys(REQUIRED_INVARIANTS, 7),
            not_run={},
            tests_collected=MIN_PLAUSIBLE_TESTS + 1,
        ),
    )
    code = release_check.main(["--evidence-only"])
    out = capsys.readouterr().out
    assert code == 0
    assert "evidence usable" in out
    for name in REQUIRED_INVARIANTS:
        assert f"{name}: 7 passing test(s)" in out


def test_evidence_only_mode_fails_when_a_backing_suite_stopped_running(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The regression this mode exists to catch: a zero-tolerance suite
    dropped from collection, so its invariant has no evidence."""
    _no_evidence(monkeypatch)
    code = release_check.main(["--evidence-only"])
    assert code == 2
    assert "evidence unusable" in capsys.readouterr().err


def test_the_cli_fails_the_read_tool_gate_without_telemetry(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`--skip-db` makes an offline run possible and honest, not passing."""
    import platform_core.evaluation.evidence as evidence_module

    monkeypatch.setattr(
        release_check,
        "load_evidence",
        lambda: evidence_module.GateEvidence(
            counts=dict.fromkeys(REQUIRED_INVARIANTS, 0),
            measured=dict.fromkeys(REQUIRED_INVARIANTS, True),
            backing_tests=dict.fromkeys(REQUIRED_INVARIANTS, 1),
            not_run={},
            tests_collected=MIN_PLAUSIBLE_TESTS + 1,
        ),
    )
    code = release_check.main(["--allow-missing-eval", "--skip-db"])
    out = capsys.readouterr().out
    assert code == 1
    assert "release_allowed: False" in out
    assert "[FAIL] read_tool_success_rate" in out


def test_the_cli_passes_once_every_gate_has_evidence(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """The positive path, so the suite proves the CLI *can* go green - a
    command that can only ever fail is not a gate, it is a wall.

    `--report` points at a path that does not exist on purpose. Without it the
    run picks up whatever `tests/artifacts/eval_report.json` happens to hold,
    and a developer who had just run `scripts/run_eval.py` against a pipeline
    with real quality failures would see this test fail for a reason that has
    nothing to do with the CLI.
    """
    import platform_core.evaluation.evidence as evidence_module

    monkeypatch.setattr(
        release_check,
        "load_evidence",
        lambda: evidence_module.GateEvidence(
            counts=dict.fromkeys(REQUIRED_INVARIANTS, 0),
            measured=dict.fromkeys(REQUIRED_INVARIANTS, True),
            backing_tests=dict.fromkeys(REQUIRED_INVARIANTS, 1),
            not_run={},
            tests_collected=MIN_PLAUSIBLE_TESTS + 1,
        ),
    )

    async def _healthy(_tenant: str | None) -> ReadToolOutcome:
        return ReadToolOutcome(succeeded=100, failed=0, third_party_failures=0)

    monkeypatch.setattr(release_check, "_read_tool_outcomes", _healthy)
    code = release_check.main(
        [
            "--allow-missing-eval",
            "--report",
            str(tmp_path / "absent.json"),
            "--tenant-id",
            "t",
        ]
    )
    out = capsys.readouterr().out
    assert code == 0, out
    assert "release_allowed: True" in out


def test_the_db_read_runs_on_a_loop_psycopg_accepts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression guard for a real defect.

    `main()` used a bare `asyncio.run`, which selects `ProactorEventLoop` on
    Windows; psycopg's async driver refuses it, so read-tool telemetry could
    never be read on a Windows checkout and the gate reported "no telemetry"
    for a reason that had nothing to do with telemetry.

    The assertion is `isinstance(..., SelectorEventLoop)` rather than a class
    name: on Windows `asyncio.SelectorEventLoop` *is* `_WindowsSelectorEventLoop`,
    so a name comparison would be asserting the wrong thing on the one
    platform the bug affects. `isinstance` is also exactly what psycopg
    checks.
    """
    import platform_core.evaluation.evidence as evidence_module

    monkeypatch.setattr(
        release_check,
        "load_evidence",
        lambda: evidence_module.GateEvidence(
            counts=dict.fromkeys(REQUIRED_INVARIANTS, 0),
            measured=dict.fromkeys(REQUIRED_INVARIANTS, True),
            backing_tests=dict.fromkeys(REQUIRED_INVARIANTS, 1),
            not_run={},
            tests_collected=MIN_PLAUSIBLE_TESTS + 1,
        ),
    )

    seen: list[bool] = []

    async def _record_loop(_tenant: str | None) -> ReadToolOutcome:
        loop = asyncio.get_running_loop()
        seen.append(isinstance(loop, asyncio.SelectorEventLoop))
        return ReadToolOutcome(succeeded=1, failed=0, third_party_failures=0)

    monkeypatch.setattr(release_check, "_read_tool_outcomes", _record_loop)
    release_check.main(["--allow-missing-eval", "--tenant-id", "t"])

    assert seen == [True], "the DB read must not run on a Proactor loop; psycopg rejects it"
