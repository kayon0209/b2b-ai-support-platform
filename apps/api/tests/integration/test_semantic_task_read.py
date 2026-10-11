"""A planned semantic read executes through Gateway and completes its task."""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from platform_core.evaluation.task_trajectory import (
    build_task_quality_record,
    score_task_trajectory,
)
from platform_core.identity.middleware import TenantContextMiddleware
from platform_core.identity.tenant_context import TenantContext

pytestmark = pytest.mark.integration

_DEEPEVAL_EVAL = os.getenv("DEEPEVAL_LOCAL_EVAL_RUN") == "1"
if _DEEPEVAL_EVAL:
    from deepeval import assert_test
    from deepeval.dataset import Golden
    from evals.deepeval_metrics import (
        TaskQualityRecordExactMetric,
        TaskTrajectoryExactMetric,
    )
    from evals.tracing import local_observe, require_tracing

    require_tracing()


def _observe_eval_span(*, span_type: str, name: str):
    if _DEEPEVAL_EVAL:
        return local_observe(span_type=span_type, name=name)

    def passthrough(function):
        return function

    return passthrough


@_observe_eval_span(span_type="tool", name="order.get_status")
async def _read_synthetic_order(parameters: dict[str, object]) -> dict[str, object]:
    return {
        "found": True,
        "resource": "orders",
        "order_id": parameters["order_id"],
        "status": "in_production",
        "account": "account-read-test",
        "source": "integration-fixture",
        "fetched_at": "2026-10-07T00:00:00Z",
    }


@_observe_eval_span(span_type="tool", name="verify_order_status")
async def _verify_synthetic_order(
    parameters: dict[str, object], output: dict[str, object] | None
) -> bool:
    return bool(output and output.get("order_id") == parameters.get("order_id"))


@_observe_eval_span(span_type="agent", name="persisted_task_trajectory")
def _attach_task_trajectory_trace(input_text: str, expected_json: str, observed_json: str) -> None:
    if not _DEEPEVAL_EVAL:
        return
    from deepeval.tracing import update_current_trace as update_trace

    update_trace(
        name="verified-read-task-trajectory",
        input=input_text,
        output=observed_json,
        expected_output=expected_json,
        tags=["integration-fixture", "read-only", "verified-receipt"],
        metadata={"case_id": "synthetic-workbench-order-status-read-001"},
    )


ADMIN_URL = os.environ.get("APP_ADMIN_DATABASE_URL", "")
TENANT = uuid.UUID("0190f000-0000-7000-8000-000000001201")
CONVERSATION = uuid.UUID("0190f000-0000-7000-8000-000000001202")
FEEDBACK_CONVERSATION = uuid.UUID("0190f000-0000-7000-8000-000000001205")
FEEDBACK_SECOND_CONVERSATION = uuid.UUID("0190f000-0000-7000-8000-000000001206")
FEEDBACK_CASE = uuid.UUID("0190f000-0000-7000-8000-000000001207")
TASK = uuid.UUID("0190f000-0000-7000-8000-000000001203")
TOOL_ID = uuid.UUID("0190f000-0000-7000-8000-000000001204")
REVIEW_PROMPT_VERSION_ID = uuid.UUID("0190f000-0000-7000-8000-00000000120c")
REVIEW_RUN_IDS = [
    uuid.UUID("0190f000-0000-7000-8000-000000001208"),
    uuid.UUID("0190f000-0000-7000-8000-000000001209"),
    uuid.UUID("0190f000-0000-7000-8000-00000000120a"),
    uuid.UUID("0190f000-0000-7000-8000-00000000120b"),
]
ACTOR = uuid.uuid5(uuid.NAMESPACE_URL, "phase1-semantic-read-agent")
TOOL_NAME = "order.get_status"
TASK_READ_TRAJECTORY_GOLDEN = {
    "case_id": "synthetic-workbench-order-status-read-001",
    "input": "Read the status of synthetic order SO-240918.",
    "provenance": "derived from this integration fixture; synthetic",
    "final_task_status": "succeeded",
    "tool_trace": [
        {
            "tool_name": TOOL_NAME,
            "risk": "read",
            "arguments": {"order_id": "SO-240918"},
            "execution_status": "executed",
            "verification_status": "verified",
            "permission_decision": "allowed",
        }
    ],
    "final_business_state": {"order.status": "in_production"},
}


class _Resolver:
    async def __call__(self, request: object) -> TenantContext:
        return TenantContext(
            tenant_id=TENANT,
            actor_id=ACTOR,
            actor_kind="user",
            role="support_admin",
        )


class _AuditResolver(_Resolver):
    async def __call__(self, request: object) -> TenantContext:
        return TenantContext(
            tenant_id=TENANT,
            actor_id=ACTOR,
            actor_kind="user",
            role="tenant_owner",
        )


