"""Authorization engine (ticket 22, docs/security.md authorization).

Three layers, evaluated in order, deny overrides allow:
1. RBAC: role -> permitted action set (base vocabulary).
2. ABAC: condition expressions over principal/resource attributes.
3. Resource ACL: exceptional grants/denies supplied by domain modules.

The engine is pure and synchronous: no DB, no I/O. Domain modules own the
policy FACTS (role sets, attribute maps, ACL rows) and call this engine
with them. Every decision returns an AccessDecision with a reason code so
audit events never guess why access was granted or denied.
"""

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class Decision(StrEnum):
    ALLOW = "allow"
    DENY = "deny"


class Action(StrEnum):
    # Case vocabulary
    CASE_READ = "case.read"
    CASE_CREATE = "case.create"
    CASE_UPDATE = "case.update"
    CASE_CLOSE = "case.close"
    # Knowledge vocabulary
    KNOWLEDGE_READ = "knowledge.read"
    KNOWLEDGE_UPLOAD = "knowledge.upload"
    KNOWLEDGE_PUBLISH = "knowledge.publish"
    KNOWLEDGE_ACL_MANAGE = "knowledge.acl.manage"
    # Administration vocabulary
    TENANT_ADMIN = "tenant.admin"
    SECURITY_ADMIN = "security.admin"
    AUDIT_READ = "audit.read"
    # Prompt/model configuration release (docs/development-plan.md Phase 4).
    # Separate from SECURITY_ADMIN because promoting a prompt changes what
    # every customer-visible answer says - it is a production change with a
    # quality blast radius, not a security-configuration change.
    PROMPT_READ = "prompt.read"
    PROMPT_RELEASE = "prompt.release"
    # Feature flag vocabulary. FLAG_READ is separate from FLAG_WRITE because
    # "what is currently rolled out?" is incident-analysis information that
    # security staff and auditors need, while moving a rollout is a
    # production change reserved for the tenant owner.
    FLAG_READ = "flag.read"
    FLAG_WRITE = "flag.write"
    # Tools vocabulary
    TOOL_READ = "tool.read"
    TOOL_WRITE_LOW = "tool.write.low"
    TOOL_WRITE_CONFIRMED = "tool.write.confirmed"
    TOOL_HUMAN_APPROVAL = "tool.human_approval"


# Role -> allowed actions (docs/security.md recommended roles).
RBAC_TABLE: dict[str, frozenset[Action]] = {
    "tenant_owner": frozenset(Action),  # all actions
    "security_admin": frozenset(
        {
            Action.SECURITY_ADMIN,
            Action.AUDIT_READ,
            Action.CASE_READ,
            Action.KNOWLEDGE_READ,
            Action.PROMPT_READ,
            Action.FLAG_READ,
        }
    ),
    "support_admin": frozenset(
        {
            Action.CASE_READ,
            Action.CASE_CREATE,
            Action.CASE_UPDATE,
            Action.CASE_CLOSE,
            Action.KNOWLEDGE_READ,
            Action.KNOWLEDGE_UPLOAD,
            Action.KNOWLEDGE_ACL_MANAGE,
            Action.TOOL_READ,
            Action.TOOL_WRITE_LOW,
            # A support admin can authorize a confirmed write (the
            # confirmation still binds to the exact action hash and
            # expires) but not a human_approval tool, which is reserved
            # for tenant_owner.
            Action.TOOL_WRITE_CONFIRMED,
        }
    ),
    "knowledge_manager": frozenset(
        {
            Action.KNOWLEDGE_READ,
            Action.KNOWLEDGE_UPLOAD,
            Action.KNOWLEDGE_PUBLISH,
        }
    ),
    "support_agent": frozenset(
        {
            Action.CASE_READ,
            Action.CASE_CREATE,
            Action.CASE_UPDATE,
            Action.KNOWLEDGE_READ,
            Action.TOOL_READ,
        }
    ),
    "support_viewer": frozenset({Action.CASE_READ, Action.KNOWLEDGE_READ}),
    "integration_service": frozenset({Action.TOOL_READ, Action.TOOL_WRITE_LOW}),
    "auditor": frozenset(
        {Action.AUDIT_READ, Action.CASE_READ, Action.PROMPT_READ, Action.FLAG_READ}
    ),
}


