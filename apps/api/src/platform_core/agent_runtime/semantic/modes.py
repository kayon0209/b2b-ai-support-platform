"""Mode resolution and the kill switch.

Two independent controls, and the distinction matters:

- **Tenant feature flags** decide what a tenant has opted into. Deterministic
  per (flag key, tenant id) via the existing `knowledge.flag_service`, so the
  same tenant always gets the same answer in every process.
- **A process kill switch** stops all enhanced work immediately, regardless of
  flags. It exists so an operator can halt a bad deployment without first
  working out which tenants have which flag on.

Both default to closed. A missing flag row, an unreachable database or an
unset environment variable all resolve to "off", never to "on".
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from platform_core.agent_runtime.semantic.contracts import SemanticMode

FLAG_SHADOW = "agent.semantic_shadow"
FLAG_ASSIST = "agent.semantic_assist"
FLAG_SEMANTIC_READ = "agent.semantic_read"
FLAG_TASKS = "agent.conversation_tasks"
FLAG_COPILOT = "agent.copilot_generate"

# The setting name, not a flag: it is a process-level stop, deliberately not
# tenant-scoped.
KILL_SWITCH_SETTING = "semantic_enhancements_enabled"

# Ordered weakest-first. When flags disagree the weakest granted mode wins, so
# enabling `assist` on a tenant that never had `shadow` cannot skip the step
# where its behaviour was only observed.
_MODE_FLAG_ORDER: tuple[tuple[SemanticMode, str], ...] = (
    (SemanticMode.SHADOW, FLAG_SHADOW),
    (SemanticMode.ASSIST, FLAG_ASSIST),
    (SemanticMode.SEMANTIC_READ, FLAG_SEMANTIC_READ),
)


@dataclass(frozen=True)
class ModeResolution:
    """The effective mode and why."""

    mode: SemanticMode
    kill_switch_on: bool
    granted_flags: tuple[str, ...] = ()
    # Set when two flags were on and the weaker one was applied. Recorded
    # because a configuration conflict is something an operator must be able to
    # see, not something to silently normalise.
    conflict: bool = False

    def as_snapshot(self) -> dict[str, Any]:
        return {
            "mode": self.mode.value,
            "kill_switch_on": self.kill_switch_on,
            "granted_flags": list(self.granted_flags),
            "conflict": self.conflict,
        }


def kill_switch_on(settings: Any) -> bool:
    """Whether the process is allowed to do semantic work at all."""
    return bool(getattr(settings, KILL_SWITCH_SETTING, True))


def resolve_mode(
    settings: Any,
    enabled_flags: dict[str, bool],
) -> ModeResolution:
    """Combine the kill switch and the tenant's flags into one mode.

    `enabled_flags` is a plain dict of flag key -> enabled, supplied by the
    caller (usually `flag_service.evaluate`) so this function stays pure and
    unit-testable without a database.
    """
    if not kill_switch_on(settings):
        return ModeResolution(mode=SemanticMode.OFF, kill_switch_on=False)

    granted = [key for _, key in _MODE_FLAG_ORDER if enabled_flags.get(key)]
    if not granted:
        return ModeResolution(mode=SemanticMode.OFF, kill_switch_on=True)

    # The highest-ranked granted flag is the ceiling; anything weaker that is
    # also on is recorded as a conflict rather than silently overriding.
    ceiling = SemanticMode.OFF
    for mode, key in _MODE_FLAG_ORDER:
        if key in granted:
            ceiling = mode
    return ModeResolution(
        mode=ceiling,
        kill_switch_on=True,
        granted_flags=tuple(granted),
        conflict=len(granted) > 1,
    )


def weakest_mode(a: SemanticMode, b: SemanticMode) -> SemanticMode:
    """The more restricted of two modes. Used at every call site that has two
    opinions about how much authority a step has."""
    return a if a.rank <= b.rank else b


__all__ = [
    "FLAG_ASSIST",
    "FLAG_COPILOT",
    "FLAG_SEMANTIC_READ",
    "FLAG_SHADOW",
    "FLAG_TASKS",
    "KILL_SWITCH_SETTING",
    "ModeResolution",
    "kill_switch_on",
    "resolve_mode",
    "weakest_mode",
]


_TENANT_SCOPED: tuple[uuid.UUID, ...] = ()