class _ViewerResolver(_Resolver):
    async def __call__(self, request: object) -> TenantContext:
        return TenantContext(
            tenant_id=TENANT,
            actor_id=ACTOR,
            actor_kind="user",
            role="support_viewer",
        )


class _OrderExecutor:
    async def execute(
        self, tool_name: str, parameters: dict[str, object], idempotency_key: str
    ) -> dict[str, object]:
        return await _read_synthetic_order(parameters)

    async def verify_postcondition(
        self,
        tool_name: str,
        parameters: dict[str, object],
        output: dict[str, object] | None,
    ) -> bool:
        return await _verify_synthetic_order(parameters, output)


def _client(
    resolver: _Resolver | None = None, *, raise_server_exceptions: bool = False
) -> TestClient:
    import importlib

    main_mod = importlib.import_module("platform_core.main")
    app = FastAPI()
    for route in main_mod.app.router.routes:
        app.router.routes.append(route)
    app.add_middleware(TenantContextMiddleware, resolver=resolver or _Resolver())
    return TestClient(app, raise_server_exceptions=raise_server_exceptions)


def _seed() -> None:
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO tenants (id, slug, name, status) "
                "VALUES (:id, 'phase1-read-execution', 'Read task execution', 'active')"
            ),
            {"id": TENANT},
        )
        conn.execute(
            text(
                "INSERT INTO conversation_control_leases (id, tenant_id, conversation_ref_id, "
                "owner_type, owner_ref, mode, lease_version, changed_reason, updated_at) "
                "VALUES (:id, :tenant, :conversation, 'human', :owner, 'HUMAN_ACTIVE', "
                "3, 'read-test', 0)"
            ),
            {
                "id": uuid.uuid4(),
                "tenant": TENANT,
                "conversation": CONVERSATION,
                "owner": str(ACTOR),
            },
        )
        conn.execute(
            text(
                "INSERT INTO conversation_control_leases (id, tenant_id, conversation_ref_id, "
                "owner_type, owner_ref, mode, lease_version, changed_reason, updated_at) "
                "VALUES (:id, :tenant, :conversation, 'closed', :owner, 'RESOLVED', "
                "1, 'feedback-test', 0)"
            ),
            {
                "id": uuid.uuid4(),
                "tenant": TENANT,
                "conversation": FEEDBACK_CONVERSATION,
                "owner": str(ACTOR),
            },
        )
        conn.execute(
            text(
                "INSERT INTO conversation_control_leases (id, tenant_id, conversation_ref_id, "
                "owner_type, owner_ref, mode, lease_version, changed_reason, updated_at) "
                "VALUES (:id, :tenant, :conversation, 'closed', :owner, 'RESOLVED', "
                "1, 'feedback-test', 0)"
            ),
            {
                "id": uuid.uuid4(),
                "tenant": TENANT,
                "conversation": FEEDBACK_SECOND_CONVERSATION,
                "owner": str(ACTOR),
            },
        )
        now = int(time.time())
        conn.execute(
            text(
                "INSERT INTO cases (id, tenant_id, subject, description, category, priority, "
                "status, version, opened_at, resolved_at, elapsed_running_seconds, "
                "last_state_changed_at) VALUES (:id, :tenant, 'synthetic feedback case', '', "
                "'general', 'p2', 'resolved', 2, :opened, :resolved, 0, :resolved)"
            ),
            {
                "id": FEEDBACK_CASE,
                "tenant": TENANT,
                "opened": now - 9 * 24 * 3600,
                "resolved": now - 8 * 24 * 3600,
            },
        )
        for conversation, relationship in (
            (FEEDBACK_CONVERSATION, "origin"),
            (FEEDBACK_SECOND_CONVERSATION, "follow_up"),
        ):
            conn.execute(
                text(
                    "INSERT INTO case_conversations (id, tenant_id, case_id, "
                    "conversation_ref_id, relationship) VALUES (:id, :tenant, :case, "
                    ":conversation, :relationship)"
                ),
                {
                    "id": uuid.uuid4(),
                    "tenant": TENANT,
                    "case": FEEDBACK_CASE,
                    "conversation": conversation,
                    "relationship": relationship,
                },
            )
            conn.execute(
                text(
                    "INSERT INTO conversation_contacts (id, tenant_id, conversation_ref_id, "
                    "external_contact_id, channel, created_at) VALUES (:id, :tenant, "
                    ":conversation, 'synthetic-contact-01', 'web', :created)"
                ),
                {
                    "id": uuid.uuid4(),
                    "tenant": TENANT,
                    "conversation": conversation,
                    "created": now - 9 * 24 * 3600,
                },
            )
        for conversation in (FEEDBACK_CONVERSATION, FEEDBACK_SECOND_CONVERSATION):
            conn.execute(
                text(
                    "INSERT INTO conversation_turns (id, tenant_id, conversation_ref_id, role, "
                    "text_redacted, text_hash, ts, ref, source, origin, source_refs, author_ref, "
                    "created_at) VALUES (:id, :tenant, :conversation, 'agent', 'synthetic reply', "
                    ":hash, 1, '', 'agent', 'free', '[]'::jsonb, :author, 1)"
                ),
                {
                    "id": uuid.uuid4(),
                    "tenant": TENANT,
                    "conversation": conversation,
                    "hash": "1" * 64,
                    "author": str(ACTOR),
                },
            )
        review_run_fixtures = (
            ("completed", "knowledge_qa"),
            ("abstained", "sensitive"),
            ("handed_off", "human_required"),
            ("failed", "business_read"),
        )
        conn.execute(
            text(
                "INSERT INTO prompt_versions (id, tenant_id, template_name, version, body, "
                "published) VALUES (:id, :tenant, 'agent_qa', 1, 'synthetic review prompt', false)"
            ),
            {"id": REVIEW_PROMPT_VERSION_ID, "tenant": TENANT},
        )
        for index, (run_id, (status, route)) in enumerate(
            zip(REVIEW_RUN_IDS, review_run_fixtures, strict=True)
        ):
            conn.execute(
                text(
                    "INSERT INTO agent_runs (id, tenant_id, conversation_ref_id, route, status, "
                    "prompt_version_id, started_at, model_config, retrieval_config, "
                    "policy_version, code_version, "
                    "trace_id, input_hash, output_hash, token_usage, latency_ms, abstain_reason, "
                    "version) VALUES (:id, :tenant, :conversation, :route, :status, "
                    ":prompt_version, :started, "
                    "'{}'::jsonb, '{}'::jsonb, 'v1', 'test-v1', 'review-fixture', :hash, NULL, "
                    "'{}'::jsonb, 1, :reason, 0)"
                ),
                {
                    "id": run_id,
                    "tenant": TENANT,
                    "conversation": CONVERSATION,
                    "route": route,
                    "status": status,
                    "prompt_version": REVIEW_PROMPT_VERSION_ID,
                    "started": int(time.time()),
                    "hash": str(index + 1) * 64,
                    "reason": "NO_AUTHORIZED_EVIDENCE" if status == "abstained" else None,
                },
            )
        conn.execute(
            text(
                "INSERT INTO conversation_turns (id, tenant_id, conversation_ref_id, role, "
                "text_redacted, text_hash, ts, ref, source, origin, source_refs, author_ref, "
                "created_at) "
                "VALUES (:id, :tenant, :conversation, 'customer', 'synthetic same-case return', "
                ":hash, :returned, '', 'customer', '', '[]'::jsonb, NULL, :returned)"
            ),
            {
                "id": uuid.uuid4(),
                "tenant": TENANT,
                "conversation": FEEDBACK_SECOND_CONVERSATION,
                "hash": "2" * 64,
                "returned": now - 6 * 24 * 3600,
            },
        )
        schema = {
            "type": "object",
            "properties": {"order_id": {"type": "string", "minLength": 1}},
            "required": ["order_id"],
            "additionalProperties": False,
        }
        conn.execute(
            text(
                "INSERT INTO tool_definitions (id, tenant_id, name, version, risk, input_schema, "
                "output_schema, required_permissions, timeout_ms, idempotent, "
                "requires_confirmation) "
                "VALUES (:id, :tenant, :name, 1, 'read', CAST(:schema AS jsonb), '{}'::jsonb, "
                "'[\"tool.read\"]'::jsonb, 5000, true, false)"
            ),
            {"id": TOOL_ID, "tenant": TENANT, "name": TOOL_NAME, "schema": json.dumps(schema)},
        )
        slots = [
            {
                "name": "order_id",
                "value": "SO-240918",
                "origin": "customer_stated",
                "confirmed": True,
            },
            {
                "name": "tool",
                "value": TOOL_NAME,
                "origin": "server_capability",
                "selection_source": "allowlisted_candidate_schema_match",
                "confirmed": False,
            },
        ]
        conn.execute(
            text(
                "INSERT INTO conversation_tasks (id, tenant_id, conversation_ref_id, "
                "source_turn_id, task_local_key, sequence, kind, status, version, action_revision, "
                "content_hash, depends_on, condition, slots, missing_slots, created_at, "
                "updated_at) "
                "VALUES (:id, :tenant, :conversation, 'turn-read-1', 'read-0', 0, 'read', 'ready', "
                "1, 1, :hash, '[]'::jsonb, NULL, CAST(:slots AS jsonb), '[]'::jsonb, 0, 0)"
            ),
            {
                "id": TASK,
                "tenant": TENANT,
                "conversation": CONVERSATION,
                "hash": "0" * 64,
                "slots": json.dumps(slots),
            },
        )
    admin.dispose()


