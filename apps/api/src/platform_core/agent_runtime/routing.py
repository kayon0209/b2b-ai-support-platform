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

from platform_core.agent_runtime.intent import Scene

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
