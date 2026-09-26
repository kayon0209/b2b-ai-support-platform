"""An artifact that cannot say where it came from cannot back a release.

The problem
-----------
Three runs write JSON into `tests/artifacts/` and the release gate reads one of
them to decide whether a release may proceed. None of the three recorded which
run produced it or which code it was produced from, and
`docs/research/chinese-intent-measurement.md` already states the consequence:
the gate evidence "will be overwritten by *any* pytest run".

So the file behind a release decision is whichever run finished last, on
whatever branch that was, and a stale artifact from a green run three commits
ago is indistinguishable from a fresh one. `release_check.py` defines `-1` as
the sentinel for "this artifact does not carry this field", which is an
admission of the same gap from the reading side.

What is asserted
----------------
- Stamping round-trips and leaves the payload readable underneath.
- An artifact from a different `code_version` is detectable, which is the whole
  point: a green artifact from before a security fix must not vouch for the
  code after it.
- A document written before lineage existed reports **no provenance** rather
  than a synthetic one. Returning a fabricated "current" envelope would make
  every legacy file look attested.
- Two artifacts from one run agree on the run id, and the derived-from
  relationship survives the round trip.
"""

from __future__ import annotations

import time

from platform_core.evaluation.artifacts import (
    ENVELOPE_KEY,
    Envelope,
    lineage_of,
    provenance_of,
    stamp,
)


def _stamped(**kwargs: object) -> dict:
    """A stamped gate-evidence document, with per-test overrides.

    `artifact` is popped rather than passed through: it is the first positional
    argument of `stamp`, so leaving it in `kwargs` passes it twice.
    """
    name = str(kwargs.pop("artifact", "release_gate_evidence"))
    base: dict = {
        "run_id": "run-1",
        "code_version": "abc1234",
        "produced_at": 1_700_000_000,
    }
    base.update(kwargs)
    return stamp(name, {"counts": {"x": 1}}, **base)  # type: ignore[arg-type]


def test_stamping_leaves_the_payload_readable() -> None:
    """The envelope is additive and keyed, not a wrapper.

    A consumer written before this change reads `document["counts"]` and must
    keep working. Renaming the payload under a new top-level key would have
    broken `evidence.load_evidence` and every artifact reader at once.
    """
    document = _stamped()

    assert document["counts"] == {"x": 1}
    envelope = provenance_of(document)
    assert envelope is not None
    assert envelope.payload == {"counts": {"x": 1}}, (
        "the payload must not leak the envelope key back into itself"
    )


def test_an_artifact_from_other_code_is_detectable() -> None:
    """The property the whole module exists for.

    A green artifact produced before a security fix cannot vouch for the code
    after it. Before this, the two were the same file shape and nothing in it
    carried the code it was produced from.
    """
    stale = provenance_of(_stamped(code_version="abc1234"))

    assert stale is not None
    assert stale.is_from("abc1234") is True
    assert stale.is_from("def5678") is False, (
        "a stale artifact cannot be distinguished from a current one"
    )


def test_a_document_without_lineage_reports_none_rather_than_a_stub() -> None:
    """Legacy artifacts exist, and inventing provenance for them is the failure.

    A synthetic envelope with an empty `code_version` would compare unequal to
    everything and be *safe*. One filled in with the current version would
    compare equal and be a lie. This asserts the first and refuses the second.
    """
    legacy = {"counts": {"x": 1}, "measured": {"m": True}}

    assert provenance_of(legacy) is None
    assert "no provenance" in lineage_of(legacy)


def test_provenance_of_refuses_a_non_dict_document() -> None:
    """The reader is defensive about shape.

    An artifact truncated by a killed CI run parses as something other than a
    dict, and `provenance_of` is called on whatever was on disk.
    """
    for bad in (None, [], "text", 3):
        assert provenance_of(bad) is None, bad


def test_two_artifacts_from_one_run_agree_and_keep_their_lineage() -> None:
    """`derived_from` only answers a question if the run id agrees.

    A performance report claiming to derive from the gate evidence is only
    meaningful if both were produced by the same run; otherwise it is a claim
    about two unrelated sessions. Asserted together for that reason - either
    field alone would pass while the pair says nothing.
    """
    gate = _stamped(run_id="run-7")
    perf = _stamped(
        artifact="performance_report",
        run_id="run-7",
        derived_from=("release_gate_evidence",),
    )

    gate_env, perf_env = provenance_of(gate), provenance_of(perf)
    assert isinstance(gate_env, Envelope) and isinstance(perf_env, Envelope)
    assert gate_env.run_id == perf_env.run_id, "same run must produce one run id"
    assert perf_env.derived_from == ("release_gate_evidence",)
    assert "from release_gate_evidence" in lineage_of(perf)


def test_lineage_is_readable_as_a_sentence() -> None:
    """Used in log lines and failure messages, so it has to say something.

    An empty field is not an explanation; "no provenance (written before lineage
    existed)" is a sentence an operator can act on.
    """
    described = lineage_of(_stamped())
    assert "release_gate_evidence" in described
    assert "abc1234" in described
    assert "run-1" in described


def test_the_envelope_version_is_carried_so_a_shape_change_is_detectable() -> None:
    """A future change to the envelope has to be distinguishable from an old file.

    Otherwise a reader meets a field it does not understand and cannot tell
    whether the writer knew about lineage at all.
    """
    envelope = provenance_of(_stamped())
    assert envelope is not None
    assert envelope.version >= 1
    assert ENVELOPE_KEY in _stamped()


def test_age_is_measured_from_the_stamp_not_the_file_mtime() -> None:
    """A copied or checked-out artifact keeps its mtime; the stamp travels.

    Reading the stamp is what makes "three days old" survive being moved into an
    artifact bucket, which is where these files end up.
    """
    envelope = provenance_of(_stamped(produced_at=int(time.time()) - 3600))
    assert envelope is not None
    assert 3500 <= envelope.age_seconds() <= 3700
