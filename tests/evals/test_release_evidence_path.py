"""The release evidence plugin supports isolated test artifact directories."""

from pathlib import Path

from pytest_plugins_release.gate_evidence import (
    DEFAULT_EVIDENCE_DIRECTORY,
    evidence_path_for,
)


def test_release_evidence_defaults_to_the_repository_artifact_directory() -> None:
    assert evidence_path_for() == DEFAULT_EVIDENCE_DIRECTORY / "release_gate_evidence.json"


def test_release_evidence_can_be_redirected_for_an_isolated_test_run(tmp_path: Path) -> None:
    assert evidence_path_for(tmp_path) == tmp_path / "release_gate_evidence.json"