def _clear() -> None:
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        for statement in (
            "DELETE FROM quality_review_evidence WHERE tenant_id = :tenant",
            "DELETE FROM quality_review_decisions WHERE tenant_id = :tenant",
            "DELETE FROM quality_review_items WHERE tenant_id = :tenant",
            "DELETE FROM quality_review_batches WHERE tenant_id = :tenant",
            "DELETE FROM customer_resolution_feedback_events WHERE tenant_id = :tenant",
            "DELETE FROM tool_executions WHERE tenant_id = :tenant",
            "DELETE FROM tool_proposals WHERE tenant_id = :tenant",
            "DELETE FROM conversation_task_events WHERE tenant_id = :tenant",
            "DELETE FROM conversation_tasks WHERE tenant_id = :tenant",
            "DELETE FROM conversation_turns WHERE tenant_id = :tenant",
            "DELETE FROM case_conversations WHERE tenant_id = :tenant",
            "DELETE FROM conversation_contacts WHERE tenant_id = :tenant",
            "DELETE FROM cases WHERE tenant_id = :tenant",
            "DELETE FROM agent_runs WHERE tenant_id = :tenant",
            "DELETE FROM prompt_versions WHERE id = :prompt_version AND tenant_id = :tenant",
            "DELETE FROM outbox_events WHERE tenant_id = :tenant",
            "DELETE FROM audit_events WHERE tenant_id = :tenant",
            "DELETE FROM conversation_control_leases WHERE tenant_id = :tenant",
            "DELETE FROM tool_definitions WHERE tenant_id = :tenant",
            "DELETE FROM tenants WHERE id = :tenant",
        ):
            conn.execute(
                text(statement),
                {"tenant": TENANT, "prompt_version": REVIEW_PROMPT_VERSION_ID},
            )
    admin.dispose()


