"""Capability filtering: what this tenant, this actor, this conversation may do.

The ordering here is the requirement, and it is not interchangeable:

1. **Start from what the tenant has.** A tool with no connector, or one this
   tenant has not enabled, is not a candidate. It is not "a candidate that
   will fail later" - an unreachable tool is not a candidate at all.
2. **Then apply the actor's permissions.** The policy engine decides; this
   module only asks it and records the answer.
3. **Then apply the task kind.** A read task may be offered read tools. A write
   task may only be offered write tools, and even then only as a *proposal* -
   which is a property of the gateway, not of this function.
4. **Only then** may a model's suggestion be accepted against what survived.

Doing it the other way round - letting the model propose and filtering
afterwards - is the bug this ordering exists to prevent: a rejected suggestion
that has already been shown to an operator reads as a real capability, and
"the AI offered an address change" becomes something a tenant believes the
platform can do.

`crm.update_account` is in `TOOL_CATALOG` and is deliberately excluded from
`selector.py`'s AI vocabulary, because it takes a free-form `fields` patch that
cannot be derived deterministically from an utterance. It can therefore appear
here for a *human* proposer and never for a semantic one. `allow_semantic_write`
is what keeps that distinction, and it defaults off.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from platform_core.agent_runtime.semantic.contracts import SemanticTaskKind
from platform_core.agent_runtime.semantic.validator import CapabilityView
from platform_core.tool_gateway.models import ToolDefinition, ToolRisk

# Risk classes that may serve a read task.
READ_RISKS = frozenset({"read", "low_risk"})

# Risk classes that may serve a write task. `low_write` is a write that does
# not need a second approver; it still goes through the gateway.
WRITE_RISKS = frozenset({"low_write", "confirmed_write", "human_approval"})

# Never offered to the semantic layer under any mode, including
# `semantic_read`. A prohibited tool that reached a model would be a tool whose
# existence the model could report to an operator.
FORBIDDEN_RISKS = frozenset({"prohibited"})


@dataclass
class CapabilityFilter:
    """The outcome: what may be offered, and what was removed and why.

    `refused` is kept as a structured list rather than a log line because the
    workbench needs to say "this tenant cannot change delivery addresses" and
    the eval report needs to count refusals by reason. Both require the reason
    to survive past the request.
    """

    available: dict[str, CapabilityView] = field(default_factory=dict)
    refused: list[tuple[str, str]] = field(default_factory=list)

    def as_snapshot(self) -> dict[str, Any]:
        # Names and reasons only; a tool's schema is not sensitive but neither
        # is it useful in a metric label.
        return {
            "available": sorted(self.available),
            "refused_count": len(self.refused),
            "refused_reasons": sorted({reason for _, reason in self.refused}),
        }


async def tenant_capabilities(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    kind: SemanticTaskKind,
    actor_role: str,
    principal_id: str,
    policy: Any,
    allow_semantic_write: bool = False,
) -> CapabilityFilter:
    """Build the capability set for one semantic decision.

    `policy` is the `PolicyEngine`-compatible checker, injected so this stays
    testable without a policy fixture. `actor_role` and `principal_id` are
    the *server-derived* values - never anything a request body supplied.
    """
    from platform_policy import Action, Decision, Principal

    # Platform-catalog tools have tenant_id IS NULL and are available to
    # every tenant; SQL IN does not match NULL, hence the explicit OR.
    # Highest version per name wins, which is how a tenant's upgraded
    # definition shadows the catalog row.
    stmt = (
        select(ToolDefinition)
        .where(
            (ToolDefinition.tenant_id == tenant_id) | ToolDefinition.tenant_id.is_(None),
        )
        .order_by(ToolDefinition.name, ToolDefinition.version.desc())
    )
    rows = (await session.execute(stmt)).scalars().all()

    result = CapabilityFilter()
    principal = Principal(tenant_id=str(tenant_id), actor_id=principal_id, role=actor_role)
    seen: set[str] = set()

    for row in rows:
        name = row.name
        if name in seen:
            continue
        seen.add(name)
        risk = row.risk

        if risk in FORBIDDEN_RISKS:
            result.refused.append((name, "CAPABILITY_PROHIBITED"))
            continue

        if kind is SemanticTaskKind.READ:
            if risk not in READ_RISKS:
                result.refused.append((name, "CAPABILITY_NOT_A_READ"))
                continue
        elif kind is SemanticTaskKind.WRITE:
            if risk not in WRITE_RISKS:
                result.refused.append((name, "CAPABILITY_NOT_A_WRITE"))
                continue
            if not allow_semantic_write:
                # The whole point of the R1 boundary. A write reached from a
                # model output would skip the proposal a human has to see,
                # even though the tool itself is registered.
                result.refused.append((name, "CAPABILITY_SEMANTIC_WRITE_DISABLED"))
                continue
        else:
            # A clarification task needs no tool at all.
            continue

        # The row's own `required_permissions` is authoritative, not the risk
        # class: a deployment may require more than the risk implies, and
        # deriving the action from the risk would silently under-check.
        for raw_action in list(row.required_permissions or ()):
            try:
                action = Action(str(raw_action))
            except ValueError:
                result.refused.append((name, "CAPABILITY_PERMISSION_UNKNOWN"))
                break
            decision = policy.check(principal, action)
            if decision.decision is not Decision.ALLOW:
                result.refused.append((name, decision.reason_code or "CAPABILITY_POLICY_DENIED"))
                break
        else:
            result.available[name] = CapabilityView(
                tool_name=name,
                # The registry's risk, never the model's. A model claiming a
                # write is `read` cannot downgrade it here.
                risk_class=risk,
                allowed_task_kinds=frozenset({kind.value}),
            )

    return result


def tools_for_conversation(
    capabilities: dict[str, CapabilityView],
    *,
    already_verified: dict[str, Any],
) -> dict[str, CapabilityView]:
    """Narrow a capability set by what this conversation has already verified.

    A read whose result is already in hand needs no second call, and offering it
    again invites a duplicate external request for information the agent is
    already looking at. Ownership is the caller's responsibility: this only
    removes tools whose subject record is already present and verified.
    """
    if not already_verified:
        return capabilities
    return {
        name: cap
        for name, cap in capabilities.items()
        if not _already_satisfied(name, already_verified)
    }


def _already_satisfied(tool_name: str, verified: dict[str, Any]) -> bool:
    """Whether a verified fact already answers this tool's question.

    A deliberately small, explicit mapping. Guessing from a tool name's prefix
    would make "we have any order record" suppress a *different* order's status
    lookup, which is the kind of inference that produces a confidently wrong
    answer to a customer.
    """
    return bool(verified.get(tool_name))


__all__ = [
    "FORBIDDEN_RISKS",
    "READ_RISKS",
    "WRITE_RISKS",
    "CapabilityFilter",
    "tenant_capabilities",
    "tools_for_conversation",
]


# Imported for the risk enum's documentation value: the string literals above
# must match `ToolRisk`, and referencing it here keeps the two in one file.
_RISK_VALUES = tuple(ToolRisk)
