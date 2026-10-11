"""Durable deadline and external-attempt ceilings for release evaluation."""

from __future__ import annotations

import asyncio
import os
import time
import uuid

import pytest
from sqlalchemy import create_engine, text

from platform_core.knowledge.release_evaluator import RELEASE_EVALUATION_REQUEST_EVENT

pytestmark = pytest.mark.integration

ADMIN_URL = os.environ.get(
    "APP_ADMIN_DATABASE_URL",
    "postgresql+psycopg://platform:platform@localhost:5435/platform",
)
TENANT = uuid.UUID("0190f000-0000-7000-8000-000000000981")


def test_release_retry_reuses_first_claim_deadline_and_total_attempt_limit() -> None:
    from platform_core.execution_budget import AttemptBudgetExhausted
    from worker.release_evaluation_consumer import (
        ClaimedReleaseJob,
        _budget_for_release_claim,
        claim_release_jobs,
    )
    from worker.wiring import queue_bookkeeping_session

    admin = create_engine(ADMIN_URL)
    event_id = uuid.uuid4()
    row_id = uuid.uuid4()
    first_started_at = int(time.time()) - 30
    deadline_at = int(time.time()) + 300
    total_external_attempt_limit = 11
    with admin.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO tenants (id, slug, name, status) "
                "VALUES (:id, 'release-budget-retry', 'Release budget retry', 'active') "
                "ON CONFLICT (id) DO NOTHING"
            ),
            {"id": TENANT},
        )
        conn.execute(
            text(
                "DELETE FROM outbox_events WHERE tenant_id = :tenant AND event_type = :event_type"
            ),
            {"tenant": TENANT, "event_type": RELEASE_EVALUATION_REQUEST_EVENT},
        )
        conn.execute(
            text(
                "INSERT INTO outbox_events (id, tenant_id, event_id, event_type, event_version, "
                "aggregate_type, aggregate_id, payload, status, created_at, attempts, trace_id, "
                "first_attempt_at, deadline_at, external_attempt_limit) VALUES "
                "(:id, :tenant, :event, :event_type, 1, 'release_evaluation', :aggregate, "
                "'{}'::jsonb, 'queued', :created, 1, 'release-budget-restart', "
                ":first_started, :deadline, :attempt_limit)"
            ),
            {
                "id": row_id,
                "tenant": TENANT,
                "event": event_id,
                "event_type": RELEASE_EVALUATION_REQUEST_EVENT,
                "aggregate": str(event_id),
                "created": first_started_at,
                "first_started": first_started_at,
                "deadline": deadline_at,
                "attempt_limit": total_external_attempt_limit,
            },
        )

    async def _claim() -> ClaimedReleaseJob:
        async with queue_bookkeeping_session() as session:
            claims = await claim_release_jobs(session, batch=1)
            await session.commit()
        target = [claim for claim in claims if claim.event_id == event_id]
        assert len(target) == 1, f"expected one retry claim for the persisted job, got {claims}"
        return target[0]

    try:
        claim = asyncio.run(_claim())
        assert claim.attempt == 2
        assert claim.first_attempt_at == first_started_at
        assert claim.deadline_at == deadline_at
        assert claim.external_attempt_limit == total_external_attempt_limit

        budget, remaining = _budget_for_release_claim(claim, now=deadline_at - 60)
        assert remaining == 60
        assert budget.max_attempts == 4
        assert budget.operation_limits["model"] == 4
        with pytest.raises(TimeoutError, match="RELEASE_EVALUATION_DEADLINE_EXCEEDED"):
            _budget_for_release_claim(claim, now=deadline_at)
        with pytest.raises(AttemptBudgetExhausted):
            _budget_for_release_claim(
                ClaimedReleaseJob(
                    event_id=claim.event_id,
                    tenant_id=claim.tenant_id,
                    processing_token=uuid.uuid4(),
                    attempt=4,
                    first_attempt_at=claim.first_attempt_at,
                    deadline_at=claim.deadline_at,
                    external_attempt_limit=claim.external_attempt_limit,
                ),
                now=deadline_at - 60,
            )
    finally:
        with admin.begin() as conn:
            conn.execute(
                text("DELETE FROM outbox_events WHERE tenant_id = :tenant AND event_id = :event"),
                {"tenant": TENANT, "event": event_id},
            )
            conn.execute(text("DELETE FROM tenants WHERE id = :tenant"), {"tenant": TENANT})
        admin.dispose()
