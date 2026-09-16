"""Policy package: shared authorization vocabulary (docs/security.md).

Deny overrides allow. RBAC base roles + ABAC condition evaluation +
resource ACL facts supplied by domain modules.
"""

from platform_policy.engine import (
    AccessDecision,
    Action,
    Decision,
    PolicyEngine,
    Principal,
    Resource,
)

__all__ = [
    "AccessDecision",
    "Action",
    "Decision",
    "PolicyEngine",
    "Principal",
    "Resource",
]
