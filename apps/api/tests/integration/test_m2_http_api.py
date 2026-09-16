"""Integration tests: M2 HTTP API surface (cases, retrieval, agent runs, tools).

These tests exercise the real FastAPI app against the real PostgreSQL
instance with RLS enabled. What they are here to prove, in order of
importance:

1. **No silent widening.** A caller cannot grant itself a role or a tenant.
   Auth failure is 401; policy refusal is a 403 envelope; cross-tenant reads
   return 404 rather than another tenant's data.
2. **Envelope discipline.** Every failure uses the documented
   `{"error": {code, message, retryable, details}}` shape with a stable code,
   and every success carries `trace_id`.
3. **Write discipline.** Commands without an `Idempotency-Key` are refused
   before any state changes.
4. **Tool safety.** A high-risk tool cannot be executed without a matching
   confirmation bound to the frozen action hash.

The pattern mirrors `test_audit_api.py`: a role-fabricating resolver is
injected so policy gates can be tested without a live Keycloak.
"""

import os
import uuid
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from platform_core.identity.middleware import TenantContextMiddleware
from platform_core.identity.tenant_context import TenantContext

pytestmark = pytest.mark.integration

ADMIN_URL = os.environ.get(
    "APP_ADMIN_DATABASE_URL",
    "postgresql+psycopg://platform:platform@localhost:5435/platform",
)

TENANT_A = "01900000-0000-7000-8000-0000000000c1"
TENANT_B = "01900000-0000-7000-8000-0000000000c2"
SLUG_A = "m2-api-a"
SLUG_B = "m2-api-b"

# A tool that exercises the confirmation gate: high risk, requires an
# explicit human confirmation before it can execute.
CONFIRMED_TOOL = "m2_test_refund"
# A tool whose risk class does not require confirmation.
LOW_RISK_TOOL = "m2_test_add_note"


@pytest.fixture(scope="module", autouse=True)
def seed_tenants_and_tools() -> None:
    """Seed two tenants and the tool catalog used by the tool-gateway tests.

    `tool_definitions.tenant_id` is nullable because the platform catalog is
    global reference data; these definitions are seeded with a NULL tenant
    so both tenants resolve the same catalog row, which is how production
    platform tools are modeled.
    """
    admin = create_engine(ADMIN_URL)
    _cleanup(admin)  # a previous interrupted run may have left rows behind
    with admin.begin() as conn:
        for tid, slug in ((TENANT_A, SLUG_A), (TENANT_B, SLUG_B)):
            conn.execute(
                text(
                    "INSERT INTO tenants (id, slug, name, status) VALUES "
                    "(:id, :slug, 'M2', 'active') ON CONFLICT (slug) DO NOTHING"
                ),
                {"id": tid, "slug": slug},
            )
        conn.execute(
            text(
                "INSERT INTO tool_definitions (id, tenant_id, name, version, risk, "
                "input_schema, output_schema, required_permissions, timeout_ms, "
                "idempotent, requires_confirmation) VALUES "
                "(gen_random_uuid(), NULL, :name, 1, :risk, "
                "CAST(:inschema AS jsonb), '{}'::jsonb, '[]'::jsonb, 10000, true, :reqconf)"
            ),
            [
                {
                    "name": CONFIRMED_TOOL,
                    "risk": "confirmed_write",
                    "inschema": '{"type":"object","properties":{"amount":{"type":"integer"}},'
                    '"required":["amount"]}',
                    "reqconf": True,
                },
                {
                    "name": LOW_RISK_TOOL,
                    "risk": "low_write",
                    "inschema": '{"type":"object","properties":{"note":{"type":"string"}}}',
                    "reqconf": False,
                },
            ],
        )
    yield
    _cleanup(admin)
    admin.dispose()


