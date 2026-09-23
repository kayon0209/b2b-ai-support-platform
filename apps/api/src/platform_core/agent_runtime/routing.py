"""Which team a handoff is for (feature list 7.3).

"Transfer to a human" is not a destination. A complaint about a short circuit
and a question about an invoice are both "a person must look at this", and
sending them to the same queue means the first person to read either is
probably the wrong one - which is the round trip that makes customers repeat
themselves.

The decision is made here, as data, in one place. That is the 6.5 rule applied
to routing: a rule living in a prompt is a rule nobody can audit, change or
test, and it changes silently whenever the model or its wording does.

**This does not assign anyone.** Chatwoot owns assignment (docs/adr/0001), and
the platform naming a team is a recommendation attached to the handoff, not a
claim that someone is now responsible. Saying otherwise would be reporting an
outcome the platform cannot observe - the same class of lie as a tool reporting
success because its transport call returned.

An unmapped scene yields None, meaning the general queue. Guessing a team for
an unfamiliar scene would send work somewhere specific on the strength of a
guess.
"""

from __future__ import annotations

from platform_core.agent_runtime.intent import BusinessLine, Scene

# Scene -> internal team slug (matches `identity.Department.slug`).
#
# Deliberately a plain table rather than tenant configuration for now: it is
# the single place to change when per-tenant routing lands, and it is
# inspectable and testable in the meantime. A scene absent from this table is
# not a bug to paper over with a fallback - it is an unmapped scene, and the
# general queue is the honest answer for it.
SCENE_TEAMS: dict[str, str] = {
    Scene.COMPLAINT.value: "quality",
    Scene.TECHNICAL_SUPPORT.value: "engineering",
    Scene.ORDER_FULFILMENT.value: "fulfilment",
    Scene.BILLING.value: "finance",
    Scene.ACCOUNT_SECURITY.value: "security",
    Scene.PRE_SALES.value: "sales",
    Scene.AFTER_SALES.value: "support",
}


def team_for_scene(scene: Scene | str | None) -> str | None:
    """The team this scene belongs to, or None for the general queue."""
    if scene is None:
        return None
    key = scene.value if isinstance(scene, Scene) else str(scene)
    return SCENE_TEAMS.get(key)


# (scene, business_line) -> team slug. **Empty by default**, for the same
# reason `SCENE_TEAMS` is a table and not a guess: a slug that no `Department`
# row carries routes work to a queue nobody reads, and the handoff looks
# successful while the customer waits forever.
#
# This is the extension point the audit (7.3) called missing - "routing by
# business line rather than only by scene". It is deliberately data: a PCB
# complaint and a component complaint both belong in `quality`, and only the
# deployment knows whether that team is further split (quality-pcb /
# quality-components). Filling this table in is one line per tenant convention;
# inventing slugs here would be the platform asserting an org chart.
LINE_TEAMS: dict[tuple[str, str], str] = {}


def team_for(
    scene: Scene | str | None, business_line: BusinessLine | str | None = None
) -> str | None:
    """The team a handoff belongs to, refined by business line when configured.

    A configured (scene, line) pair wins; otherwise the scene's team; otherwise
    None. The middle fallback is what makes this additive - deploying it
    without filling `LINE_TEAMS` behaves exactly like `team_for_scene`, so no
    existing handoff changes destination.
    """
    scene_key = scene.value if isinstance(scene, Scene) else (str(scene) if scene else None)
    if scene_key is None:
        return None

    if business_line is not None:
        line_key = (
            business_line.value if isinstance(business_line, BusinessLine) else str(business_line)
        )
        specific = LINE_TEAMS.get((scene_key, line_key))
        if specific:
            return specific

    return SCENE_TEAMS.get(scene_key)


def routing_note(
    scene: Scene | str | None, business_line: BusinessLine | str | None = None
) -> str | None:
    """A short label for the handoff, or None when it adds nothing.

    Carries the business line to the agent even when no team override exists,
    because "a PCB complaint" and "a complaint" are different jobs for the
    person reading the queue, and the line is already classified - dropping it
    at the handoff would be discarding a fact the platform just established.
    Returns None for an unclassified line rather than writing "unspecified"
    onto every handoff, which would be noise dressed as information.
    """
    if business_line is None:
        return None
    line_key = (
        business_line.value if isinstance(business_line, BusinessLine) else str(business_line)
    )
    if not line_key or line_key == BusinessLine.UNSPECIFIED.value:
        return None
    return line_key