@dataclass(frozen=True)
class Principal:
    """Authenticated actor resolved server-side (never from payloads)."""

    tenant_id: str
    actor_id: str
    role: str
    # ABAC attributes: department, enterprise_account_id, region, ...
    attributes: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Resource:
    """Target resource with domain-supplied attributes and ACL facts."""

    resource_type: str
    resource_id: str
    attributes: dict[str, Any] = field(default_factory=dict)
    # ACL entries relevant to this resource: list of
    # {"principal_type": ..., "principal_id": ..., "permission": ...}
    acl_entries: tuple[dict[str, str], ...] = field(default_factory=tuple)
    # True when the resource carries at least one ACL row (fail-closed mode)
    acl_restricted: bool = False


@dataclass(frozen=True)
class AccessDecision:
    decision: Decision
    reason_code: str
    role: str


class PolicyEngine:
    """Deny-overrides-allow evaluator. See module docstring for layers."""

    def __init__(self, rbac_table: dict[str, frozenset[Action]] | None = None) -> None:
        self._rbac = rbac_table or RBAC_TABLE

    def check(
        self,
        principal: Principal,
        action: Action,
        resource: Resource | None = None,
    ) -> AccessDecision:
        # Layer 0: unknown roles deny outright.
        allowed = self._rbac.get(principal.role)
        if allowed is None:
            return AccessDecision(Decision.DENY, "ROLE_UNKNOWN", principal.role)

        # Layer 1: RBAC base permission.
        if action not in allowed:
            return AccessDecision(Decision.DENY, "ROLE_LACKS_ACTION", principal.role)

        # Layer 2: resource ACL (only when the domain supplied one).
        if resource is not None and resource.acl_restricted:
            matched = self._acl_match(principal, resource)
            if not matched:
                return AccessDecision(Decision.DENY, "ACL_NO_MATCH", principal.role)

        # Layer 3: ABAC conditions per action family.
        abac_reason = self._abac(principal, action, resource)
        if abac_reason is not None:
            return AccessDecision(Decision.DENY, abac_reason, principal.role)

        return AccessDecision(Decision.ALLOW, "OK", principal.role)

    @staticmethod
    def _acl_match(principal: Principal, resource: Resource) -> bool:
        for entry in resource.acl_entries:
            p_type = entry.get("principal_type")
            p_id = entry.get("principal_id")
            if p_type == "user" and p_id == principal.actor_id:
                return True
            if p_type == "role" and p_id == principal.role:
                return True
            if p_type in ("department", "enterprise_account"):
                groups = principal.attributes.get("groups", ())
                if isinstance(groups, (list, tuple, set, frozenset)) and p_id in groups:
                    return True
        return False

    @staticmethod
    def _abac(principal: Principal, action: Action, resource: Resource | None) -> str | None:
        """Return a deny reason code or None to allow.

        Conditions (docs/security.md): department, enterprise account,
        classification, region, contract status.
        """
        if resource is None:
            return None
        attrs = resource.attributes

        # Region confinement: principals cannot act outside their region.
        principal_region = principal.attributes.get("region")
        resource_region = attrs.get("region")
        if principal_region and resource_region and principal_region != resource_region:
            return "REGION_MISMATCH"

        # Classification ladder: restricted data needs security_admin or
        # tenant_owner regardless of role grants above.
        classification = attrs.get("classification")
        if classification == "restricted" and action in (
            Action.KNOWLEDGE_READ,
            Action.CASE_READ,
        ):
            if principal.role not in ("security_admin", "tenant_owner"):
                return "CLASSIFICATION_RESTRICTED"

        # Contract status: suspended enterprise accounts are read-only.
        if attrs.get("contract_status") == "suspended" and action in (
            Action.CASE_CREATE,
            Action.CASE_UPDATE,
            Action.TOOL_WRITE_LOW,
            Action.TOOL_WRITE_CONFIRMED,
        ):
            return "ACCOUNT_SUSPENDED"

        # Department scoping for case updates: agents only touch their
        # department's cases unless they are admins.
        if action in (Action.CASE_UPDATE, Action.CASE_CLOSE) and principal.role == "support_agent":
            dept = principal.attributes.get("department")
            case_dept = attrs.get("department")
            if dept and case_dept and dept != case_dept:
                return "DEPARTMENT_MISMATCH"
        return None