def _cleanup(admin: Any) -> None:
    """Remove this module's rows in FK order.

    Proposals reference definitions and executions reference proposals, so
    the deletion order matters: a definition cannot be removed while a
    proposal still points at it.
    """
    with admin.begin() as conn:
        names = (CONFIRMED_TOOL, LOW_RISK_TOOL)
        conn.execute(
            text(
                "DELETE FROM tool_executions WHERE proposal_id IN ("
                "  SELECT p.id FROM tool_proposals p"
                "  JOIN tool_definitions d ON d.id = p.tool_definition_id"
                "  WHERE d.name = ANY(:names))"
            ),
            {"names": list(names)},
        )
        conn.execute(
            text(
                "DELETE FROM action_confirmations WHERE proposal_id IN ("
                "  SELECT p.id FROM tool_proposals p"
                "  JOIN tool_definitions d ON d.id = p.tool_definition_id"
                "  WHERE d.name = ANY(:names))"
            ),
            {"names": list(names)},
        )
        conn.execute(
            text(
                "DELETE FROM tool_proposals WHERE tool_definition_id IN ("
                "  SELECT id FROM tool_definitions WHERE name = ANY(:names))"
            ),
            {"names": list(names)},
        )
        conn.execute(
            text("DELETE FROM tool_definitions WHERE name = ANY(:names)"),
            {"names": list(names)},
        )
        conn.execute(
            text("DELETE FROM cases WHERE tenant_id IN (:t1, :t2)"),
            {"t1": TENANT_A, "t2": TENANT_B},
        )
        conn.execute(
            text("DELETE FROM audit_events WHERE tenant_id IN (:t1, :t2)"),
            {"t1": TENANT_A, "t2": TENANT_B},
        )
        conn.execute(
            text("DELETE FROM outbox_events WHERE tenant_id IN (:t1, :t2)"),
            {"t1": TENANT_A, "t2": TENANT_B},
        )
        conn.execute(
            text("DELETE FROM inbox_events WHERE tenant_id IN (:t1, :t2)"),
            {"t1": TENANT_A, "t2": TENANT_B},
        )
        for slug in (SLUG_A, SLUG_B):
            conn.execute(text("DELETE FROM tenants WHERE slug = :slug"), {"slug": slug})


class _RoleResolver:
    """Fabricate a TenantContext with a fixed tenant and role.

    Resolution is server-side in production (bootstrap token -> membership
    row); this stands in for that lookup so the policy gate is the thing
    under test rather than the auth backend.
    """

    def __init__(self, tenant_id: str, role: str | None) -> None:
        self._tenant_id = tenant_id
        self._role = role
        self._actor = uuid.uuid5(uuid.NAMESPACE_URL, f"actor:{tenant_id}-{role}")

    async def __call__(self, request: object) -> TenantContext:
        return TenantContext(
            tenant_id=uuid.UUID(self._tenant_id),
            actor_id=self._actor,
            actor_kind="user",
            role=self._role,
        )


def _client(tenant_id: str, role: str | None) -> TestClient:
    """Build a fresh app sharing the real routers under a fabricated role.

    A fresh FastAPI instance is required because middleware cannot be added
    after the app has started.
    """
    import importlib

    main_mod = importlib.import_module("platform_core.main")
    fresh = FastAPI()
    for route in main_mod.app.router.routes:
        fresh.router.routes.append(route)
    fresh.add_middleware(TenantContextMiddleware, resolver=_RoleResolver(tenant_id, role))
    return TestClient(fresh, raise_server_exceptions=False)


def _auth() -> dict[str, str]:
    return {"Authorization": "Bearer pt_bootstrap_test"}


# --- 1. Authentication -----------------------------------------------------


def test_missing_bearer_token_is_401() -> None:
    """An unauthenticated request never reaches a handler."""
    from platform_core.identity.middleware import bootstrap_token_resolver
    from platform_core.main import app

    if not any(
        getattr(m.cls, "__name__", "") == "TenantContextMiddleware" for m in app.user_middleware
    ):
        app.add_middleware(TenantContextMiddleware, resolver=bootstrap_token_resolver)
    client = TestClient(app, raise_server_exceptions=False)

    resp = client.get("/v1/cases")
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "AUTH_UNRESOLVED"


def test_unknown_role_is_denied_by_policy() -> None:
    """A principal with an unknown role fails closed, not open."""
    resp = _client(TENANT_A, "no_such_role").get("/v1/cases", headers=_auth())
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "POLICY_DENIED"


# --- 2. Error envelope -----------------------------------------------------


def test_error_envelope_shape() -> None:
    """Every failure uses the same four-key error object plus trace_id."""
    resp = _client(TENANT_A, "support_agent").get("/v1/cases/not-a-uuid", headers=_auth())
    assert resp.status_code == 400
    body = resp.json()
    assert set(body.keys()) == {"error", "trace_id"}
    err = body["error"]
    assert set(err.keys()) == {"code", "message", "retryable", "details"}
    assert err["code"] == "VALIDATION_FAILED"
    assert isinstance(err["retryable"], bool)


def test_success_carries_trace_id() -> None:
    resp = _client(TENANT_A, "support_agent").get("/v1/cases", headers=_auth())
    assert resp.status_code == 200
    assert resp.json()["trace_id"]


def test_unsupported_action_suffix_is_404_not_500() -> None:
    """A nonexistent sub-resource is a clean 404."""
    resp = _client(TENANT_A, "support_agent").post(
        f"/v1/cases/{uuid.uuid4()}/nonsense", headers=_auth(), json={}
    )
    assert resp.status_code == 404


# --- 3. Write discipline (Idempotency-Key) ---------------------------------


