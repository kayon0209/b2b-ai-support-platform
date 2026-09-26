"""One shape for build artifacts, and a record of what produced each one.

The problem
----------
Three test runs write JSON into `tests/artifacts/`, each in its own shape, and
the release gate reads one of them to decide whether a release may proceed. None
of them says **which run produced it** or **which code it was produced from**.

`docs/research/chinese-intent-measurement.md` already names the consequence in
one line - the gate evidence "will be overwritten by *any* pytest run". So the
file backing a release decision is whichever run happened to finish last, on
whatever branch that was, and nothing in it records that. A stale artifact from
a green run three commits ago reads exactly like a fresh one.

`release_check.py` has the same problem from the other side: it defines `-1` as
the sentinel for "this artifact does not carry this field", which is a
reasonable thing to need and is also an admission that nothing guarantees the
field is there.

What the envelope adds
----------------------
Five fields, all cheap, all of which turn "we believe this number" into "we
believe this number, from this code, at this time":

- `artifact` - the name, so a file renamed by hand is detectable.
- `run_id` - identifies the run. Not a timestamp, which two concurrent runs
  would share.
- `produced_at` - epoch seconds, for "how stale is this".
- `code_version` - what the tree looked like. A local commit hash when there is
  one, and an explicit `dirty` marker when there is not, because a hash of a
  dirty tree is a hash of nothing reproducible.
- `derived_from` - the artifacts this one was computed from.

`derived_from` is the part worth arguing about
----------------------------------------------
`release_gate_evidence.json` is evidence *about* the test run, and
`migration_report.json` is evidence about the schema. Neither is derived from
the other today. But the question "is this gate evidence consistent with the
migration report for the same run?" is exactly the question a release reviewer
asks and exactly the one that cannot currently be answered, because neither file
records a run id. The field is populated when there is a real dependency and
left empty when there is not - an empty list means "this artifact came from a
run, not from another artifact", which is a true and useful statement.

Not a database
--------------
These are files a person opens after a CI run, and a schema that requires a
migration to add a field would mean the lineage question could not be answered
until the next migration. The envelope is additive and unknown fields in an
existing file are ignored, so an older artifact still loads - as one with no
provenance, which `provenance_of` reports honestly rather than treating as
current.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ENVELOPE_KEY = "_artifact"

# Bumped when the envelope changes shape. A reader can compare it and say
# "this was written by something that did not know about lineage" instead of
# silently treating the missing fields as absent-and-therefore-fine.
ENVELOPE_VERSION = 1


@dataclass(frozen=True)
class Envelope:
    """Provenance of one artifact, plus where its payload lives."""

    artifact: str
    run_id: str
    produced_at: int
    code_version: str
    derived_from: tuple[str, ...]
    version: int
    payload: dict[str, Any]

    def age_seconds(self, *, now: int | None = None) -> int:
        return (now if now is not None else int(time.time())) - self.produced_at

    def is_from(self, code_version: str) -> bool:
        return self.code_version == code_version

    def as_dict(self) -> dict[str, Any]:
        return {
            "artifact": self.artifact,
            "run_id": self.run_id,
            "produced_at": self.produced_at,
            "code_version": self.code_version,
            "derived_from": list(self.derived_from),
            "version": self.version,
        }


def current_code_version() -> str:
    """What the working tree looks like right now.

    `dirty` is not decoration. `git rev-parse HEAD` on a dirty tree returns the
    last commit's hash, which describes code that is not the code running. A
    hash without that marker would let a gate claim provenance it does not
    have, which is the exact failure this module exists to prevent.
    """
    repo = Path(__file__).resolve().parents[4]
    try:
        head = subprocess.run(  # noqa: S603
            ["git", "rev-parse", "--short", "HEAD"],  # noqa: S607
            cwd=repo,
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        ).stdout.strip()
        dirty = (
            subprocess.run(  # noqa: S603
                ["git", "status", "--porcelain"],  # noqa: S607
                cwd=repo,
                capture_output=True,
                text=True,
                check=True,
                timeout=10,
            ).stdout.strip()
            != ""
        )
    except (OSError, subprocess.SubprocessError):
        # Not a repository, or git is unavailable. Say so rather than
        # returning a hash that looks authoritative.
        return "unknown"
    return f"{head}-dirty" if dirty else head


def _run_id() -> str:
    """One id per process, so every artifact from a run agrees.

    Derived from the PID and the start time rather than generated per write:
    two artifacts written by the same run that disagree about which run it was
    would defeat the entire purpose of the field.
    """
    return f"{os.getpid()}-{int(time.time())}-{uuid.uuid4().hex[:6]}"


def stamp(
    artifact: str,
    payload: dict[str, Any],
    *,
    run_id: str | None = None,
    derived_from: tuple[str, ...] = (),
    code_version: str | None = None,
    produced_at: int | None = None,
) -> dict[str, Any]:
    """Return `payload` with its provenance attached under `_artifact`.

    The payload is returned rather than the file being written here: these are
    produced from inside pytest, where the writer owns the path and the session
    teardown. Keeping the envelope construction separate from the write is what
    lets the migration and performance reports, which already have their own
    write helper, adopt it without changing when they write.
    """
    return {
        **payload,
        ENVELOPE_KEY: {
            "artifact": artifact,
            "run_id": run_id or _run_id(),
            "produced_at": produced_at if produced_at is not None else int(time.time()),
            "code_version": code_version or current_code_version(),
            "derived_from": list(derived_from),
            "version": ENVELOPE_VERSION,
        },
    }


def provenance_of(document: Any) -> Envelope | None:
    """Read the envelope, or None when the document predates this.

    None is the honest answer for an artifact written before lineage existed.
    Returning a synthetic "current" envelope instead would make every old file
    look attested, which is the failure this module is here to stop.
    """
    if not isinstance(document, dict):
        return None
    raw = document.get(ENVELOPE_KEY)
    if not isinstance(raw, dict):
        return None
    return Envelope(
        artifact=str(raw.get("artifact", "")),
        run_id=str(raw.get("run_id", "")),
        produced_at=int(raw.get("produced_at", 0) or 0),
        code_version=str(raw.get("code_version", "")),
        derived_from=tuple(str(x) for x in raw.get("derived_from", []) or ()),
        version=int(raw.get("version", 0) or 0),
        payload={k: v for k, v in document.items() if k != ENVELOPE_KEY},
    )


def lineage_of(document: Any) -> str:
    """A one-line description fit for a log line or a failure message.

    Used where the honest answer is often "this artifact has no provenance" -
    which is a sentence an operator can act on, where an empty field is not.
    """
    envelope = provenance_of(document)
    if envelope is None:
        return "no provenance (written before lineage existed)"
    origin = f" from {', '.join(envelope.derived_from)}" if envelope.derived_from else ""
    return f"{envelope.artifact} @ {envelope.code_version} run={envelope.run_id}{origin}"


def write(path: Path, document: dict[str, Any]) -> None:
    """Write a stamped document. Refuses rather than clobbering on a bad path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))