@pytest.fixture(autouse=True)
def _clean(_require_isolated_test_database: None):
    _clear()
    _seed()
    yield
    _clear()


@pytest.fixture(scope="session")
def _require_isolated_test_database() -> None:
    """Fail before any seed/cleanup SQL unless all URLs name a safe temp DB."""
    owner_url_value = os.environ.get("APP_DATABASE_URL", "")
    app_url_value = os.environ.get("APP_DATABASE_APP_URL", "")
    test_url_value = os.environ.get("APP_TEST_DATABASE_URL", "")
    admin_url_value = os.environ.get("APP_ADMIN_DATABASE_URL", "")
    if os.environ.get("APP_TEST_DATABASE_ISOLATED") != "1":
        raise RuntimeError(
            "task trajectory integration requires an explicitly isolated test database"
        )
    if not owner_url_value or not app_url_value or not test_url_value or not admin_url_value:
        raise RuntimeError(
            "task trajectory integration requires explicit owner, app, test, and admin URLs"
        )

    owner_url = make_url(owner_url_value)
    app_url = make_url(app_url_value)
    test_url = make_url(test_url_value)
    admin_url = make_url(admin_url_value)
    urls = (owner_url, app_url, test_url, admin_url)
    if owner_url.database in {None, "", "platform", "postgres"}:
        raise RuntimeError(
            "task trajectory integration refuses the development/maintenance database"
        )
    if any(url.database != owner_url.database for url in urls):
        raise RuntimeError(
            "task trajectory integration URLs must point to the same isolated database"
        )
    if app_url.username != "platform_app" or test_url.username != "platform_app":
        raise RuntimeError("APP_DATABASE_APP_URL and APP_TEST_DATABASE_URL must use platform_app")

    engine = create_engine(test_url_value)
    try:
        with engine.connect() as connection:
            role = connection.execute(
                text(
                    "SELECT current_user, r.rolsuper, r.rolbypassrls "
                    "FROM pg_roles r WHERE r.rolname = current_user"
                )
            ).one()
        if role[0] != "platform_app" or role[1] or role[2]:
            raise RuntimeError(
                "task trajectory integration requires non-owner platform_app RLS role"
            )
    finally:
        engine.dispose()


