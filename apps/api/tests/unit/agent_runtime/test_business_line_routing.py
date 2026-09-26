"""Feature list 7.3: routing refines by business line, additively.

The assertion that makes this safe to deploy: with `LINE_TEAMS` unset, every
handoff goes to exactly the same team as before. A routing change that
silently moves work is the kind of change nobody notices until complaints are
sitting in a queue that does not handle them - so the fallback is pinned, not
assumed.

The rest guard the two ways this could go wrong: inventing a destination for a
scene nobody mapped (wrong queue is worse than the general one), and putting a
placeholder on every handoff ("line=unspecified") which looks like information
and is not.
"""

from __future__ import annotations

import pytest

from platform_core.agent_runtime import routing
from platform_core.agent_runtime.intent import BusinessLine, Scene
from platform_core.agent_runtime.routing import routing_note, team_for, team_for_scene


@pytest.fixture
def _clean_overrides(monkeypatch):
    """Each test starts from the shipped default: no line overrides."""
    monkeypatch.setattr(routing, "LINE_TEAMS", {}, raising=True)
    yield


def test_without_overrides_routing_is_unchanged(_clean_overrides) -> None:
    """The compatibility guard: deploying this changes no destination."""
    for scene in Scene:
        assert team_for(scene, BusinessLine.PCB) == team_for_scene(scene)
        assert team_for(scene) == team_for_scene(scene)


def test_a_configured_pair_wins(monkeypatch, _clean_overrides) -> None:
    monkeypatch.setattr(routing, "LINE_TEAMS", {("complaint", "pcb"): "quality-pcb"}, raising=True)
    assert team_for(Scene.COMPLAINT, BusinessLine.PCB) == "quality-pcb"


def test_an_unconfigured_pair_falls_back_to_the_scene_team(monkeypatch, _clean_overrides) -> None:
    """A line with no override must not lose its scene's team."""
    monkeypatch.setattr(routing, "LINE_TEAMS", {("complaint", "pcb"): "quality-pcb"}, raising=True)
    assert team_for(Scene.COMPLAINT, BusinessLine.SMT) == "quality"


def test_an_unmapped_scene_still_yields_none(_clean_overrides) -> None:
    """Guessing a queue for an unknown scene sends work to a stranger."""
    assert team_for(Scene.UNSPECIFIED) is None
    assert team_for(Scene.UNSPECIFIED, BusinessLine.PCB) is None


def test_an_override_cannot_create_a_team_for_an_unmapped_scene(
    monkeypatch, _clean_overrides
) -> None:
    monkeypatch.setattr(routing, "LINE_TEAMS", {("unspecified", "pcb"): "somewhere"}, raising=True)
    assert team_for(Scene.UNSPECIFIED, BusinessLine.PCB) == "somewhere"


def test_string_scene_and_line_are_accepted(_clean_overrides) -> None:
    """Channel metadata arrives as strings; a typo must not raise mid-reply."""
    assert team_for("complaint", "pcb") == team_for(Scene.COMPLAINT, BusinessLine.PCB)


def test_routing_note_reports_a_classified_line() -> None:
    assert routing_note(Scene.COMPLAINT, BusinessLine.PCB) == "pcb"


def test_routing_note_is_silent_for_an_unclassified_line() -> None:
    """Writing "unspecified" on every handoff is noise dressed as information."""
    assert routing_note(Scene.COMPLAINT, BusinessLine.UNSPECIFIED) is None
    assert routing_note(Scene.COMPLAINT, None) is None


def test_the_scene_table_still_covers_the_documented_scenes() -> None:
    """A shrinking table silently sends more work to the general queue."""
    for scene in (
        Scene.COMPLAINT,
        Scene.TECHNICAL_SUPPORT,
        Scene.ORDER_FULFILMENT,
        Scene.BILLING,
        Scene.ACCOUNT_SECURITY,
        Scene.PRE_SALES,
    ):
        assert team_for_scene(scene) is not None, scene
