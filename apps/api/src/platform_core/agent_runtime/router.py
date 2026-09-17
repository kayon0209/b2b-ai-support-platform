"""Agent run API (docs/api-contracts.md agent run API).

POST /v1/conversations/{conversation_ref}/agent-runs

Queues an agent run for a conversation and returns immediately.

Contract notes:
- docs/api-contracts.md requires the webhook path to respond within 300 ms
  and to never call an LLM synchronously. The same rule governs this
  endpoint: it persists the work and returns `status: "queued"`. Generation
  happens in the worker, where the pre-send lease re-check lives.
- The endpoint therefore enqueues through the same transactional inbox the
  Chatwoot webhook uses. That reuses the proven claim/ack/idempotency path
  instead of introducing a second queue with its own semantics.
- `expected_control_version` is recorded for audit. The authoritative
  compare-and-set still happens in the orchestrator immediately before
  dispatch, because the lease can move between queueing and sending.
"""

import time
from typing import Any

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import select

from platform_core.agent_runtime.models import AgentRun, RunStatus
from platform_core.api import (
    IDEMPOTENCY_KEY_REQUIRED,
    VALIDATION_FAILED,
    error_response,
    get_context,
    new_trace_id,
    ok_response,
    parse_uuid,
    require_idempotency_key,
    require_policy,
    tenant_session,
)
from platform_core.audit import service as audit_service
from platform_core.identity.usage import usage_snapshot
from platform_core.support_bridge import inbox
from platform_policy import Action

router = APIRouter(prefix="/v1/conversations", tags=["agent-runtime"])

VALID_MODES = frozenset({"customer_reply", "internal_draft"})


class AgentRunIn(BaseModel):
    trigger_message_ref: str = Field(min_length=1, max_length=255)
    mode: str = Field(default="customer_reply")
    expected_control_version: int | None = Field(default=None, ge=1)


@router.post("/{conversation_ref}/agent-runs")
async def create_agent_run(request: Request, conversation_ref: str, body: AgentRunIn) -> Any:
    ctx = get_context(request)
    if ctx is None:
        return error_response("AUTH_UNRESOLVED", "tenant context not resolved", status_code=401)

    # Queuing a run can lead to a customer-visible reply, so it is gated as
    # a case write rather than a read.
    denied = require_policy(ctx, Action.CASE_UPDATE)
    if denied is not None:
        return denied

    if body.mode not in VALID_MODES:
        return error_response(
            VALIDATION_FAILED, f"mode must be one of {sorted(VALID_MODES)}", status_code=400
        )

    idem = require_idempotency_key(request)
    if not idem:
        return error_response(
            IDEMPOTENCY_KEY_REQUIRED,
            "queuing an agent run requires an Idempotency-Key header",
            status_code=400,
        )

    try:
        conversation_ref_id = parse_uuid(conversation_ref, field="conversation_ref")
    except ValueError as exc:
        return error_response(VALIDATION_FAILED, str(exc), status_code=400)

    trace_id = new_trace_id()
    async with tenant_session(ctx) as session:
        # Quota gate. Refusing with 429 is deliberate: a caller must be able
        # to tell "declined for capacity" from "no supporting evidence",
        # which would otherwise look identical (no answer).
        usage = await usage_snapshot(session, tenant_id=ctx.tenant_id)
        if usage.over_quota:
            return error_response(
                "QUOTA_EXCEEDED",
                f"monthly agent-run quota ({usage.quota}) is exhausted",
                status_code=429,
                details={"quota": usage.quota, "runs_used": usage.runs_used},
            )

        # The inbox row is keyed by delivery id, which gives this endpoint
        # the same at-least-once + dedup semantics as a webhook delivery.
        result = await inbox.persist_inbox_event(
            session,
            tenant_id=ctx.tenant_id,
            delivery_id=f"agent-run:{idem}",
            event_type="message_created",
            raw_body=b"",
            raw_payload={
                "event": "message_created",
                "id": body.trigger_message_ref,
                "message_type": "incoming",
                "conversation": {"id": conversation_ref},
            },
        )

        if result.duplicate:
            # Idempotent replay: report the queued work, never queue twice.
            return ok_response(
                {
                    "status": RunStatus.QUEUED.value,
                    "conversation_ref": str(conversation_ref_id),
                    "duplicate": True,
                    "idempotency_key": idem,
                },
                trace_id=trace_id,
            )

        # Persist the queued run so the caller gets a real run id and the
        # lineage row exists before any model spend happens.
        run = AgentRun(
            tenant_id=ctx.tenant_id,
            conversation_ref_id=conversation_ref_id,
            route="knowledge_qa",
            status=RunStatus.QUEUED.value,
            # `started_at` is the quality dashboard's window column. Leaving it
            # unset made every run invisible to `/v1/quality/metrics`
            # (total_runs 0, everything counted as untimed).
            started_at=int(time.time()),
            model_config={"mode": body.mode},
            retrieval_config={},
            policy_version="v1",
            code_version="0.1.0",
            trace_id=trace_id,
            input_hash="",
            token_usage={},
        )
        session.add(run)
        await session.flush()

        await audit_service.record(
            session,
            ctx=ctx,
            action="agent_run.queued",
            resource_type="agent_run",
            resource_id=run.id,
            decision="completed",
            reason_code="OK",
            after={
                "mode": body.mode,
                "expected_control_version": body.expected_control_version,
                "inbox_event_id": str(result.event_id),
            },
            trace_id=trace_id,
        )
        payload = {
            "run_id": str(run.id),
            "status": RunStatus.QUEUED.value,
            "conversation_ref": str(conversation_ref_id),
            "idempotency_key": idem,
        }

    return ok_response(payload, trace_id=trace_id)


@router.get("/{conversation_ref}/agent-runs")
async def list_agent_runs(
    request: Request,
    conversation_ref: str,
    limit: int = Query(default=20, ge=1, le=100),
) -> Any:
    """Recent runs for a conversation, newest first.

    Exposes status, route and the abstention reason so the admin UI can
    explain why a run abstained or handed off without reading the database.
    """
    ctx = get_context(request)
    if ctx is None:
        return error_response("AUTH_UNRESOLVED", "tenant context not resolved", status_code=401)
    denied = require_policy(ctx, Action.CASE_READ)
    if denied is not None:
        return denied

    try:
        conversation_ref_id = parse_uuid(conversation_ref, field="conversation_ref")
    except ValueError as exc:
        return error_response(VALIDATION_FAILED, str(exc), status_code=400)

    async with tenant_session(ctx) as session:
        rows = (
            (
                await session.execute(
                    select(AgentRun)
                    .where(AgentRun.conversation_ref_id == conversation_ref_id)
                    # AgentRun carries no created_at column, so the UUIDv7
                    # primary key (time-ordered) is the ordering key.
                    .order_by(AgentRun.id.desc())
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
        items = [
            {
                "run_id": str(r.id),
                "status": r.status,
                "route": r.route,
                "abstain_reason": r.abstain_reason,
                "latency_ms": r.latency_ms,
                "trace_id": r.trace_id,
            }
            for r in rows
        ]

    return ok_response({"items": items}, trace_id=new_trace_id())