@pytest.mark.eval
def test_workbench_semantic_read_uses_gateway_and_links_verified_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Import the FastAPI routes before patching the module factory: the
    # Tool Gateway router imports `resolve_executors` by value at module load.
    # If this is the first import during a collection run, patching the module
    # first leaks the fake into every later TestClient in that process.
    import importlib

    from platform_core.agent_runtime import tasks_router
    from platform_core.tool_gateway import registry

    importlib.import_module("platform_core.main")

    async def active_capabilities(*_args: object, **_kwargs: object) -> set[str]:
        return {"orders_read"}

    async def executors(*_args: object, **_kwargs: object) -> dict[str, _OrderExecutor]:
        return {TOOL_NAME: _OrderExecutor()}

    monkeypatch.setattr(tasks_router, "active_connector_capabilities", active_capabilities)
    monkeypatch.setattr(registry, "resolve_executors", executors)

    response = _client().post(
        f"/v1/workbench/conversations/{CONVERSATION}/tasks/{TASK}/commands",
        headers={"Authorization": "Bearer pt_bootstrap_test", "Idempotency-Key": "semantic-read-1"},
        json={"command": "execute_read", "expected_version": 1, "expected_lease_version": 3},
    )

    assert response.status_code == 200, response.text
    task = response.json()["task"]
    assert task["status"] == "succeeded"
    assert task["execution_id"]
    result = next(slot for slot in task["slots"] if slot["name"] == "tool_result")
    assert result["origin"] == "verified_receipt"
    assert result["value"]["tool_name"] == TOOL_NAME
    assert result["value"]["execution_id"] == task["execution_id"]
    assert result["value"]["order_id"] == "SO-240918"
    assert result["value"]["status"] == "in_production"

    replay = _client().post(
        f"/v1/workbench/conversations/{CONVERSATION}/tasks/{TASK}/commands",
        headers={
            "Authorization": "Bearer pt_bootstrap_test",
            "Idempotency-Key": "semantic-read-1",
        },
        json={"command": "execute_read", "expected_version": 1, "expected_lease_version": 3},
    )
    assert replay.status_code == 200, replay.text
    assert replay.json()["replayed"] is True
    assert replay.json()["task"]["execution_id"] == task["execution_id"]

    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        executions = (
            conn.execute(
                text(
                    "SELECT e.id, td.name, td.risk, e.sanitized_input, e.status, "
                    "e.verification_status, p.permission_decision "
                    "FROM tool_executions e "
                    "JOIN tool_definitions td ON td.id = e.tool_definition_id "
                    "AND td.tenant_id = e.tenant_id "
                    "LEFT JOIN tool_proposals p ON p.id = e.proposal_id "
                    "AND p.tenant_id = e.tenant_id "
                    "WHERE e.tenant_id = :tenant ORDER BY e.started_at, e.id"
                ),
                {"tenant": TENANT},
            )
            .mappings()
            .all()
        )
    admin.dispose()
    assert len(executions) == 1
    execution = executions[0]
    assert execution["status"] == "executed"
    assert execution["verification_status"] == "verified"
    assert execution["permission_decision"] == "allowed"

    observed_trace = {
        "final_task_status": task["status"],
        "tool_trace": [
            {
                "tool_name": execution["name"],
                "risk": execution["risk"],
                "arguments": execution["sanitized_input"],
                "execution_status": execution["status"],
                "verification_status": execution["verification_status"],
                "permission_decision": execution["permission_decision"],
            }
        ],
        "final_business_state": {"order.status": result["value"]["status"]},
        "receipt_linked": result["value"]["execution_id"]
        == task["execution_id"]
        == str(execution["id"]),
        # This scenario issues one read command and no write request.
        "unauthorized_write_attempts": 0,
        "unauthorized_write_executions": sum(row["risk"] != "read" for row in executions),
    }
    expected_trace = {
        key: value
        for key, value in TASK_READ_TRAJECTORY_GOLDEN.items()
        if key in {"final_task_status", "tool_trace", "final_business_state"}
    }
    trajectory_score = score_task_trajectory(expected_trace, observed_trace)
    assert trajectory_score["goal_complete"] is True, trajectory_score

    # Attach all three quality layers to this same frozen synthetic case:
    # deterministic semantic route, a resolvable claim citing the verified
    # receipt, and the persisted task/tool/business trajectory above. This is
    # a local contract test, not a claim that deterministic overlap proves
    # general natural-language entailment: the QA fixture uses an exact
    # evidence span, and broader entailment remains a separate evaluation.
    from platform_core.agent_runtime.intent import classify
    from platform_core.agent_runtime.qa_path import (
        DraftAnswer,
        claim_contradiction_candidates,
        validate_citations,
    )
    from platform_core.retrieval.hybrid import RetrievedChunk

    case_id = str(TASK_READ_TRAJECTORY_GOLDEN["case_id"])
    semantic_route = classify(str(TASK_READ_TRAJECTORY_GOLDEN["input"])).route.value
    expected_route = "business_read"
    claim_text = f"Order {result['value']['order_id']} status is in_production."
    receipt_chunk = RetrievedChunk(
        chunk_id=uuid.uuid5(uuid.NAMESPACE_URL, f"{case_id}:verified-receipt"),
        document_version_id=None,
        title="Verified order receipt",
        section_path=["orders"],
        excerpt=claim_text,
        source_uri=f"tool://orders/{result['value']['order_id']}",
        score=1.0,
    )
    draft = DraftAnswer(
        text=claim_text,
        claims={0: [receipt_chunk.chunk_id]},
        claim_texts={0: claim_text},
    )
    citation_validation = validate_citations(draft, [receipt_chunk])
    contradiction_candidates = claim_contradiction_candidates(draft, [receipt_chunk])
    semantic_passed = semantic_route == expected_route
    normalized_claim = " ".join(claim_text.casefold().split())
    normalized_receipt = " ".join(receipt_chunk.excerpt.casefold().split())
    exact_evidence_span = normalized_claim in normalized_receipt
    citation_support_passed = (
        citation_validation.ok and exact_evidence_span and not contradiction_candidates
    )
    assert semantic_passed, semantic_route
    assert citation_support_passed, citation_validation

    task_quality = build_task_quality_record(
        case_id=case_id,
        components={
            "semantic_route": {
                "case_id": case_id,
                "passed": semantic_passed,
            },
            "citation_support": {
                "case_id": case_id,
                "passed": citation_support_passed,
            },
            "task_trajectory": {
                "case_id": case_id,
                "passed": trajectory_score["goal_complete"],
            },
        },
        required_components=["semantic_route", "citation_support", "task_trajectory"],
    )
    assert task_quality["status"] == "passed"
    expected_quality = build_task_quality_record(
        case_id=case_id,
        components={
            "semantic_route": {
                "case_id": case_id,
                "passed": True,
            },
            "citation_support": {
                "case_id": case_id,
                "passed": True,
            },
            "task_trajectory": {
                "case_id": case_id,
                "passed": True,
            },
        },
        required_components=["semantic_route", "citation_support", "task_trajectory"],
    )

    if _DEEPEVAL_EVAL:
        input_text = str(TASK_READ_TRAJECTORY_GOLDEN["input"])
        expected_json = json.dumps(
            {
                **expected_trace,
                "semantic_route": expected_route,
                "citation_support": {
                    "citation_resolved": True,
                    "exact_evidence_span": True,
                    "contradiction_candidate_count": 0,
                },
                "task_quality": expected_quality,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        observed_json = json.dumps(
            {
                **observed_trace,
                "semantic_route": semantic_route,
                "citation_support": {
                    "citation_resolved": citation_validation.ok,
                    "exact_evidence_span": exact_evidence_span,
                    "contradiction_candidate_count": len(contradiction_candidates),
                },
                "task_quality": task_quality,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        _attach_task_trajectory_trace(input_text, expected_json, observed_json)
        golden = Golden(
            name=str(TASK_READ_TRAJECTORY_GOLDEN["case_id"]),
            input=input_text,
            expected_output=expected_json,
            additional_metadata={
                "case_id": TASK_READ_TRAJECTORY_GOLDEN["case_id"],
                "provenance": TASK_READ_TRAJECTORY_GOLDEN["provenance"],
            },
        )
        assert_test(
            golden=golden,
            metrics=[TaskTrajectoryExactMetric(), TaskQualityRecordExactMetric()],
        )
        artifact_root = Path(os.environ.get("APP_EVAL_ARTIFACT_DIR", "tests/artifacts"))
        report_path = (
            artifact_root / "task-trajectories" / f"{TASK_READ_TRAJECTORY_GOLDEN['case_id']}.json"
        )
        report_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        report_path.write_text(
            json.dumps(
                {
                    "dataset_id": "workbench-read-trajectory-smoke-v1",
                    "case_id": TASK_READ_TRAJECTORY_GOLDEN["case_id"],
                    "provenance": TASK_READ_TRAJECTORY_GOLDEN["provenance"],
                    "score": trajectory_score,
                    "task_quality": task_quality,
                    "semantic_route": semantic_route,
                    "citation_support": {
                        "citation_resolved": citation_validation.ok,
                        "exact_evidence_span": exact_evidence_span,
                        "contradiction_candidate_count": len(contradiction_candidates),
                    },
                    "llm_as_judge": False,
                    "external_model_calls": 0,
                    "trace_store_directory": os.environ.get("DEEPEVAL_RESULTS_FOLDER"),
                    "production_quality_claim_eligible": False,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        report_path.chmod(0o600)


@pytest.mark.eval
def test_customer_resolution_feedback_is_explicit_idempotent_and_visible_to_metrics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from platform_core.evaluation import customer_feedback_service
    from platform_core.support_bridge.visitor_token import issue

    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        conn.execute(
            text(
                "UPDATE conversation_control_leases SET owner_type = 'closed', owner_ref = :owner, "
                "mode = 'RESOLVED' WHERE tenant_id = :tenant"
            ),
            {"tenant": TENANT, "owner": str(ACTOR)},
        )
    admin.dispose()

    client = _client()
    token, _expiry = issue(TENANT, FEEDBACK_CONVERSATION, "synthetic-feedback-main")
    headers = {"Authorization": f"Bearer {token}", "Idempotency-Key": "feedback-request-1"}
    timeline = client.get("/v1/support/timeline", headers={"Authorization": f"Bearer {token}"})
    assert timeline.status_code == 200, timeline.text
    assert timeline.json()["rating_eligible"] is True
    assert timeline.json()["resolution_feedback_requested"] is False

    requested = client.post("/v1/support/resolution-feedback/requested", headers=headers)
    assert requested.status_code == 200, requested.text
    request_replay = client.post("/v1/support/resolution-feedback/requested", headers=headers)
    assert request_replay.status_code == 200, request_replay.text
    assert request_replay.json()["replayed"] is True

    answer_headers = {
        "Authorization": f"Bearer {token}",
        "Idempotency-Key": "feedback-answer-1",
    }
    answer = client.post(
        "/v1/support/resolution-feedback",
        headers=answer_headers,
        json={"confirmed": True},
    )
    assert answer.status_code == 200, answer.text
    conflict = client.post(
        "/v1/support/resolution-feedback",
        headers=answer_headers,
        json={"confirmed": False},
    )
    assert conflict.status_code == 409
    state = client.get("/v1/support/timeline", headers={"Authorization": f"Bearer {token}"})
    assert state.json()["resolution_confirmation"] is True

    # An actually exposed question with no answer stays pending for the full
    # observation window, then enters the no-response count without becoming a
    # positive resolution confirmation.
    old_now = int(time.time()) - 8 * 24 * 3600
    time_module = customer_feedback_service.time
    monkeypatch.setattr(customer_feedback_service, "time", SimpleNamespace(time=lambda: old_now))
    second_token, _expiry = issue(TENANT, FEEDBACK_SECOND_CONVERSATION, "synthetic-feedback-silent")
    silent_request = client.post(
        "/v1/support/resolution-feedback/requested",
        headers={
            "Authorization": f"Bearer {second_token}",
            "Idempotency-Key": "feedback-request-silent",
        },
    )
    assert silent_request.status_code == 200, silent_request.text
    monkeypatch.setattr(customer_feedback_service, "time", time_module)

    summary = _client(_AuditResolver()).get(
        "/v1/quality/outcomes?window_seconds=2592000&confirmation_window_seconds=604800"
    )
    assert summary.status_code == 200, summary.text
    outcomes = summary.json()["outcomes"]
    assert outcomes["confirmation_requested"] == 2
    assert outcomes["customer_confirmed"] == 1
    assert outcomes["customer_no_response"] == 1
    assert outcomes["explicit_confirmation_rate_of_requests"] == 0.5
    assert outcomes["silence_marked_resolved_without_confirmation"] == 0
    assert outcomes["silence_is_confirmation"] is False
    assert outcomes["same_issue_recontacts_within_window"] == 1
    assert outcomes["mature_recontact_cohort"] == 2
    assert outcomes["same_issue_recontact_rate"] == 0.5
    assert outcomes["recontact_linkage_status"] == (
        "measured_for_verified_same_contact_same_case_links"
    )


@pytest.mark.eval
def test_online_review_batch_is_weighted_idempotent_and_finalizable() -> None:
    denied = _client(_ViewerResolver()).post(
        "/v1/quality/reviews/batches",
        headers={
            "Authorization": "Bearer pt_bootstrap_test",
            "Idempotency-Key": "quality-review-denied-role",
        },
        json={
            "window_seconds": 86_400,
            "size": 4,
            "target_prompt_version_id": str(REVIEW_PROMPT_VERSION_ID),
        },
    )
    assert denied.status_code == 403

    client = _client(_AuditResolver(), raise_server_exceptions=True)
    headers = {
        "Authorization": "Bearer pt_bootstrap_test",
        "Idempotency-Key": "quality-review-batch-1",
    }
    created = client.post(
        "/v1/quality/reviews/batches",
        headers=headers,
        json={
            "window_seconds": 86_400,
            "size": 4,
            "target_prompt_version_id": str(REVIEW_PROMPT_VERSION_ID),
        },
    )
    assert created.status_code == 200, created.text
    body = created.json()
    assert body["selected_count"] == 4
    assert body["target_prompt_version_id"] == str(REVIEW_PROMPT_VERSION_ID)
    assert set(item["stratum"] for item in body["items"]) == {
        "routine",
        "abstained",
        "handed_off",
        "failed",
    }
    batch_id = body["batch_id"]

    replay = client.post(
        "/v1/quality/reviews/batches",
        headers=headers,
        json={
            "window_seconds": 86_400,
            "size": 4,
            "target_prompt_version_id": str(REVIEW_PROMPT_VERSION_ID),
        },
    )
    assert replay.status_code == 200, replay.text
    assert replay.json()["batch_id"] == batch_id
    assert replay.json()["replayed"] is True

    for index, item in enumerate(body["items"]):
        verdict = "override" if index == 0 else "agree"
        decision = client.post(
            f"/v1/quality/reviews/batches/{batch_id}/decisions",
            headers={
                "Authorization": "Bearer pt_bootstrap_test",
                "Idempotency-Key": f"quality-review-decision-{index}",
            },
            json={
                "agent_run_id": item["agent_run_id"],
                "verdict": verdict,
                "reason_code": "wrong_route" if verdict == "override" else None,
            },
        )
        assert decision.status_code == 200, decision.text

    summary = client.get(f"/v1/quality/reviews/batches/{batch_id}")
    assert summary.status_code == 200, summary.text
    assert summary.json()["summary"]["status"] == "measured"
    assert summary.json()["summary"]["weighted_override_rate"] == 0.25

    finalize_headers = {
        "Authorization": "Bearer pt_bootstrap_test",
        "Idempotency-Key": "quality-review-finalize-1",
    }
    finalized = client.post(
        f"/v1/quality/reviews/batches/{batch_id}/finalize",
        headers=finalize_headers,
    )
    assert finalized.status_code == 200, finalized.text
    evidence = finalized.json()
    assert len(evidence["evidence_hash"]) == 64
    assert evidence["snapshot"]["summary"]["weighted_override_rate"] == 0.25
    assert evidence["snapshot"]["target_prompt_version_id"] == str(REVIEW_PROMPT_VERSION_ID)
    assert evidence["snapshot"]["versions"]["prompt_version_ids"] == [str(REVIEW_PROMPT_VERSION_ID)]

    replayed_evidence = client.post(
        f"/v1/quality/reviews/batches/{batch_id}/finalize",
        headers=finalize_headers,
    )
    assert replayed_evidence.status_code == 200, replayed_evidence.text
    assert replayed_evidence.json()["evidence_id"] == evidence["evidence_id"]
    assert replayed_evidence.json()["replayed"] is True

    late_decision = client.post(
        f"/v1/quality/reviews/batches/{batch_id}/decisions",
        headers={
            "Authorization": "Bearer pt_bootstrap_test",
            "Idempotency-Key": "quality-review-too-late",
        },
        json={
            "agent_run_id": body["items"][0]["agent_run_id"],
            "verdict": "agree",
        },
    )
    assert late_decision.status_code == 409

    promotion = client.post(
        f"/v1/prompts/{REVIEW_PROMPT_VERSION_ID}/promote",
        headers={
            "Authorization": "Bearer pt_bootstrap_test",
            "Idempotency-Key": "quality-review-threshold-promotion",
        },
        json={
            "eval_run_id": "synthetic-review-release-eval",
            "scores": [{"category": "citation", "passed": 20, "total": 20}],
            "regressions": [],
        },
    )
    assert promotion.status_code == 422, promotion.text
    assert promotion.json()["error"]["code"] == "HUMAN_REVIEW_OVERRIDE_THRESHOLD"
    active = client.get(
        "/v1/prompts/active",
        params={"template_name": "agent_qa"},
        headers={"Authorization": "Bearer pt_bootstrap_test"},
    )
    assert active.status_code == 200, active.text
    assert active.json()["active"] is None
    admin = create_engine(ADMIN_URL)
    try:
        with admin.begin() as connection:
            refusal = connection.execute(
                text(
                    "SELECT action, decision, reason_code FROM audit_events "
                    "WHERE tenant_id = :tenant AND resource_id = :version "
                    "AND action = 'prompt.promotion_blocked' ORDER BY occurred_at DESC LIMIT 1"
                ),
                {"tenant": TENANT, "version": REVIEW_PROMPT_VERSION_ID},
            ).one()
    finally:
        admin.dispose()
    assert refusal == ("prompt.promotion_blocked", "denied", "HUMAN_REVIEW_OVERRIDE_THRESHOLD")


def test_semantic_read_refuses_a_write_tool_without_gateway_proposal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import importlib

    from platform_core.agent_runtime import tasks_router
    from platform_core.tool_gateway import registry

    importlib.import_module("platform_core.main")

    async def active_capabilities(*_args: object, **_kwargs: object) -> set[str]:
        return {"orders_read"}

    async def executors(*_args: object, **_kwargs: object) -> dict[str, _OrderExecutor]:
        return {TOOL_NAME: _OrderExecutor()}

    monkeypatch.setattr(tasks_router, "active_connector_capabilities", active_capabilities)
    monkeypatch.setattr(registry, "resolve_executors", executors)
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        conn.execute(
            text(
                "UPDATE tool_definitions SET risk = 'confirmed_write' "
                "WHERE tenant_id = :tenant AND id = :tool"
            ),
            {"tenant": TENANT, "tool": TOOL_ID},
        )
    admin.dispose()

    response = _client().post(
        f"/v1/workbench/conversations/{CONVERSATION}/tasks/{TASK}/commands",
        headers={
            "Authorization": "Bearer pt_bootstrap_test",
            "Idempotency-Key": "semantic-read-write-risk",
        },
        json={"command": "execute_read", "expected_version": 1, "expected_lease_version": 3},
    )

    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "TASK_READ_TOOL_NOT_READ_ONLY"
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        proposal_count = conn.execute(
            text("SELECT count(*) FROM tool_proposals WHERE tenant_id = :tenant"),
            {"tenant": TENANT},
        ).scalar_one()
    admin.dispose()
    assert proposal_count == 0