def test_case_command_without_idempotency_key_is_refused() -> None:
    """The write is refused before any version bump is consumed."""
    client = _client(TENANT_A, "support_admin")
    case_resp = client.post(
        "/v1/cases",
        headers=_auth(),
        json={"subject": "idem guard", "priority": "p2"},
    )
    assert case_resp.status_code == 200
    case_id = case_resp.json()["case"]["case_id"]

    resp = client.post(
        f"/v1/cases/{case_id}/commands",
        headers=_auth(),
        json={"command": "change_priority", "parameters": {"priority": "p1"}},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"

    # Proof that nothing changed: version is still the creation version.
    after = client.get(f"/v1/cases/{case_id}", headers=_auth()).json()["case"]
    assert after["version"] == 1


def test_agent_run_requires_idempotency_key() -> None:
    resp = _client(TENANT_A, "support_admin").post(
        f"/v1/conversations/{uuid.uuid4()}/agent-runs",
        headers=_auth(),
        json={"trigger_message_ref": "msg-1"},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"


# --- 4. Cross-tenant isolation --------------------------------------------


def test_cross_tenant_case_read_is_404() -> None:
    """Tenant B cannot see tenant A's case, even with a valid id."""
    a_client = _client(TENANT_A, "support_admin")
    created = a_client.post("/v1/cases", headers=_auth(), json={"subject": "private to A"}).json()[
        "case"
    ]["case_id"]

    b_resp = _client(TENANT_B, "support_admin").get(f"/v1/cases/{created}", headers=_auth())
    assert b_resp.status_code == 404
    assert b_resp.json()["error"]["code"] == "CASE_NOT_FOUND"


def test_cross_tenant_tool_proposal_read_is_404() -> None:
    """A proposal id from another tenant does not leak its existence."""
    a_client = _client(TENANT_A, "support_admin")
    proposed = a_client.post(
        "/v1/tool-proposals",
        headers={**_auth(), "Idempotency-Key": "x-tenant-propose"},
        json={"tool_name": LOW_RISK_TOOL, "arguments": {"note": "hello"}},
    )
    assert proposed.status_code == 200, proposed.text
    proposal_id = proposed.json()["proposal"]["proposal_id"]

    b_resp = _client(TENANT_B, "support_admin").get(
        f"/v1/tool-proposals/{proposal_id}", headers=_auth()
    )
    assert b_resp.status_code == 404
    assert b_resp.json()["error"]["code"] == "PROPOSAL_NOT_FOUND"


# --- 5. Retrieval ----------------------------------------------------------


def test_retrieval_query_requires_knowledge_read() -> None:
    """support_viewer holds knowledge.read, so it is allowed through."""
    resp = _client(TENANT_A, "support_viewer").post(
        "/v1/retrieval/query", headers=_auth(), json={"query": "anything"}
    )
    assert resp.status_code == 200
    assert "results" in resp.json()


def test_retrieval_query_denied_for_role_without_knowledge_read() -> None:
    """auditor has audit.read but no knowledge.read -> 403."""
    resp = _client(TENANT_A, "auditor").post(
        "/v1/retrieval/query", headers=_auth(), json={"query": "anything"}
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "POLICY_DENIED"


def test_retrieval_rejects_top_k_above_ceiling() -> None:
    """top_k is bounded before it can become unbounded server work."""
    resp = _client(TENANT_A, "support_viewer").post(
        "/v1/retrieval/query", headers=_auth(), json={"query": "x", "top_k": 5000}
    )
    assert resp.status_code == 422


# --- 6. Case optimistic concurrency ---------------------------------------


def test_stale_case_version_conflicts() -> None:
    """A command carrying an outdated expected_version is refused with 409."""
    client = _client(TENANT_A, "support_admin")
    case_id = client.post(
        "/v1/cases", headers=_auth(), json={"subject": "concurrency", "priority": "p2"}
    ).json()["case"]["case_id"]

    first = client.post(
        f"/v1/cases/{case_id}/commands",
        headers={**_auth(), "Idempotency-Key": "cc-1"},
        json={
            "command": "change_priority",
            "expected_version": 1,
            "parameters": {"priority": "p1"},
        },
    )
    assert first.status_code == 200
    assert first.json()["case"]["version"] == 2

    stale = client.post(
        f"/v1/cases/{case_id}/commands",
        headers={**_auth(), "Idempotency-Key": "cc-2"},
        json={
            "command": "change_priority",
            "expected_version": 1,  # already consumed
            "parameters": {"priority": "p3"},
        },
    )
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "CASE_VERSION_CONFLICT"


# --- 7. Tool gateway -------------------------------------------------------


def test_confirmed_write_tool_requires_confirmation_before_execute() -> None:
    """The safety-critical path: no confirmation, no execution.

    This is the single most important assertion in this file. A high-risk
    tool must not execute because the HTTP caller asked nicely.
    """
    client = _client(TENANT_A, "support_admin")
    proposed = client.post(
        "/v1/tool-proposals",
        headers={**_auth(), "Idempotency-Key": "conf-1"},
        json={"tool_name": CONFIRMED_TOOL, "arguments": {"amount": 500}},
    )
    assert proposed.status_code == 200, proposed.text
    proposal = proposed.json()["proposal"]
    assert proposal["required_confirmation"] is True
    assert proposal["status"] == "authorized"

    # Executing without confirming is refused with a stable code.
    blocked = client.post(
        f"/v1/tool-proposals/{proposal['proposal_id']}/execute",
        headers={**_auth(), "Idempotency-Key": "conf-exec-1"},
        json={},
    )
    assert blocked.status_code == 409
    assert blocked.json()["error"]["code"] == "CONFIRMATION_REQUIRED"


def test_low_risk_tool_cannot_be_confirmed_unnecessarily() -> None:
    """A meaningless confirmation is rejected, not silently recorded.

    Storing an approval for an action that never needed one would make the
    audit trail misleading.
    """
    client = _client(TENANT_A, "support_admin")
    proposed = client.post(
        "/v1/tool-proposals",
        headers={**_auth(), "Idempotency-Key": "lowrisk-1"},
        json={"tool_name": LOW_RISK_TOOL, "arguments": {"note": "hello"}},
    )
    assert proposed.status_code == 200, proposed.text
    proposal = proposed.json()["proposal"]
    assert proposal["required_confirmation"] is False

    resp = client.post(
        f"/v1/tool-proposals/{proposal['proposal_id']}/confirm",
        headers=_auth(),
        json={},
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "CONFIRMATION_NOT_REQUIRED"


def test_unregistered_tool_is_refused() -> None:
    resp = _client(TENANT_A, "support_admin").post(
        "/v1/tool-proposals",
        headers={**_auth(), "Idempotency-Key": "unreg-1"},
        json={"tool_name": "m2_does_not_exist", "arguments": {}},
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "TOOL_NOT_REGISTERED"


def test_arguments_failing_schema_are_refused() -> None:
    """JSON Schema validation is deny-by-default: `amount` is required."""
    resp = _client(TENANT_A, "support_admin").post(
        "/v1/tool-proposals",
        headers={**_auth(), "Idempotency-Key": "schema-1"},
        json={"tool_name": CONFIRMED_TOOL, "arguments": {}},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "TOOL_ARGS_INVALID"


def test_credentials_in_arguments_are_redacted_before_storage() -> None:
    """A credential-shaped key is stored as *** and never echoed back."""
    resp = _client(TENANT_A, "support_admin").post(
        "/v1/tool-proposals",
        headers={**_auth(), "Idempotency-Key": "redact-1"},
        json={
            "tool_name": LOW_RISK_TOOL,
            "arguments": {"note": "ok", "api_key": "super-secret-value"},
        },
    )
    assert resp.status_code == 200, resp.text
    stored = resp.json()["proposal"]["arguments"]
    assert stored["api_key"] == "***"
    assert "super-secret-value" not in resp.text


def test_tool_proposal_requires_idempotency_key() -> None:
    resp = _client(TENANT_A, "support_admin").post(
        "/v1/tool-proposals",
        headers=_auth(),
        json={"tool_name": LOW_RISK_TOOL, "arguments": {"note": "x"}},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"


def test_read_only_role_cannot_propose_low_write() -> None:
    """support_agent holds tool.read but not tool.write.low."""
    resp = _client(TENANT_A, "support_agent").post(
        "/v1/tool-proposals",
        headers={**_auth(), "Idempotency-Key": "ro-1"},
        json={"tool_name": LOW_RISK_TOOL, "arguments": {"note": "x"}},
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "POLICY_DENIED"


def test_execute_without_executor_reports_missing_executor() -> None:
    """No executor is registered, so execution fails loudly and honestly.

    This asserts the failure is a *known* error code rather than a silent
    success, and that the proposal did not advance to a completed state.
    """
    client = _client(TENANT_A, "support_admin")
    proposed = client.post(
        "/v1/tool-proposals",
        headers={**_auth(), "Idempotency-Key": "noexec-1"},
        json={"tool_name": LOW_RISK_TOOL, "arguments": {"note": "x"}},
    )
    assert proposed.status_code == 200, proposed.text
    proposal_id = proposed.json()["proposal"]["proposal_id"]

    resp = client.post(
        f"/v1/tool-proposals/{proposal_id}/execute",
        headers={**_auth(), "Idempotency-Key": "noexec-exec-1"},
        json={},
    )
    assert resp.status_code == 501
    assert resp.json()["error"]["code"] == "TOOL_EXECUTOR_MISSING"

    # The proposal must not be reported as executed.
    after = client.get(f"/v1/tool-proposals/{proposal_id}", headers=_auth()).json()
    assert after["proposal"]["status"] not in ("executed", "verified")
