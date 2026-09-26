"""Unit tests: RBAC/ABAC policy engine (ticket 22).

Deny-overrides-allow; every decision carries a reason code for audit.
"""

import pytest

from platform_policy import (
    Action,
    Decision,
    PolicyEngine,
    Principal,
    Resource,
)


@pytest.fixture
def engine() -> PolicyEngine:
    return PolicyEngine()


def _agent(**attrs) -> Principal:
    return Principal(tenant_id="t1", actor_id="u1", role="support_agent", attributes=attrs)


def test_agent_can_read_and_update_cases(engine: PolicyEngine) -> None:
    d = engine.check(_agent(), Action.CASE_READ)
    assert d.decision == Decision.ALLOW
    d2 = engine.check(_agent(), Action.CASE_UPDATE)
    assert d2.decision == Decision.ALLOW


def test_agent_cannot_publish_knowledge(engine: PolicyEngine) -> None:
    d = engine.check(_agent(), Action.KNOWLEDGE_PUBLISH)
    assert d.decision == Decision.DENY
    assert d.reason_code == "ROLE_LACKS_ACTION"


def test_unknown_role_denies(engine: PolicyEngine) -> None:
    p = Principal(tenant_id="t1", actor_id="u9", role="super_admin")
    d = engine.check(p, Action.CASE_READ)
    assert d.decision == Decision.DENY
    assert d.reason_code == "ROLE_UNKNOWN"


def test_viewer_cannot_create_cases(engine: PolicyEngine) -> None:
    viewer = Principal(tenant_id="t1", actor_id="u2", role="support_viewer")
    d = engine.check(viewer, Action.CASE_CREATE)
    assert d.decision == Decision.DENY


def test_acls_restrict_when_present(engine: PolicyEngine) -> None:
    restricted = Resource(
        resource_type="document",
        resource_id="d1",
        acl_restricted=True,
        acl_entries=({"principal_type": "department", "principal_id": "beta-team"},),
    )
    in_group = _agent(groups=("beta-team",))
    out_group = _agent(groups=("support-team",))
    assert engine.check(in_group, Action.KNOWLEDGE_READ, restricted).decision == Decision.ALLOW
    d = engine.check(out_group, Action.KNOWLEDGE_READ, restricted)
    assert d.decision == Decision.DENY and d.reason_code == "ACL_NO_MATCH"


def test_unrestricted_resource_needs_no_acl_match(engine: PolicyEngine) -> None:
    open_resource = Resource(resource_type="document", resource_id="d2")
    assert engine.check(_agent(), Action.KNOWLEDGE_READ, open_resource).decision == Decision.ALLOW


def test_classification_restricted_blocks_agents(engine: PolicyEngine) -> None:
    secret_doc = Resource(
        resource_type="document",
        resource_id="d3",
        attributes={"classification": "restricted"},
    )
    d = engine.check(_agent(), Action.KNOWLEDGE_READ, secret_doc)
    assert d.decision == Decision.DENY
    assert d.reason_code == "CLASSIFICATION_RESTRICTED"
    # security_admin passes RBAC and ABAC
    sec = Principal(tenant_id="t1", actor_id="u3", role="security_admin")
    assert engine.check(sec, Action.KNOWLEDGE_READ, secret_doc).decision == Decision.ALLOW


def test_region_mismatch_denies(engine: PolicyEngine) -> None:
    resource = Resource(resource_type="case", resource_id="c1", attributes={"region": "eu"})
    cn_agent = _agent(region="cn-north-1")
    d = engine.check(cn_agent, Action.CASE_READ, resource)
    assert d.decision == Decision.DENY and d.reason_code == "REGION_MISMATCH"


def test_suspended_account_blocks_writes_but_allows_reads(engine: PolicyEngine) -> None:
    suspended = Resource(
        resource_type="enterprise_account",
        resource_id="ea1",
        attributes={"contract_status": "suspended"},
    )
    d = engine.check(_agent(), Action.CASE_CREATE, suspended)
    assert d.decision == Decision.DENY and d.reason_code == "ACCOUNT_SUSPENDED"
    assert engine.check(_agent(), Action.CASE_READ, suspended).decision == Decision.ALLOW


def test_department_mismatch_blocks_agent_case_updates(engine: PolicyEngine) -> None:
    other_dept = Resource(
        resource_type="case",
        resource_id="c2",
        attributes={"department": "billing"},
    )
    d = engine.check(_agent(department="technical"), Action.CASE_UPDATE, other_dept)
    assert d.decision == Decision.DENY and d.reason_code == "DEPARTMENT_MISMATCH"
    # same department passes
    ok = engine.check(_agent(department="billing"), Action.CASE_UPDATE, other_dept)
    assert ok.decision == Decision.ALLOW


def test_tenant_owner_has_all_actions(engine: PolicyEngine) -> None:
    owner = Principal(tenant_id="t1", actor_id="u4", role="tenant_owner")
    for action in (
        Action.SECURITY_ADMIN,
        Action.TOOL_HUMAN_APPROVAL,
        Action.KNOWLEDGE_ACL_MANAGE,
    ):
        assert engine.check(owner, action).decision == Decision.ALLOW


def test_agent_role_may_propose_a_confirmed_write_but_not_approve_one(
    engine: PolicyEngine,
) -> None:
    """The agent's write authority is bounded by the confirmation route.

    `integration_service` holds TOOL_WRITE_CONFIRMED so the agent can put a
    confirmed write in front of a human. AGENTS.md rule 7 says an LLM may
    propose a write action, and without this action the gateway denied the
    agent at propose time — it re-checks the risk class's action — so the
    rule was unimplementable and no human ever saw the proposal.

    What makes the grant safe is the complement asserted here. The only path
    that creates an ActionConfirmation is
    `POST /v1/tool-proposals/{id}/confirm`, which requires CASE_UPDATE, which
    this role must NOT hold. Adding CASE_UPDATE to this role would silently
    turn the agent into its own approver and the grant above into a way
    around the confirmation. TOOL_HUMAN_APPROVAL stays out for the same
    reason: it is the top of the risk ladder and has to be unreachable by the
    agent at every stage, propose included.
    """
    agent = Principal(tenant_id="t1", actor_id="ai", role="integration_service")
    assert engine.check(agent, Action.TOOL_READ).decision == Decision.ALLOW
    assert engine.check(agent, Action.TOOL_WRITE_LOW).decision == Decision.ALLOW
    assert engine.check(agent, Action.TOOL_WRITE_CONFIRMED).decision == Decision.ALLOW

    confirm_gate = engine.check(agent, Action.CASE_UPDATE)
    assert confirm_gate.decision == Decision.DENY
    assert confirm_gate.reason_code == "ROLE_LACKS_ACTION"
    assert engine.check(agent, Action.TOOL_HUMAN_APPROVAL).decision == Decision.DENY
