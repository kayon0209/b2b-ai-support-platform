from __future__ import annotations

import hashlib
import json
import uuid

import pytest
from sqlalchemy.dialects import postgresql

from platform_contracts.knowledge_release_jobs import ReleaseEvaluationRequested
from platform_core.config import Settings
from platform_core.knowledge.release_evaluator import (
    RELEASE_EVALUATION_REQUEST_EVENT,
    RELEASE_POST_TEST_REQUEST_EVENT,
)
from worker import release_evaluation_consumer as consumer


class _Rows:
    def __init__(self, rows: list[object] | None = None) -> None:
        self._rows = rows or []

    def all(self) -> list[object]:
        return self._rows


class _Session:
    def __init__(self) -> None:
        self.statements: list[object] = []

    async def execute(self, statement: object) -> _Rows:
        self.statements.append(statement)
        return _Rows()


def _payload() -> dict[str, str | int]:
    values: dict[str, str | int] = {
        "schema_version": 1,
        "draft_id": str(uuid.uuid4()),
        "knowledge_space_id": str(uuid.uuid4()),
        "baseline_version_id": str(uuid.uuid4()),
        "candidate_version_id": str(uuid.uuid4()),
        "dataset_sha256": "a" * 64,
        "dataset_approval_ref": "approval-2026-09",
        "idempotency_key_sha256": "b" * 64,
    }
    values["request_hash"] = hashlib.sha256(
        json.dumps(values, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return values


@pytest.mark.asyncio
async def test_release_job_claim_reads_metadata_only_and_uses_fenced_claim() -> None:
    session = _Session()

    assert await consumer.claim_release_jobs(session) == []  # type: ignore[arg-type]

    statement = str(
        session.statements[-1].compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )
    selected_columns = statement.split(" FROM ", maxsplit=1)[0]
    assert "payload" not in selected_columns.lower()
    assert "event_id" in selected_columns
    assert "tenant_id" in selected_columns
    assert RELEASE_EVALUATION_REQUEST_EVENT in statement
    assert RELEASE_POST_TEST_REQUEST_EVENT in statement
    assert "FOR UPDATE SKIP LOCKED" in statement
    assert "processing_started_at" in statement
    assert "first_attempt_at" in statement
    assert "deadline_at" in statement
    assert "external_attempt_limit" in statement


@pytest.mark.asyncio
async def test_stale_release_claim_recovery_clears_token_and_bounds_attempts() -> None:
    session = _Session()

    reclaimed, failed = await consumer.reclaim_stale_release_jobs(session)  # type: ignore[arg-type]

    assert (reclaimed, failed) == (0, 0)
    statements = [
        str(statement.compile(dialect=postgresql.dialect())) for statement in session.statements
    ]
    assert len(statements) == 3
    for statement in statements:
        assert "processing_started_at" in statement
        assert "processing_token" in statement
    assert "deadline_at" in statements[0]
    assert "attempts" in statements[1]
    assert "attempts" in statements[2]


def test_release_job_payload_hash_is_canonical_and_strict() -> None:
    raw = _payload()
    parsed = consumer._validate_payload(
        RELEASE_EVALUATION_REQUEST_EVENT,
        raw,  # type: ignore[arg-type]
    )
    assert isinstance(parsed, ReleaseEvaluationRequested)

    with pytest.raises(ValueError, match="REQUEST_HASH_INVALID"):
        consumer._validate_payload(
            RELEASE_EVALUATION_REQUEST_EVENT,
            {**raw, "request_hash": "c" * 64},  # type: ignore[arg-type]
        )
    with pytest.raises(ValueError, match="PAYLOAD_INVALID"):
        consumer._validate_payload(
            RELEASE_EVALUATION_REQUEST_EVENT,
            {**raw, "question": "must never enter the job payload"},  # type: ignore[arg-type]
        )


def test_post_test_contract_has_no_tenant_or_customer_payload_fields() -> None:
    fields = set(consumer.ReleasePostTestRequested.model_fields)
    assert fields == {
        "schema_version",
        "evaluation_id",
        "dataset_sha256",
        "dataset_approval_ref",
        "request_hash",
    }


def test_release_external_attempts_and_deadline_survive_delivery_retries() -> None:
    token = uuid.uuid4()
    claim_one = consumer.ClaimedReleaseJob(
        event_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        processing_token=token,
        attempt=1,
        first_attempt_at=100,
        deadline_at=250,
        external_attempt_limit=10,
    )
    claim_two = consumer.ClaimedReleaseJob(
        event_id=claim_one.event_id,
        tenant_id=claim_one.tenant_id,
        processing_token=uuid.uuid4(),
        attempt=2,
        first_attempt_at=claim_one.first_attempt_at,
        deadline_at=claim_one.deadline_at,
        external_attempt_limit=claim_one.external_attempt_limit,
    )
    claim_three = consumer.ClaimedReleaseJob(
        event_id=claim_one.event_id,
        tenant_id=claim_one.tenant_id,
        processing_token=uuid.uuid4(),
        attempt=3,
        first_attempt_at=claim_one.first_attempt_at,
        deadline_at=claim_one.deadline_at,
        external_attempt_limit=claim_one.external_attempt_limit,
    )
    budget_one, remaining_one = consumer._budget_for_release_claim(claim_one, now=160)
    budget_two, remaining_two = consumer._budget_for_release_claim(claim_two, now=180)
    budget_three, _remaining_three = consumer._budget_for_release_claim(claim_three, now=200)

    per_attempt = [
        budget.operation_limits["model"] for budget in (budget_one, budget_two, budget_three)
    ]
    assert per_attempt == [4, 3, 3]
    assert sum(per_attempt) == claim_one.external_attempt_limit
    assert remaining_one == 90
    assert remaining_two == 70
    with pytest.raises(TimeoutError, match="RELEASE_EVALUATION_DEADLINE_EXCEEDED"):
        consumer._budget_for_release_claim(claim_three, now=250)


def test_release_evaluator_runtime_is_closed_when_auto_run_is_off(monkeypatch) -> None:
    monkeypatch.setattr(
        consumer,
        "get_settings",
        lambda: Settings(knowledge_evaluator_auto_run=False),
    )
    with pytest.raises(consumer.WorkerConfigurationError, match="AUTO_RUN=true"):
        consumer.build_release_evaluator_runtime()


def test_release_evaluator_has_its_own_worker_role(monkeypatch) -> None:
    from worker.runner import ROLE_RELEASE_EVALUATOR, resolve_queue

    monkeypatch.setenv("APP_WORKER_QUEUE", ROLE_RELEASE_EVALUATOR)
    assert resolve_queue([]) == ROLE_RELEASE_EVALUATOR


def test_release_answerer_config_hash_tracks_primary_model_and_endpoint() -> None:
    from worker.release_evaluator import build_release_answerer

    args = {
        "primary_model": "answer-model-v1",
        "provider_base_url": "https://provider.example/v1/",
        "timeout_seconds": 30.0,
        "max_retries": 2,
    }
    _answer, version, original = build_release_answerer(object(), **args)
    _answer, _version, another_model = build_release_answerer(
        object(), **{**args, "primary_model": "answer-model-v2"}
    )
    _answer, _version, another_endpoint = build_release_answerer(
        object(), **{**args, "provider_base_url": "https://other.example/v1"}
    )

    assert version.startswith("knowledge_qa") and "-v" in version
    assert original != another_model
    assert original != another_endpoint
