"""Team routing: "transfer to a human" is not a destination.

Both halves are tested. Mapping to the wrong team sends work somewhere
specific and wrong, which is worse than a general queue; and guessing a team
for an unmapped scene would do the same on the strength of a guess.
"""

from platform_core.agent_runtime.intent import Scene
from platform_core.agent_runtime.routing import SCENE_TEAMS, team_for_scene


def test_each_mapped_scene_has_a_team() -> None:
    for scene in (
        Scene.COMPLAINT,
        Scene.TECHNICAL_SUPPORT,
        Scene.ORDER_FULFILMENT,
        Scene.BILLING,
        Scene.ACCOUNT_SECURITY,
        Scene.PRE_SALES,
        Scene.AFTER_SALES,
    ):
        assert team_for_scene(scene), scene


def test_a_complaint_goes_to_quality_not_a_general_queue() -> None:
    """The point of the feature, in one assertion."""
    assert team_for_scene(Scene.COMPLAINT) == "quality"


def test_an_unmapped_scene_is_not_guessed_at() -> None:
    """UNSPECIFIED has no team on purpose.

    Sending it somewhere specific would put work in front of a named team on
    the strength of a guess, and a wrong-but-specific destination is worse
    than a general queue because someone now has to notice it is not theirs.
    """
    assert team_for_scene(Scene.UNSPECIFIED) is None
    assert team_for_scene(None) is None
    assert team_for_scene("not-a-scene") is None


def test_the_mapping_is_data_in_one_place() -> None:
    """6.5: routing rules must be auditable, not embedded in a prompt where
    they change whenever the wording does. A plain table in one module is the
    minimum that satisfies; asserting on it is what stops it being scattered
    again."""
    assert isinstance(SCENE_TEAMS, dict)
    assert all(isinstance(k, str) and isinstance(v, str) for k, v in SCENE_TEAMS.items())
    # No scene maps to an empty string - that would read as "routed" while
    # actually going nowhere.
    assert all(value for value in SCENE_TEAMS.values())
