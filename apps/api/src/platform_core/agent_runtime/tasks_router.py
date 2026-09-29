"""Workbench task and copilot routes (T07 backend).

Four endpoints, matching spec §6. Three of the rules below are the reason this
file is not a thin wrapper over the store:

**A command cannot mark a tool successful.** `POST .../tasks/{id}/commands`
accepts exactly `collect_fields`, `cancel`, `handoff` and `prepare_proposal`.
There is no `complete`, no `succeed`, no `set_status` - a task reaches
`succeeded` only through `store.transition` with a verified receipt or a
recorded human action, and this router has no way to supply one. A client that
asks is refused with a 400 naming the allowed commands, because "unknown
command" is a much less actionable error than "here is what you may do".

**The lease is re-read immediately before a mutating command.** A task's
`version` protects the task; the lease protects the conversation. An agent who
was replaced between rendering the panel and clicking a button is refused, not
silently allowed to act on a conversation somebody else now owns.

**Nothing here sends anything.** `POST .../copilot/jobs` returns 200 and a job
id. Sending is the existing workbench reply path, which the agent uses after
inserting the generated text into their own draft. The two are separate
buttons in the UI for the same reason they are separate endpoints here.

Every response body is projection-shaped: task slots carry names, origins and
confirmation flags, never values (SEC-04), and a copilot body is only ever
returned to a `CASE_READ` principal on a conversation they can already see.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any, Literal

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import select as sa_select
from sqlalchemy.exc import IntegrityError

from observability_metrics import get_metrics
from platform_core.agent_runtime import chat_service
from platform_core.agent_runtime.copilot import (
    COPILOT_EVENT_TYPE,
    CopilotError,
    CopilotJobStatus,
    CopilotKind,
    apply_staleness,
    derive_job_id,
    new_job,
    should_expire,
)
from platform_core.agent_runtime.models import ConversationTurn
from platform_core.agent_runtime.semantic.modes import FLAG_COPILOT
from platform_core.agent_runtime.tasks import store as task_store
from platform_core.agent_runtime.tasks.models import CopilotDraft, StandardFlowStartRequest
from platform_core.agent_runtime.tasks.standard_flows import (
    FLAG_STANDARD_FLOW_INSTANCES,
    FLOW_EXECUTOR_UNAVAILABLE,
    FLOW_INTERNAL_CASE_ONLY,
    get_standard_flow,
)
from platform_core.agent_runtime.tasks.state_machine import (
    TaskKind,
    TaskStatus,
    TaskTransitionError,
)
from platform_core.api import (
    CASE_NOT_FOUND,
    VALIDATION_FAILED,
    error_response,
    get_context,
    new_trace_id,
    ok_response,
    require_policy,
    require_write_idempotency,
    tenant_session,
)
from platform_core.audit import service as audit_service
from platform_core.cases.service import verified_account_for_conversation
from platform_core.evaluation.pii import redact_text, should_withhold_value
from platform_core.identity import lease_service
from platform_core.identity.control_lease import LeaseConflict
from platform_core.identity.tenant_context import TenantContext
from platform_core.integrations.readiness import active_connector_capabilities
from platform_core.knowledge import flag_service
from platform_core.outbox_service import enqueue, latest_status_for_aggregate
from platform_policy import Action

router = APIRouter(prefix="/v1/workbench", tags=["workbench-tasks"])

# The complete command vocabulary. Anything else is refused, and the error
# names these - see the module docstring for why there is no completion
# command here.
CommandName = Literal[
    "collect_fields", "cancel", "handoff", "prepare_proposal", "query_order_status"
]
ALLOWED_COMMANDS: tuple[str, ...] = (
    "collect_fields",
    "cancel",
    "handoff",
    "prepare_proposal",
    "query_order_status",
)
INTERNAL_CASE_FLOWS = frozenset(
    {"invoice_application", "repair_quality_intake", "technical_escalation"}
)
DEMO_PRODUCT_FLOWS = frozenset({"repair_quality_intake", "technical_escalation"})
DEMO_ORDER_FLOWS = frozenset({"order_status"})

MAX_COLLECTED_FIELDS = 20
MAX_FIELD_VALUE_CHARS = 200


class TaskCommandIn(BaseModel):
    command: str = Field(max_length=32)
    expected_version: int = Field(ge=1)
    expected_lease_version: int = Field(ge=1)
    fields: dict[str, str] = Field(default_factory=dict)


class StandardFlowStartIn(BaseModel):
    model_config = {"extra": "forbid"}

    flow_key: str = Field(min_length=1, max_length=63)
    expected_lease_version: int = Field(ge=1)


class CopilotJobIn(BaseModel):
    kind: Literal["summary", "reply"]
    timeline_revision: int = Field(ge=0)
    lease_version: int = Field(ge=0)
    instructions: str = Field(default="", max_length=2000)
    task_id: uuid.UUID | None = None
    source_turn_ids: list[str] = Field(default_factory=list, max_length=20)


def _auth(request: Request, action: Action) -> tuple[Any, Any]:
    ctx = get_context(request)
    if ctx is None:
        return None, error_response(
            "AUTH_UNRESOLVED", "tenant context not resolved", status_code=401
        )
    return ctx, require_policy(ctx, action)


def _demo_business_flows_enabled() -> bool:
    from platform_core.config import get_settings

    settings = get_settings()
    return (
        settings.environment in ("local", "test")
        and settings.business_api_adapter.strip().lower() == "demo"
    )


def _can_prepare_flow_proposal(flow_key: str | None, ctx: TenantContext) -> bool:
    if flow_key not in INTERNAL_CASE_FLOWS:
        return False
    if flow_key in DEMO_PRODUCT_FLOWS and not _demo_business_flows_enabled():
        return False
    return require_policy(ctx, Action.TOOL_WRITE_CONFIRMED) is None


def _can_query_order_flow(
    flow_key: str | None, ctx: TenantContext, *, orders_read_available: bool
) -> bool:
    return (
        flow_key in DEMO_ORDER_FLOWS
        and _demo_business_flows_enabled()
        and orders_read_available
        and require_policy(ctx, Action.TOOL_READ) is None
    )


def _flow_blocked_reason(flow_key: str, *, orders_read_available: bool) -> str | None:
    if flow_key in INTERNAL_CASE_FLOWS:
        return FLOW_INTERNAL_CASE_ONLY
    if flow_key in DEMO_ORDER_FLOWS and _demo_business_flows_enabled() and orders_read_available:
        return None
    return FLOW_EXECUTOR_UNAVAILABLE


def _task_out(
    row: Any,
    *,
    can_prepare_flow_proposal: bool = False,
    can_query_order_flow: bool = False,
) -> dict[str, Any]:
    """The task projection.

    `slots` holds names, origins and confirmation flags. The value of a
    delivery address or a tax id is deliberately not here - it lives in the
    transcript, which is access-controlled and retention-bounded, and this row
    is read by a list endpoint and by the audit path.
    """
    template = get_standard_flow(row.flow_key) if row.flow_key else None
    return {
        "task_id": str(row.id),
        "local_key": row.task_local_key,
        "kind": row.kind,
        "status": row.status,
        "sequence": row.sequence,
        "version": row.version,
        "action_revision": row.action_revision,
        "slots": _sanitize_task_slots(row.flow_key, row.slots),
        "missing_slots": row.missing_slots,
        "depends_on": row.depends_on,
        "condition": row.condition,
        "blocked_reason": row.blocked_reason,
        "proposal_id": str(row.proposal_id) if row.proposal_id else None,
        "execution_id": str(row.execution_id) if row.execution_id else None,
        "source_turn_id": row.source_turn_id,
        "flow_key": row.flow_key,
        "flow_version": row.flow_version,
        "flow_title": template.title if template else None,
        "flow_can_prepare_proposal": bool(
            row.flow_key in INTERNAL_CASE_FLOWS and can_prepare_flow_proposal
        ),
        "flow_can_query_order": bool(row.flow_key in DEMO_ORDER_FLOWS and can_query_order_flow),
        "updated_at": row.updated_at,
    }


def _flow_sensitive_fields(flow_key: str | None) -> set[str]:
    if not flow_key:
        return set()
    template = get_standard_flow(flow_key)
    if template is None:
        return set()
    return {
        field.name
        for field in (*template.required_fields, *template.optional_fields)
        if field.sensitive
    }


def _sanitize_task_slots(flow_key: str | None, slots: Any) -> list[dict[str, Any]]:
    """Remove sensitive values and redact PII from slots, including old rows.

    The projection is a second privacy boundary: historical rows may have
    been written before collection redaction was made comprehensive.
    """
    sensitive_fields = _flow_sensitive_fields(flow_key)
    safe_slots: list[dict[str, Any]] = []
    for raw_slot in slots if isinstance(slots, list) else []:
        if not isinstance(raw_slot, dict):
            continue
        slot = dict(raw_slot)
        name = str(slot.get("name", ""))
        origin = slot.get("origin")
        sensitive = (
            name in sensitive_fields
            or name.lower() in SENSITIVE_FIELD_NAMES
            or should_withhold_value(name)
        )
        if sensitive:
            if "value" in slot:
                slot.pop("value", None)
                slot["value_withheld"] = True
        elif isinstance(slot.get("value"), str) and origin in {
            "customer_stated",
            "agent_collected",
        }:
            slot["value"], _redaction_count = redact_text(slot["value"])
        safe_slots.append(slot)
    return safe_slots


# --- tasks ------------------------------------------------------------------


@router.get("/conversations/{conversation_ref}/tasks")
async def list_conversation_tasks(
    request: Request,
    conversation_ref: uuid.UUID,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> Any:
    """List a conversation's tasks with their state and blockers."""
    ctx, denied = _auth(request, Action.CASE_READ)
    if denied is not None:
        return denied
    assert ctx is not None
    async with tenant_session(ctx) as session:
        rows = await task_store.list_tasks(
            session,
            tenant_id=ctx.tenant_id,
            conversation_ref_id=conversation_ref,
            limit=limit,
            offset=offset,
        )
        connector_capabilities = await active_connector_capabilities(
            session, tenant_id=ctx.tenant_id
        )
        orders_read_available = "orders_read" in connector_capabilities
    return ok_response(
        {
            "conversation_ref": str(conversation_ref),
            "demo_presales_enabled": (
                _demo_business_flows_enabled() and require_policy(ctx, Action.TOOL_READ) is None
            ),
            "items": [
                _task_out(
                    row,
                    can_prepare_flow_proposal=_can_prepare_flow_proposal(row.flow_key, ctx),
                    can_query_order_flow=_can_query_order_flow(
                        row.flow_key, ctx, orders_read_available=orders_read_available
                    ),
                )
                for row in rows
            ],
            "limit": limit,
            "offset": offset,
        }
    )


@router.get("/conversations/{conversation_ref}/demo-presales/{product_ref}")
async def demo_presales_evidence(
    request: Request,
    conversation_ref: uuid.UUID,
    product_ref: str,
) -> Any:
    """Read local/test synthetic product, stock and quote evidence for a human.

    The endpoint never writes a CRM opportunity, emits no customer-facing
    quote, and derives the account only from the conversation's linked Case.
    """
    ctx, denied = _auth(request, Action.CASE_READ)
    if denied is not None:
        return denied
    assert ctx is not None
    tool_denied = require_policy(ctx, Action.TOOL_READ)
    if tool_denied is not None:
        return tool_denied
    if not _demo_business_flows_enabled():
        return error_response(
            "DEMO_PRESALES_UNAVAILABLE",
            "synthetic pre-sales evidence is enabled only in local/test Demo mode",
            status_code=404,
        )
    product_ref = product_ref.strip()
    if not product_ref or len(product_ref) > 255:
        return error_response(VALIDATION_FAILED, "invalid product reference", status_code=400)

    trace_id = new_trace_id()
    from platform_core.integrations.canonical_business import BusinessAdapterError

    try:
        async with tenant_session(ctx) as session:
            account_id = await verified_account_for_conversation(
                session,
                tenant_id=ctx.tenant_id,
                conversation_ref_id=conversation_ref,
            )
            if account_id is None:
                return error_response(
                    "DEMO_PRESALES_ACCOUNT_UNVERIFIED",
                    "link this conversation to one tenant account before viewing demo evidence",
                    status_code=409,
                    trace_id=trace_id,
                )
            from platform_core.identity.profile import business_system_ref_for_account
            from platform_core.integrations.demo_canonical_business import (
                DemoCanonicalBusinessAdapter,
                synthetic_demo_authority_bindings,
            )
            from platform_core.integrations.demo_presales import build_demo_presales_evidence

            account_ref = await business_system_ref_for_account(
                session,
                tenant_id=ctx.tenant_id,
                account_id=account_id,
                system_key="business_api",
            )
            if account_ref is None:
                return error_response(
                    "DEMO_PRESALES_ACCOUNT_UNVERIFIED",
                    "the linked account has no Demo business-system reference",
                    status_code=409,
                    trace_id=trace_id,
                )
            now = datetime.now(UTC)
            try:
                adapter = DemoCanonicalBusinessAdapter()
                evidence = await build_demo_presales_evidence(
                    adapter,
                    bindings=synthetic_demo_authority_bindings(ctx.tenant_id, now=now),
                    product_ref=product_ref,
                    expected_account_ref=account_ref,
                    as_of=now,
                )
            except BusinessAdapterError as exc:
                if exc.code == "DEMO_PRESALES_RECORD_UNAVAILABLE":
                    code = "DEMO_PRESALES_RECORD_UNAVAILABLE"
                    status_code = 404
                elif exc.code.startswith("BUSINESS_OWNERSHIP_"):
                    code = "DEMO_PRESALES_ACCOUNT_UNVERIFIED"
                    status_code = 409
                else:
                    code = "DEMO_PRESALES_SOURCE_UNVERIFIED"
                    status_code = 409
                return error_response(
                    code,
                    "the Demo source did not verify fresh, matching product, inventory "
                    "and quote facts",
                    status_code=status_code,
                    trace_id=trace_id,
                )
            await audit_service.record(
                session,
                ctx=ctx,
                action="demo_presales.evidence_viewed",
                resource_type="conversation",
                resource_id=conversation_ref,
                metadata={
                    "source": "demo",
                    "source_version": "demo-fixture-v1",
                    "synthetic_records_verified": 3,
                },
                trace_id=trace_id,
            )
    except BusinessAdapterError:
        return error_response(
            "DEMO_PRESALES_UNAVAILABLE",
            "the synthetic authority refused this evidence request",
            status_code=503,
            retryable=True,
            trace_id=trace_id,
        )
    return ok_response(
        {
            "evidence": evidence.model_dump(mode="json"),
            "demo_only": True,
        },
        trace_id=trace_id,
    )


def _request_digest(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@router.post("/conversations/{conversation_ref}/standard-flows/tasks")
async def start_standard_flow(
    request: Request,
    conversation_ref: uuid.UUID,
    body: StandardFlowStartIn,
) -> Any:
    """Create an operator-owned task bound to the latest customer turn.

    A catalog instance is intentionally parked in `manual_flow`, which is not
    schedulable. A flow-specific executor must be implemented and authorized
    before any template can become ready or produce a Tool Gateway proposal.
    """
    ctx, denied = _auth(request, Action.CASE_UPDATE)
    if denied is not None:
        return denied
    assert ctx is not None
    missing = require_write_idempotency(request, Action.CASE_UPDATE)
    if missing is not None:
        return missing
    if ctx.actor_id is None:
        return error_response(VALIDATION_FAILED, "an identified agent is required", status_code=400)
    template = get_standard_flow(body.flow_key)
    if template is None:
        return error_response("FLOW_NOT_FOUND", "unknown standard flow", status_code=404)

    idem_raw = request.headers["Idempotency-Key"]
    idem_hash = hashlib.sha256(idem_raw.encode("utf-8")).hexdigest()
    request_hash = _request_digest(
        {"flow_key": template.key, "expected_lease_version": body.expected_lease_version}
    )
    actor_ref = str(ctx.actor_id)
    trace_id = new_trace_id()
    try:
        async with tenant_session(ctx) as session:
            # Locking the lease row makes ownership and task creation one
            # serialized decision with concurrent operator reassignment.
            lease = await lease_service.lease_snapshot(
                session,
                tenant_id=ctx.tenant_id,
                conversation_ref_id=conversation_ref,
                for_update=True,
            )
            if lease is None:
                return error_response(CASE_NOT_FOUND, "conversation not found", status_code=404)
            if lease.owner_type != "human" or lease.owner_ref != actor_ref:
                return error_response(
                    "LEASE_NOT_OWNED",
                    "only the current human owner may start a standard flow",
                    status_code=409,
                    trace_id=trace_id,
                )
            if lease.expires_at is not None and lease.expires_at <= int(time.time()):
                return error_response(
                    "LEASE_EXPIRED", "conversation lease expired", status_code=409
                )
            connector_capabilities = await active_connector_capabilities(
                session, tenant_id=ctx.tenant_id
            )
            orders_read_available = "orders_read" in connector_capabilities
            receipt = (
                await session.execute(
                    sa_select(StandardFlowStartRequest).where(
                        StandardFlowStartRequest.tenant_id == ctx.tenant_id,
                        StandardFlowStartRequest.conversation_ref_id == conversation_ref,
                        StandardFlowStartRequest.idempotency_key_hash == idem_hash,
                    )
                )
            ).scalar_one_or_none()
            if receipt is not None:
                if receipt.request_hash != request_hash:
                    return error_response(
                        "IDEMPOTENCY_CONFLICT",
                        "key was used for a different standard flow request",
                        status_code=409,
                        trace_id=trace_id,
                    )
                replay = await task_store.get_task(
                    session, tenant_id=ctx.tenant_id, task_id=receipt.task_id
                )
                if replay is None or replay.conversation_ref_id != conversation_ref:
                    return error_response(CASE_NOT_FOUND, "flow task not found", status_code=404)
                get_metrics().workbench_standard_flow_actions_total.labels(
                    flow_key=template.key, action="start", outcome="replayed"
                ).inc()
                return ok_response(
                    {
                        "task": _task_out(
                            replay,
                            can_prepare_flow_proposal=_can_prepare_flow_proposal(
                                replay.flow_key, ctx
                            ),
                            can_query_order_flow=_can_query_order_flow(
                                replay.flow_key,
                                ctx,
                                orders_read_available=orders_read_available,
                            ),
                        ),
                        "replayed": True,
                    },
                    trace_id=trace_id,
                )

            if lease.lease_version != body.expected_lease_version:
                return error_response(
                    "LEASE_CONFLICT",
                    f"lease version moved to {lease.lease_version}",
                    status_code=409,
                    trace_id=trace_id,
                )

            enabled = await flag_service.evaluate(
                session,
                flag_key=FLAG_STANDARD_FLOW_INSTANCES,
                tenant_id=ctx.tenant_id,
                default=False,
            )
            if not enabled.enabled:
                return error_response(
                    "STANDARD_FLOW_INSTANCES_DISABLED",
                    "standard flow task creation is not enabled for this tenant",
                    status_code=403,
                    trace_id=trace_id,
                )

            latest_turn = (
                await session.execute(
                    sa_select(ConversationTurn)
                    .where(
                        ConversationTurn.tenant_id == ctx.tenant_id,
                        ConversationTurn.conversation_ref_id == conversation_ref,
                        ConversationTurn.role == "customer",
                    )
                    .order_by(
                        ConversationTurn.ts.desc(),
                        ConversationTurn.created_at.desc(),
                        ConversationTurn.id.desc(),
                    )
                    .limit(1)
                )
            ).scalar_one_or_none()
            if latest_turn is None:
                return error_response(
                    "FLOW_SOURCE_TURN_REQUIRED",
                    "a standard flow must be started from a customer turn",
                    status_code=409,
                    trace_id=trace_id,
                )

            local_key = f"standard-flow:{template.key}:v{template.version}"
            customer_fields = [
                field.name
                for field in template.required_fields
                if field.source == "customer" or field.lookup_input
            ]
            task_kind = (
                TaskKind.READ
                if template.required_read_tools and not template.allowed_confirmed_write_tools
                else TaskKind.WRITE
            )
            task_id = uuid.uuid4()
            # The locked lease row serializes starts for this conversation.
            # Create/get the stable flow task first so the append-only receipt
            # can be inserted with its final task id and needs no UPDATE grant.
            task, _created = await task_store.create_or_get(
                session,
                tenant_id=ctx.tenant_id,
                conversation_ref_id=conversation_ref,
                source_turn_id=str(latest_turn.id),
                task_local_key=local_key,
                kind=task_kind,
                status=TaskStatus.MANUAL_FLOW,
                slots=[],
                missing_slots=customer_fields,
                blocked_reason=_flow_blocked_reason(
                    template.key,
                    orders_read_available=orders_read_available,
                ),
                sequence=0,
                trace_id=trace_id,
                flow_key=template.key,
                flow_version=template.version,
                actor_type="human",
                actor_ref=actor_ref,
                reason_code="STANDARD_FLOW_STARTED",
                task_id=task_id,
            )
            start_receipt = StandardFlowStartRequest(
                id=uuid.uuid4(),
                tenant_id=ctx.tenant_id,
                conversation_ref_id=conversation_ref,
                task_id=task.id,
                idempotency_key_hash=idem_hash,
                request_hash=request_hash,
                created_by=ctx.actor_id,
                created_at=int(time.time()),
            )
            session.add(start_receipt)
            await session.flush()
            await audit_service.record(
                session,
                ctx=ctx,
                action=(
                    "conversation.standard_flow_started"
                    if _created
                    else "conversation.standard_flow_start_reused"
                ),
                resource_type="conversation_task",
                resource_id=task.id,
                metadata={
                    "flow_key": template.key,
                    "flow_version": template.version,
                    "conversation_ref": str(conversation_ref),
                    "source_turn_id": str(latest_turn.id),
                },
                trace_id=trace_id,
            )
            if _created:
                await enqueue(
                    session,
                    tenant_id=ctx.tenant_id,
                    event_type="conversation_task.updated",
                    aggregate_type="conversation_task",
                    aggregate_id=str(task.id),
                    payload={
                        "task_id": str(task.id),
                        "conversation_ref": str(conversation_ref),
                        "flow_key": template.key,
                        "version": task.version,
                    },
                    trace_id=trace_id,
                )
            get_metrics().workbench_standard_flow_actions_total.labels(
                flow_key=template.key,
                action="start",
                outcome="created" if _created else "reused",
            ).inc()
    except TaskConflict_ as exc:
        return error_response(exc.code, exc.detail, status_code=409, trace_id=trace_id)
    except TaskTransitionError as exc:
        return error_response(exc.code, exc.detail, status_code=409, trace_id=trace_id)

    return ok_response(
        {
            "task": _task_out(
                task,
                can_prepare_flow_proposal=_can_prepare_flow_proposal(task.flow_key, ctx),
                can_query_order_flow=_can_query_order_flow(
                    task.flow_key,
                    ctx,
                    orders_read_available=orders_read_available,
                ),
            ),
            "replayed": not _created,
        },
        trace_id=trace_id,
    )


@router.post("/conversations/{conversation_ref}/tasks/{task_id}/commands")
async def command_conversation_task(
    request: Request,
    conversation_ref: uuid.UUID,
    task_id: uuid.UUID,
    body: TaskCommandIn,
) -> Any:
    """Apply one operator command to one task.

    `CASE_UPDATE` plus current human ownership. A visitor cannot reach this:
    the action is not in the visitor's grant set, and the ownership check
    below is a second, independent refusal.
    """
    ctx, denied = _auth(request, Action.CASE_UPDATE)
    if denied is not None:
        return denied
    assert ctx is not None
    missing = require_write_idempotency(request, Action.CASE_UPDATE)
    if missing is not None:
        return missing
    idempotency_key = request.headers["Idempotency-Key"]
    idempotency_key_hash = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()
    if ctx.actor_id is None:
        return error_response(VALIDATION_FAILED, "an identified agent is required", status_code=400)
    if body.command not in ALLOWED_COMMANDS:
        # Named explicitly: "unknown command 42" is not actionable, and the
        # absence of a completion command here is deliberate, not an omission.
        return error_response(
            VALIDATION_FAILED,
            f"command must be one of {', '.join(ALLOWED_COMMANDS)}",
            status_code=400,
        )
    if len(body.fields) > MAX_COLLECTED_FIELDS:
        return error_response(
            VALIDATION_FAILED,
            f"at most {MAX_COLLECTED_FIELDS} fields may be collected at once",
            status_code=400,
        )
    for name, value in body.fields.items():
        if len(name) > 64 or len(value) > MAX_FIELD_VALUE_CHARS:
            return error_response(
                VALIDATION_FAILED,
                f"field {name!r} exceeds the accepted length",
                status_code=400,
            )

    actor_ref = str(ctx.actor_id)
    trace_id = new_trace_id()
    request_hash = hashlib.sha256(
        json.dumps(
            {
                "tenant_id": str(ctx.tenant_id),
                "actor_id": str(ctx.actor_id),
                "command": body.model_dump(mode="json"),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    try:
        async with tenant_session(ctx) as session:
            # Ownership first, and re-read inside this transaction: the panel
            # the agent acted on may be a version behind.
            lease = await lease_service.lease_snapshot(
                session,
                tenant_id=ctx.tenant_id,
                conversation_ref_id=conversation_ref,
                for_update=True,
            )
            if lease is None:
                return error_response(CASE_NOT_FOUND, "conversation not found", status_code=404)
            if lease.owner_type != "human" or lease.owner_ref != actor_ref:
                return error_response(
                    "LEASE_NOT_OWNED",
                    "only the current human owner may command a task",
                    status_code=409,
                )
            if lease.expires_at is not None and lease.expires_at <= int(time.time()):
                return error_response(
                    "LEASE_EXPIRED", "conversation lease expired", status_code=409
                )
            if lease.lease_version != body.expected_lease_version:
                return error_response(
                    "LEASE_CONFLICT",
                    f"lease version moved to {lease.lease_version}",
                    status_code=409,
                )

            task = await task_store.get_task(session, tenant_id=ctx.tenant_id, task_id=task_id)
            if task is None or task.conversation_ref_id != conversation_ref:
                # 404 rather than 403: a task id from another conversation must
                # not be distinguishable from one that does not exist.
                return error_response(CASE_NOT_FOUND, "task not found", status_code=404)
            connector_capabilities = await active_connector_capabilities(
                session, tenant_id=ctx.tenant_id
            )
            orders_read_available = "orders_read" in connector_capabilities

            if task.flow_key in INTERNAL_CASE_FLOWS and body.command == "prepare_proposal":
                proposal_denied = require_policy(ctx, Action.TOOL_WRITE_CONFIRMED)
                if proposal_denied is not None:
                    return proposal_denied
            if body.command == "query_order_status":
                query_denied = require_policy(ctx, Action.TOOL_READ)
                if query_denied is not None:
                    return query_denied

            prior_request_hash = await task_store.command_request_hash(
                session,
                tenant_id=ctx.tenant_id,
                task_id=task.id,
                idempotency_key_hash=idempotency_key_hash,
            )
            if prior_request_hash is not None:
                if prior_request_hash != request_hash:
                    return error_response(
                        "IDEMPOTENCY_CONFLICT",
                        "Idempotency-Key was already used for a different task command",
                        status_code=409,
                    )
                return ok_response(
                    {
                        "task": _task_out(
                            task,
                            can_prepare_flow_proposal=_can_prepare_flow_proposal(
                                task.flow_key, ctx
                            ),
                            can_query_order_flow=_can_query_order_flow(
                                task.flow_key,
                                ctx,
                                orders_read_available=orders_read_available,
                            ),
                        ),
                        "replayed": True,
                    },
                    trace_id=trace_id,
                )

            if body.command == "query_order_status":
                updated = await _run_demo_order_query(
                    session,
                    ctx=ctx,
                    conversation_ref=conversation_ref,
                    task=task,
                    expected_version=body.expected_version,
                    actor_ref=actor_ref,
                    trace_id=trace_id,
                    idempotency_key_hash=idempotency_key_hash,
                    request_hash=request_hash,
                )
            else:
                command = await _build_command(
                    session,
                    ctx=ctx,
                    conversation_ref=conversation_ref,
                    task=task,
                    body=body,
                    actor_ref=actor_ref,
                    trace_id=trace_id,
                )
                if command is None:
                    return error_response(
                        "TASK_COMMAND_REFUSED",
                        "this command does not apply to the task's current state",
                        status_code=409,
                    )
                command = replace(
                    command,
                    idempotency_key_hash=idempotency_key_hash,
                    request_hash=request_hash,
                )

                if (
                    task.proposal_id is not None
                    and TaskStatus(task.status) is TaskStatus.AWAITING_CONFIRMATION
                    and command.target in (TaskStatus.CANCELLED, TaskStatus.NEEDS_HUMAN)
                ):
                    from platform_core.tool_gateway.gateway import ToolGateway, ToolGatewayError

                    try:
                        await ToolGateway(session, {}).withdraw(
                            tenant_id=ctx.tenant_id,
                            proposal_id=task.proposal_id,
                        )
                    except ToolGatewayError as exc:
                        raise TaskCommandRefused(
                            "TASK_PROPOSAL_NOT_WITHDRAWABLE",
                            "the proposal is already executing or has a final result",
                        ) from exc
                    await audit_service.record(
                        session,
                        ctx=ctx,
                        action="tool_proposal.withdrawn",
                        resource_type="tool_proposal",
                        resource_id=task.proposal_id,
                        metadata={"task_id": str(task.id), "command": body.command},
                        trace_id=trace_id,
                    )
                    command = replace(command, proposal_withdrawn=True)

                updated = await task_store.transition(
                    session, tenant_id=ctx.tenant_id, task=task, command=command
                )
            await enqueue(
                session,
                tenant_id=ctx.tenant_id,
                event_type="conversation_task.updated",
                aggregate_type="conversation_task",
                aggregate_id=str(task.id),
                payload={
                    "task_id": str(task.id),
                    "conversation_ref": str(conversation_ref),
                    "command": body.command,
                    "version": updated.version,
                },
                trace_id=trace_id,
            )
            if task.flow_key and body.command != "query_order_status":
                get_metrics().workbench_standard_flow_actions_total.labels(
                    flow_key=task.flow_key, action=body.command, outcome="updated"
                ).inc()
    except TaskCommandRefused as exc:
        return error_response(exc.code, exc.detail, status_code=409, trace_id=trace_id)
    except TaskConflict_ as exc:
        return error_response(exc.code, exc.detail, status_code=409, trace_id=trace_id)
    except LeaseConflict as exc:
        return error_response("LEASE_CONFLICT", str(exc), status_code=409, trace_id=trace_id)
    except TaskTransitionError as exc:
        return error_response(exc.code, exc.detail, status_code=409, trace_id=trace_id)

    return ok_response(
        {
            "task": _task_out(
                updated,
                can_prepare_flow_proposal=_can_prepare_flow_proposal(updated.flow_key, ctx),
                can_query_order_flow=_can_query_order_flow(
                    updated.flow_key,
                    ctx,
                    orders_read_available=orders_read_available,
                ),
            ),
            "replayed": False,
        },
        trace_id=trace_id,
    )


class TaskCommandRefused(Exception):
    """The command does not apply to this task. 409, with a reason.

    A distinct type from `TaskTransitionError` because the two mean different
    things to a client: a transition error is "the task moved on", a refusal is
    "you asked for something this task was never waiting for".
    """

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


# Slot names whose value is never written into a task row. The value lives in
# the conversation turn written alongside it; the task records that the field
# was answered, by whom, and when. Mirrors `tasks.planner.SENSITIVE_SLOT_NAMES`
# and is re-declared here because the router is the boundary that receives the
# value and must decide before it reaches the store.
SENSITIVE_FIELD_NAMES = frozenset(
    {
        "address",
        "street",
        "city",
        "postal_code",
        "tax_id",
        "phone",
        "email",
        "bank_account",
        "id_card",
    }
)


def _merge_slots(
    existing: list[dict[str, Any]], collected: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Replace a slot by name, preserving the rest and their order."""
    by_name = {s.get("name"): s for s in collected}
    merged: list[dict[str, Any]] = []
    for slot in existing:
        replacement = by_name.get(slot.get("name"))
        merged.append(replacement if replacement is not None else slot)
    for slot in collected:
        if slot.get("name") not in {s.get("name") for s in merged}:
            merged.append(slot)
    return merged


async def _persist_collected(
    ctx: TenantContext,
    fields: dict[str, str],
    *,
    verified_fields: dict[str, dict[str, Any]] | None = None,
    flow_key: str | None = None,
) -> list[dict[str, Any]]:
    """Persist operator-collected values with their actual provenance.

    The task's append-only transition records the operator actor and trace.
    These values are not customer-authored turns, so no customer message is
    manufactured. Sensitive values remain withheld; ordinary values can be
    reviewed in the subsequent Tool Gateway proposal.
    """

    if ctx.actor_id is None:
        raise ValueError("an identified agent is required to collect task fields")

    slots: list[dict[str, Any]] = []
    schema_sensitive_fields = _flow_sensitive_fields(flow_key)
    for name, value in fields.items():
        verification = (verified_fields or {}).get(name)
        if verification is not None:
            slots.append(
                {
                    "name": name,
                    "value": str(verification["record_ref"]),
                    "origin": "verified_business_record",
                    "confirmed": True,
                    "verification_source": "demo",
                    "authority_version": str(verification["authority_version"]),
                    "collected_by": str(ctx.actor_id),
                    "collected_at": int(time.time()),
                }
            )
            continue
        sensitive = (
            name in schema_sensitive_fields
            or name.lower() in SENSITIVE_FIELD_NAMES
            or should_withhold_value(name)
        )
        slot: dict[str, Any] = {
            "name": name,
            "origin": "agent_collected",
            "confirmed": False,
            "collected_by": str(ctx.actor_id),
            "collected_at": int(time.time()),
        }
        if sensitive:
            slot["value_withheld"] = True
        else:
            safe_value, _redaction_count = redact_text(value)
            slot["value"] = safe_value
        slots.append(slot)
    return slots


async def _verify_demo_flow_fields(
    session: Any,
    *,
    tenant_id: uuid.UUID,
    conversation_ref: uuid.UUID,
    task: Any,
    fields: dict[str, str],
    lookup_field_names: set[str],
) -> dict[str, dict[str, Any]]:
    """Resolve customer-supplied identifiers through the explicit local demo."""
    if (
        task.flow_key not in DEMO_PRODUCT_FLOWS | DEMO_ORDER_FLOWS
        or not _demo_business_flows_enabled()
    ):
        raise TaskCommandRefused(
            "FLOW_BUSINESS_VERIFICATION_UNAVAILABLE",
            "this flow needs an authorized business-record adapter",
        )
    account_id = await verified_account_for_conversation(
        session,
        tenant_id=tenant_id,
        conversation_ref_id=conversation_ref,
    )
    if account_id is None:
        raise TaskCommandRefused(
            "FLOW_ACCOUNT_UNVERIFIED",
            "link this conversation to one tenant account before verifying its product",
        )
    from platform_core.identity.profile import business_system_ref_for_account
    from platform_core.integrations.demo_erp import (
        verify_demo_order_owner,
        verify_demo_product_owner,
    )

    external_account_ref = await business_system_ref_for_account(
        session,
        tenant_id=tenant_id,
        account_id=account_id,
        system_key="business_api",
    )
    if external_account_ref is None:
        raise TaskCommandRefused(
            "FLOW_ACCOUNT_AUTHORITY_UNMAPPED",
            "the linked account has no Demo business-system reference",
        )
    verified: dict[str, dict[str, Any]] = {}
    for name in lookup_field_names:
        proof = (
            verify_demo_order_owner(fields.get(name, ""), external_account_ref)
            if task.flow_key == "order_status"
            else verify_demo_product_owner(fields.get(name, ""), external_account_ref)
        )
        if proof is None:
            raise TaskCommandRefused(
                "FLOW_BUSINESS_RECORD_UNVERIFIED",
                "the Demo authority could not verify this product for the linked account",
            )
        verified[name] = proof
    return verified


async def _run_demo_order_query(
    session: Any,
    *,
    ctx: TenantContext,
    conversation_ref: uuid.UUID,
    task: Any,
    expected_version: int,
    actor_ref: str,
    trace_id: str,
    idempotency_key_hash: str,
    request_hash: str,
) -> Any:
    """Run one explicit Demo order read through Tool Gateway and record its receipt."""
    if ctx.actor_id is None:
        raise TaskCommandRefused(
            "ACTOR_UNRESOLVED", "order queries require an identified support agent"
        )
    flow = get_standard_flow("order_status")
    if (
        task.flow_key != "order_status"
        or flow is None
        or task.flow_version != flow.version
        or task.kind != TaskKind.READ.value
        or TaskStatus(task.status) is not TaskStatus.MANUAL_FLOW
    ):
        raise TaskCommandRefused(
            "FLOW_INSTANCE_NOT_QUERYABLE",
            "only an open order-status read flow can query an order",
        )
    if task.missing_slots:
        raise TaskCommandRefused(
            "FLOW_FIELDS_REQUIRED", "record and verify the order ID before querying"
        )
    if not _demo_business_flows_enabled():
        raise TaskCommandRefused(
            "FLOW_ORDER_QUERY_UNAVAILABLE",
            "the standard-flow query currently requires the local/test Demo adapter",
        )

    order_slot = next(
        (
            slot
            for slot in task.slots or []
            if slot.get("name") == "order_id"
            and slot.get("origin") == "verified_business_record"
            and slot.get("verification_source") == "demo"
        ),
        None,
    )
    if order_slot is None or not str(order_slot.get("value") or "").strip():
        raise TaskCommandRefused(
            "FLOW_BUSINESS_RECORD_UNVERIFIED",
            "verify the order belongs to the linked account before querying",
        )
    account_id = await verified_account_for_conversation(
        session,
        tenant_id=ctx.tenant_id,
        conversation_ref_id=conversation_ref,
    )
    if account_id is None:
        raise TaskCommandRefused(
            "FLOW_ACCOUNT_UNVERIFIED",
            "link this conversation to one tenant account before querying its order",
        )
    from platform_core.identity.profile import business_system_ref_for_account
    from platform_core.integrations.demo_erp import verify_demo_order_owner

    external_account_ref = await business_system_ref_for_account(
        session,
        tenant_id=ctx.tenant_id,
        account_id=account_id,
        system_key="business_api",
    )
    order_id = str(order_slot["value"])
    if (
        external_account_ref is None
        or verify_demo_order_owner(order_id, external_account_ref) is None
    ):
        raise TaskCommandRefused(
            "FLOW_BUSINESS_RECORD_UNVERIFIED",
            "the Demo authority no longer verifies this order for the linked account",
        )

    from platform_core.tool_gateway.gateway import ToolDenied, ToolGateway, ToolGatewayError
    from platform_core.tool_gateway.registry import resolve_executors

    executors = await resolve_executors(
        session,
        tenant_id=ctx.tenant_id,
        tool_names=["order.get_status"],
        ctx=ctx,
        trace_id=trace_id,
    )
    if "order.get_status" not in executors:
        raise TaskCommandRefused(
            "FLOW_ORDER_CONNECTOR_UNAVAILABLE",
            "the tenant has no active orders_read connector",
        )

    proposal_key = f"standard-flow:{task.id}:order-status:r{task.action_revision}"
    gateway = ToolGateway(session, executors)
    try:
        proposal_id = await gateway.propose_id(
            tenant_id=ctx.tenant_id,
            actor_id=ctx.actor_id,
            tool_name="order.get_status",
            arguments={"order_id": order_id},
            role=ctx.role or "unknown",
            idempotency_key=proposal_key,
            permission_allowed=True,
            required_action=Action.TOOL_READ.value,
        )
    except (ToolDenied, ToolGatewayError) as exc:
        raise TaskCommandRefused(
            "FLOW_ORDER_QUERY_UNAVAILABLE",
            "Tool Gateway did not authorize or prepare the read proposal",
        ) from exc
    executing = await task_store.transition(
        session,
        tenant_id=ctx.tenant_id,
        task=task,
        command=task_store.TaskCommand(
            target=TaskStatus.EXECUTING,
            reason_code="STANDARD_FLOW_ORDER_READ_STARTED",
            actor_type="human",
            actor_ref=actor_ref,
            trace_id=trace_id,
            expected_version=expected_version,
            proposal_id=proposal_id,
        ),
    )
    try:
        execution = await gateway.execute_receipt(
            tenant_id=ctx.tenant_id,
            actor_id=ctx.actor_id,
            proposal_id=proposal_id,
        )
    except ToolGatewayError as exc:
        recovered_execution = await gateway.execution_receipt(
            tenant_id=ctx.tenant_id,
            proposal_id=proposal_id,
        )
        if recovered_execution is None:
            raise TaskCommandRefused(
                "FLOW_ORDER_QUERY_FAILED",
                "the Demo order query failed before an execution receipt was recorded",
            ) from exc
        execution = recovered_execution

    output = execution.sanitized_output if isinstance(execution.sanitized_output, dict) else {}
    verified_owner = (
        execution.status == "executed"
        and execution.verification_status == "verified"
        and output.get("source") == "demo"
        and output.get("account") == external_account_ref
        and output.get("order_id") == order_id
        and bool(output.get("fetched_at"))
    )
    if execution.status == "unknown" or execution.verification_status == "unknown":
        target = TaskStatus.UNKNOWN
        reason_code = "STANDARD_FLOW_ORDER_READ_UNKNOWN"
        blocked_reason = "TOOL_EXECUTION_UNKNOWN"
    elif execution.status != "executed" or not verified_owner:
        target = TaskStatus.FAILED
        reason_code = "STANDARD_FLOW_ORDER_READ_FAILED"
        blocked_reason = (
            "FLOW_BUSINESS_RECORD_UNVERIFIED"
            if execution.verification_status == "verified"
            else "TOOL_EXECUTION_FAILED"
        )
    else:
        target = TaskStatus.SUCCEEDED
        reason_code = "STANDARD_FLOW_ORDER_READ_VERIFIED"
        blocked_reason = ""

    slots = list(executing.slots or [])
    if target is TaskStatus.SUCCEEDED:
        raw_nodes = output.get("nodes")
        nodes = (
            [
                {
                    "label": str(node.get("label") or ""),
                    "status": str(node.get("status") or ""),
                    "at": node.get("at") if isinstance(node.get("at"), str) else None,
                }
                for node in raw_nodes
                if isinstance(node, dict)
            ]
            if isinstance(raw_nodes, list)
            else []
        )
        receipt = {
            "order_id": order_id,
            "status": str(output.get("status") or "unknown"),
            "nodes": nodes,
            "eta": output.get("eta") if isinstance(output.get("eta"), str) else None,
            "source": "demo",
            "fetched_at": str(output["fetched_at"]),
        }
        slots = _merge_slots(
            slots,
            [
                {
                    "name": "order_status_receipt",
                    "value": receipt,
                    "origin": "verified_receipt",
                    "confirmed": True,
                    "verification_source": "demo",
                }
            ],
        )

    updated = await task_store.transition(
        session,
        tenant_id=ctx.tenant_id,
        task=executing,
        command=task_store.TaskCommand(
            target=target,
            reason_code=reason_code,
            actor_type="human",
            actor_ref=actor_ref,
            trace_id=trace_id,
            expected_version=executing.version,
            completion_evidence=(
                f"{task_store.EVIDENCE_VERIFIED_RECEIPT}{execution.id}"
                if target is TaskStatus.SUCCEEDED
                else None
            ),
            blocked_reason=blocked_reason,
            slots=slots,
            execution_id=execution.id,
            idempotency_key_hash=idempotency_key_hash,
            request_hash=request_hash,
        ),
    )
    await audit_service.record(
        session,
        ctx=ctx,
        action="conversation.standard_flow_order_read",
        resource_type="conversation_task",
        resource_id=task.id,
        metadata={
            "tool_name": "order.get_status",
            "execution_id": str(execution.id),
            "verification_status": execution.verification_status,
            "source": "demo",
        },
        trace_id=trace_id,
    )
    get_metrics().workbench_standard_flow_actions_total.labels(
        flow_key="order_status",
        action="query_order_status",
        outcome=updated.status,
    ).inc()
    return updated


async def _build_command(
    session: Any,
    *,
    ctx: TenantContext,
    conversation_ref: uuid.UUID,
    task: Any,
    body: TaskCommandIn,
    actor_ref: str,
    trace_id: str,
) -> task_store.TaskCommand | None:
    """Map one command name to a `TaskCommand`, or None when it does not
    apply.

    Kept out of the route so the mapping is testable without a request, and
    returning `None` rather than an error object so a refusal and a command
    cannot be confused at the call site.
    """

    def _base() -> dict[str, Any]:
        # Built per call rather than shared, so a caller cannot mutate the
        # mapping between the two branches. The keys are spelled out because
        # `**base` erases their types and mypy then sees `object` everywhere.
        return {
            "actor_type": "human",
            "actor_ref": actor_ref,
            "trace_id": trace_id,
            "expected_version": body.expected_version,
        }

    base = _base()

    if body.command == "collect_fields":
        # B1-04: the previous revision removed the field *names* from
        # `missing_slots` and moved the task to `ready`, while writing the
        # value nowhere. An operator saw "已记录客户补充的信息" and the
        # database had no street, no source and no confirmation. The task
        # looked complete and carried nothing.
        #
        # The value is persisted now, with a source, and the field name must
        # be one this task is actually waiting for. Both halves matter: a
        # route that accepted arbitrary names would let a caller attach
        # anything to a task, and a value with no source is the exact shape
        # EVAL-02 counts as a failure.
        requested = list(body.fields)
        if not requested:
            return None
        lookup_fields: set[str] = set()
        flow_template = get_standard_flow(task.flow_key) if task.flow_key else None
        if task.flow_key:
            flow_status = TaskStatus(task.status)
            allowed_flow_states = {TaskStatus.MANUAL_FLOW}
            if task.flow_key == "invoice_application":
                allowed_flow_states.add(TaskStatus.NEEDS_HUMAN)
            if (
                flow_template is None
                or task.flow_version != flow_template.version
                or flow_status not in allowed_flow_states
            ):
                raise TaskCommandRefused(
                    "FLOW_INSTANCE_NOT_COLLECTABLE",
                    "this standard flow instance is not in its current manual collection state",
                )
            customer_fields = {
                field.name
                for field in flow_template.required_fields
                if field.source == "customer" or field.lookup_input
            }
            lookup_fields = {
                field.name for field in flow_template.required_fields if field.lookup_input
            }
            invalid_sources = [name for name in requested if name not in customer_fields]
            if invalid_sources:
                raise TaskCommandRefused(
                    "FLOW_FIELD_SOURCE_REQUIRES_VERIFICATION",
                    "verified-source fields must come from their trusted record or reviewer",
                )
        unknown = [name for name in requested if name not in task.missing_slots]
        if unknown:
            # Refused, not ignored: silently dropping a field the operator
            # typed would make the UI's confirmation a lie.
            raise TaskCommandRefused(
                "TASK_FIELD_NOT_REQUESTED",
                f"this task is not waiting for: {', '.join(unknown)}",
            )

        # Keep operator-provided data on the task with explicit provenance.
        # It is not a customer-authored transcript turn.
        verified_fields: dict[str, dict[str, Any]] = {}
        if task.flow_key and lookup_fields.intersection(requested):
            verified_fields = await _verify_demo_flow_fields(
                session,
                tenant_id=ctx.tenant_id,
                conversation_ref=conversation_ref,
                task=task,
                fields=body.fields,
                lookup_field_names=lookup_fields.intersection(requested),
            )
        collected = await _persist_collected(
            ctx=ctx,
            fields=body.fields,
            verified_fields=verified_fields,
            flow_key=task.flow_key,
        )

        remaining = [m for m in task.missing_slots if m not in requested]
        new_slots = _merge_slots(task.slots, collected)
        if task.flow_key:
            connector_capabilities = await active_connector_capabilities(
                session, tenant_id=ctx.tenant_id
            )
            return task_store.TaskCommand(
                target=(
                    TaskStatus.NEEDS_HUMAN
                    if TaskStatus(task.status) is TaskStatus.NEEDS_HUMAN
                    else TaskStatus.MANUAL_FLOW
                ),
                reason_code="STANDARD_FLOW_FIELDS_RECORDED",
                missing_slots=remaining,
                slots=new_slots,
                blocked_reason=_flow_blocked_reason(
                    task.flow_key,
                    orders_read_available="orders_read" in connector_capabilities,
                ),
                **base,
            )
        return task_store.TaskCommand(
            target=TaskStatus.READY if not remaining else TaskStatus.AWAITING_INPUT,
            reason_code=("TASK_FIELDS_COLLECTED" if not remaining else "TASK_FIELDS_PARTIAL"),
            missing_slots=remaining,
            slots=new_slots,
            **base,
        )

    if body.command == "cancel":
        return task_store.TaskCommand(
            target=TaskStatus.CANCELLED, reason_code="TASK_CANCELLED_BY_AGENT", **base
        )

    if body.command == "handoff":
        # Park rather than cancel: the task was real work someone asked for,
        # and cancelling it records an outcome nobody chose.
        return task_store.TaskCommand(
            target=TaskStatus.NEEDS_HUMAN,
            reason_code="TASK_HANDED_TO_HUMAN",
            blocked_reason="TASK_HANDED_TO_HUMAN",
            **base,
        )

    # prepare_proposal. B1-05: the previous revision only moved the task to
    # `awaiting_confirmation` and bumped the revision. No `ToolProposal` was
    # created, `proposal_id` stayed NULL, and the UI said "生成待确认提案"
    # over a task with nothing to confirm.
    #
    # A real proposal is created here, through the same `ToolGateway.propose`
    # the proposals route uses, and the resulting id is written onto the task.
    # If no write capability exists for this tenant the task goes to
    # `needs_human` with the reason, rather than sitting in a state that
    # advertises a confirmation that will never arrive.
    if task.flow_key:
        flow = get_standard_flow(task.flow_key)
        flow_status = TaskStatus(task.status)
        allowed_flow_states = {TaskStatus.MANUAL_FLOW}
        if task.flow_key in INTERNAL_CASE_FLOWS:
            allowed_flow_states.add(TaskStatus.NEEDS_HUMAN)
        if (
            flow is None
            or task.flow_version != flow.version
            or task.kind != TaskKind.WRITE.value
            or flow_status not in allowed_flow_states
        ):
            return None
        if task.flow_key not in INTERNAL_CASE_FLOWS:
            return None
        if task.missing_slots:
            raise TaskCommandRefused(
                "FLOW_FIELDS_REQUIRED", "collect and verify all required flow fields first"
            )
        account_id = await verified_account_for_conversation(
            session,
            tenant_id=ctx.tenant_id,
            conversation_ref_id=conversation_ref,
        )
        if account_id is None:
            raise TaskCommandRefused(
                "FLOW_ACCOUNT_UNVERIFIED",
                "link this conversation to a tenant account before preparing the internal case",
            )
        if task.flow_key in DEMO_PRODUCT_FLOWS:
            if not _demo_business_flows_enabled():
                raise TaskCommandRefused(
                    "FLOW_BUSINESS_VERIFICATION_UNAVAILABLE",
                    "this flow needs an authorized business-record adapter",
                )
            product_slot = next(
                (
                    slot
                    for slot in task.slots or []
                    if slot.get("name") == "product_ref"
                    and slot.get("origin") == "verified_business_record"
                    and slot.get("verification_source") == "demo"
                ),
                None,
            )
            if product_slot is None or not str(product_slot.get("value") or "").strip():
                raise TaskCommandRefused(
                    "FLOW_BUSINESS_RECORD_UNVERIFIED",
                    "the product must be verified by the configured business authority",
                )
            from platform_core.identity.org import routable_support_department_slugs
            from platform_core.identity.profile import business_system_ref_for_account
            from platform_core.integrations.demo_erp import verify_demo_product_owner

            external_account_ref = await business_system_ref_for_account(
                session,
                tenant_id=ctx.tenant_id,
                account_id=account_id,
                system_key="business_api",
            )
            if (
                external_account_ref is None
                or verify_demo_product_owner(str(product_slot["value"]), external_account_ref)
                is None
            ):
                raise TaskCommandRefused(
                    "FLOW_BUSINESS_RECORD_UNVERIFIED",
                    "the Demo authority no longer verifies this product for the linked account",
                )
            routable_owners = await routable_support_department_slugs(
                session, tenant_id=ctx.tenant_id
            )
            if not flow.owner_group or flow.owner_group not in routable_owners:
                raise TaskCommandRefused(
                    "FLOW_OWNER_UNASSIGNED",
                    "add an active support owner to the matching tenant department",
                )
        proposal = await _create_proposal(
            session,
            ctx=ctx,
            task=task,
            trace_id=trace_id,
            verified_account_id=account_id,
        )
        if proposal is None:
            return task_store.TaskCommand(
                target=TaskStatus.NEEDS_HUMAN,
                reason_code="FLOW_INTERNAL_CASE_PROPOSAL_UNAVAILABLE",
                blocked_reason="FLOW_INTERNAL_CASE_PROPOSAL_UNAVAILABLE",
                **base,
            )
        return task_store.TaskCommand(
            target=TaskStatus.AWAITING_CONFIRMATION,
            reason_code=(
                "STANDARD_FLOW_INTERNAL_CASE_PROPOSAL_PREPARED"
                if task.flow_key == "invoice_application"
                else "STANDARD_FLOW_INTERNAL_ROUTED_CASE_PROPOSAL_PREPARED"
            ),
            blocked_reason="",
            bump_action_revision=True,
            proposal_id=proposal,
            **base,
        )

    if task.kind != TaskKind.WRITE.value:
        return None
    if TaskStatus(task.status) is not TaskStatus.READY:
        return None

    proposal = await _create_proposal(
        session,
        ctx=ctx,
        task=task,
        trace_id=trace_id,
    )
    if proposal is None:
        # No write capability. Park it with the reason the planner would have
        # used, so the panel says the same thing whichever path produced it.
        return task_store.TaskCommand(
            target=TaskStatus.NEEDS_HUMAN,
            reason_code="SEMANTIC_NO_WRITE_CAPABILITY",
            blocked_reason="SEMANTIC_NO_WRITE_CAPABILITY",
            **base,
        )

    return task_store.TaskCommand(
        target=TaskStatus.AWAITING_CONFIRMATION,
        reason_code="TASK_PROPOSAL_PREPARED",
        bump_action_revision=True,
        proposal_id=proposal,
        **base,
    )


async def _create_proposal(
    session: Any,
    *,
    ctx: TenantContext,
    task: Any,
    trace_id: str,
    verified_account_id: uuid.UUID | None = None,
) -> uuid.UUID | None:
    """Create a real `ToolProposal` for a write task, or None when it cannot.

    The gateway is the only path to a business write, and this calls it rather
    than writing a proposal-shaped row: a second way to create a proposal would
    be a second set of rules about what a confirmation authorises.

    Returns None - and the caller parks the task - when:

    - the task names no tool, because nothing chose one for it; and
    - the tenant has no registered write tool at all, which is the R1 case for
      an address change.

    The idempotency key is derived from the task id and its action revision
    rather than generated per request, so a double-clicked "准备提案" creates
    one proposal rather than two.
    """
    from platform_core.tool_gateway.catalog import tool_risk
    from platform_core.tool_gateway.gateway import ToolDenied, ToolGateway, ToolGatewayError

    tool_name = _write_tool_for(task)
    if not tool_name:
        return None

    risk = await tool_risk(session, tenant_id=ctx.tenant_id, tool_name=tool_name)
    if risk not in ("low_write", "confirmed_write", "human_approval"):
        return None

    arguments = _proposal_arguments(task, verified_account_id=verified_account_id)
    if arguments is None:
        # A required argument has no value. Proposing anyway would produce a
        # proposal the gateway refuses with a schema error the agent cannot
        # map back to a field.
        return None

    required_action = {
        "low_write": "tool.write.low",
        "confirmed_write": "tool.write.confirmed",
        "human_approval": "tool.human_approval",
    }[risk]

    # The gateway's `propose` always inserts: it is a "freeze these arguments"
    # operation with no replay semantics of its own. So the replay check lives
    # here, keyed on the same (task, revision) pair the idempotency key encodes.
    #
    # Without it, a double-clicked "准备提案" produced two proposals and two
    # confirmations to choose between, and the second was indistinguishable
    # from a deliberate re-proposal with changed arguments - which is the exact
    # distinction the action revision exists to make.
    key = f"task-{task.id}-r{task.action_revision}"
    gateway = ToolGateway(session, {})
    existing_id = await gateway.proposal_id_for_idempotency_key(
        tenant_id=ctx.tenant_id,
        idempotency_key=key,
    )
    if existing_id is not None:
        return existing_id
    if ctx.actor_id is None:
        # The route refuses an unidentified caller before reaching here; this
        # is the type-level statement of the same rule, because a proposal
        # with no actor is a write nobody can be shown to have authorised.
        return None
    try:
        proposal_id = await gateway.propose_id(
            tenant_id=ctx.tenant_id,
            actor_id=ctx.actor_id,
            tool_name=tool_name,
            arguments=arguments,
            role=ctx.role or "unknown",
            # Stable per (task, revision): a replay is the same proposal, and a
            # bumped revision is deliberately a different one, which is what
            # makes the old confirmation stop matching.
            idempotency_key=key,
            permission_allowed=True,
            required_action=required_action,
        )
        return proposal_id
    except (ToolGatewayError, ToolDenied) as exc:
        # Logged, not swallowed. The previous revision caught this and returned
        # None with nothing recorded, which is how a proposal that was never
        # created looked identical to one that was refused: the task went to
        # needs_human either way, and the reason was unreadable.
        #
        # A refusal here is a real answer - the arguments failed the tool's
        # schema, or the actor lacks the permission - and an operator debugging
        # "why did my task not become a proposal" needs it.
        _log_proposal_refusal(tool_name, exc)
        return None


def _log_proposal_refusal(tool_name: str, exc: Exception) -> None:
    """Record why a proposal was refused, without the arguments.

    The tool name and the gateway's code are the two things an operator needs.
    The arguments are not logged: they carry the customer's own words and a
    confirmed write's arguments are the thing about to be approved.
    """
    import logging

    # Both gateway errors carry `.code`; the fallback keeps the call total if a
    # future exception type does not, because a missing reason code is better
    # than a crash in the logging path of a business refusal.
    code = getattr(exc, "code", type(exc).__name__)
    logging.getLogger("platform.tasks").warning(
        "proposal_refused", extra={"tool_name": tool_name, "reason_code": str(code)}
    )


def _write_tool_for(task: Any) -> str | None:
    """The write tool this task would act through, if one was recorded.

    A task whose slots name a `tool` slot has been told which tool to use by
    the capability filter. A task without one is not guessed at: R1 has no
    deterministic way to choose a write tool from a set of collected fields, and
    choosing wrong would propose a real write against the wrong target.
    """
    if task.flow_key in INTERNAL_CASE_FLOWS:
        return "case.create"
    for slot in task.slots or []:
        if slot.get("name") == "tool" and slot.get("value"):
            return str(slot["value"])
    return None


def _proposal_arguments(
    task: Any,
    *,
    verified_account_id: uuid.UUID | None = None,
) -> dict[str, Any] | None:
    """Build the tool arguments from the task's slots, or None if incomplete.

    Only non-sensitive, confirmed-by-presence slot values are used. A withheld
    value cannot become an argument: the proposal would carry an empty string
    where the customer gave an address, and the gateway would accept it.
    """
    if task.flow_key == "invoice_application":
        values = {
            slot.get("name"): slot.get("value")
            for slot in task.slots or []
            if slot.get("origin") == "agent_collected" and not slot.get("value_withheld")
        }
        order_id = str(values.get("order_id") or "").strip()
        invoice_type = str(values.get("invoice_type") or "").strip()
        if not verified_account_id or not order_id or not invoice_type:
            return None
        return {
            "enterprise_account_id": str(verified_account_id),
            "subject": f"发票申请：订单 {order_id}",
            "description": f"订单号：{order_id}；发票类型：{invoice_type}",
            "category": "invoice_application",
            "priority": "p2",
            "conversation_ref_id": str(task.conversation_ref_id),
        }

    if task.flow_key in DEMO_PRODUCT_FLOWS:
        values = {
            str(slot.get("name")): slot.get("value")
            for slot in task.slots or []
            if slot.get("value") is not None
            and not slot.get("value_withheld")
            and not slot.get("inferred")
        }
        product_slot = next(
            (
                slot
                for slot in task.slots or []
                if slot.get("name") == "product_ref"
                and slot.get("origin") == "verified_business_record"
                and slot.get("verification_source") == "demo"
            ),
            None,
        )
        product_ref = str(product_slot.get("value") or "").strip() if product_slot else ""
        summary_name = (
            "issue_summary" if task.flow_key == "repair_quality_intake" else "question_or_symptom"
        )
        summary = " ".join(str(values.get(summary_name) or "").split())
        if not verified_account_id or not product_ref or not summary:
            return None
        flow = get_standard_flow(task.flow_key)
        if flow is None or not flow.owner_group:
            return None
        quality = task.flow_key == "repair_quality_intake"
        category = "quality_issue" if quality else "technical_escalation"
        prefix = "质量问题受理" if quality else "技术问题升级"
        return {
            "enterprise_account_id": str(verified_account_id),
            "subject": f"{prefix}：{product_ref}",
            "description": (f"产品编号：{product_ref}（本地 Demo 目录核验）\n客户描述：{summary}"),
            "priority": "p2",
            "category": category,
            "conversation_ref_id": str(task.conversation_ref_id),
            "team_ref": flow.owner_group,
            "product_ref": product_ref,
            "product_verification_source": "demo",
        }

    arguments: dict[str, Any] = {}
    for slot in task.slots or []:
        name = slot.get("name")
        if not name or name == "tool":
            continue
        if slot.get("value_withheld") or slot.get("inferred"):
            # Either withheld by policy or never sourced. Both mean "we do not
            # have a value we are willing to write".
            return None
        if "value" in slot:
            arguments[str(name)] = slot["value"]
    return arguments or None


# `TaskConflict` is imported late so the module docstring's import list stays
# readable; it is the store's optimistic-concurrency error.
TaskConflict_ = task_store.TaskConflict


# --- copilot ----------------------------------------------------------------


@router.post("/conversations/{conversation_ref}/copilot/jobs")
async def create_copilot_job(
    request: Request, conversation_ref: uuid.UUID, body: CopilotJobIn
) -> Any:
    """Persist a queued draft request and return its job id.

    B1-03: the previous revision built a `CopilotJob` in memory, returned it,
    and wrote no row - so the POST answered `queued` and the GET that followed
    it answered 404. A job that does not exist cannot be polled, and an
    operator watching a spinner that will never resolve is worse than an
    error.

    The row is written before the response, in the same transaction as the
    ownership check, so a 202 always means a job exists. The generation itself
    is enqueued for the worker; nothing is generated inline and nothing is
    ever sent.

    Two validations that the old version skipped:

    - **Source turns must belong to this conversation and this tenant.** A
      caller could name any turn id and get a `queued`; a summary's sources
      are what COP-01 requires it to be checkable against, so a source that
      does not resolve is refused rather than recorded.
    - **The lease must still be the caller's**, re-read inside the
      transaction. A request queued for a conversation somebody else now owns
      would be generated against state its requester cannot see.
    """
    ctx, denied = _auth(request, Action.CASE_UPDATE)
    if denied is not None:
        return denied
    assert ctx is not None
    missing = require_write_idempotency(request, Action.CASE_UPDATE)
    if missing is not None:
        return missing
    if ctx.actor_id is None:
        return error_response(VALIDATION_FAILED, "an identified agent is required", status_code=400)

    actor_id = ctx.actor_id
    idempotency_key = request.headers["Idempotency-Key"]
    trace_id = new_trace_id()
    try:
        async with tenant_session(ctx) as session:
            replay_job_id = derive_job_id(
                tenant_id=ctx.tenant_id,
                conversation_ref_id=conversation_ref,
                actor_id=actor_id,
                kind=CopilotKind(body.kind),
                timeline_revision=body.timeline_revision,
                lease_version=body.lease_version,
                request_key=idempotency_key,
            )
            replay = (
                await session.execute(
                    sa_select(CopilotDraft).where(
                        CopilotDraft.tenant_id == ctx.tenant_id,
                        CopilotDraft.job_id == replay_job_id,
                    )
                )
            ).scalar_one_or_none()
            if replay is not None:
                replay_sources = list(replay.source_refs or [])
                replay_source_ids = [str(ref.get("turn_id", "")) for ref in replay_sources]
                if (
                    replay.conversation_ref_id != conversation_ref
                    or replay.actor_id != actor_id
                    or replay.kind != body.kind
                    or replay.timeline_revision != body.timeline_revision
                    or replay.lease_version != body.lease_version
                    or replay.task_id != body.task_id
                    or replay_source_ids != body.source_turn_ids
                    or bool(body.instructions.strip())
                ):
                    return error_response(
                        "IDEMPOTENCY_CONFLICT",
                        "key was used for a different copilot request",
                        status_code=409,
                        trace_id=trace_id,
                    )
                return ok_response(
                    {
                        "job_id": str(replay.job_id),
                        "kind": replay.kind,
                        "status": replay.status,
                        "timeline_revision": replay.timeline_revision,
                        "lease_version": replay.lease_version,
                    },
                    trace_id=trace_id,
                )

            lease = await lease_service.lease_snapshot(
                session, tenant_id=ctx.tenant_id, conversation_ref_id=conversation_ref
            )
            if lease is None:
                return error_response(CASE_NOT_FOUND, "conversation not found", status_code=404)
            if lease.owner_type != "human" or lease.owner_ref != str(actor_id):
                # SEC-02: a visitor with a valid token still cannot queue a
                # copilot job for a conversation they do not own.
                return error_response(
                    "LEASE_NOT_OWNED",
                    "only the current human owner may generate a draft",
                    status_code=409,
                )
            if lease.lease_version != body.lease_version:
                return error_response(
                    "LEASE_CONFLICT",
                    f"lease version moved to {lease.lease_version}",
                    status_code=409,
                )

            from platform_core.knowledge import flag_service

            copilot_flag = await flag_service.evaluate(
                session,
                flag_key=FLAG_COPILOT,
                tenant_id=ctx.tenant_id,
                default=False,
            )
            if not copilot_flag.enabled:
                return error_response(
                    "COPILOT_DISABLED",
                    "AI 副驾生成当前未启用。",
                    status_code=403,
                )
            if body.instructions.strip():
                return error_response(
                    "COPILOT_INSTRUCTIONS_UNAVAILABLE",
                    "自定义生成指令尚未开放；当前仅使用受控的默认提示词。",
                    status_code=400,
                )

            current_revision = await _timeline_revision(session, conversation_ref=conversation_ref)
            if current_revision != body.timeline_revision:
                # The client generated from a view that has since changed.
                # Refused here rather than producing a job that would be stale
                # on arrival.
                return error_response(
                    "TIMELINE_MOVED",
                    f"timeline is at revision {current_revision}",
                    status_code=409,
                )

            sources = await _resolve_sources(
                session,
                conversation_ref=conversation_ref,
                turn_ids=body.source_turn_ids,
            )
            if sources is None:
                return error_response(
                    "COPILOT_SOURCE_NOT_FOUND",
                    "a named source turn does not belong to this conversation",
                    status_code=404,
                )

            kind = CopilotKind(body.kind)
            job = new_job(
                tenant_id=ctx.tenant_id,
                conversation_ref_id=conversation_ref,
                actor_id=actor_id,
                kind=kind,
                timeline_revision=body.timeline_revision,
                lease_version=body.lease_version,
                task_id=body.task_id,
                source_refs=sources,
                request_key=idempotency_key,
            )

            row = CopilotDraft(
                tenant_id=ctx.tenant_id,
                conversation_ref_id=conversation_ref,
                actor_id=actor_id,
                job_id=job.job_id,
                task_id=body.task_id,
                kind=job.kind.value,
                status=CopilotJobStatus.QUEUED.value,
                timeline_revision=job.timeline_revision,
                lease_version=job.lease_version,
                source_refs=sources,
                body="",
                version=1,
                edited_by_human=False,
                created_at=job.created_at,
                updated_at=job.updated_at,
            )
            session.add(row)
            await session.flush()

            # The generation runs in the worker, from its own claim, so a slow
            # provider cannot hold this request open. The outbox row is written
            # in the same transaction as the draft, so a job cannot exist
            # without a consumer able to see it.
            await enqueue(
                session,
                tenant_id=ctx.tenant_id,
                event_type=COPILOT_EVENT_TYPE,
                aggregate_type="copilot_draft",
                aggregate_id=str(row.job_id),
                payload={
                    "job_id": str(job.job_id),
                    "draft_id": str(row.id),
                    "conversation_ref": str(conversation_ref),
                    "kind": job.kind.value,
                    "timeline_revision": job.timeline_revision,
                    "lease_version": job.lease_version,
                },
                trace_id=trace_id,
            )
    except CopilotError as exc:
        status = 422 if exc.code.endswith("REQUIRES_SOURCES") else 400
        return error_response(exc.code, exc.detail, status_code=status, trace_id=trace_id)
    except IntegrityError:
        # The (tenant_id, job_id) unique constraint fired: this exact request
        # already exists. Returning the original is the correct replay
        # behaviour, and it is a 200 rather than a 5xx because the caller
        # asked for something that is already true.
        async with tenant_session(ctx) as session:
            existing = (
                await session.execute(
                    sa_select(CopilotDraft).where(
                        CopilotDraft.tenant_id == ctx.tenant_id,
                        CopilotDraft.job_id == job.job_id,
                    )
                )
            ).scalar_one_or_none()
        if existing is None:
            return error_response(
                "CONFLICT", "the job could not be created", status_code=409, trace_id=trace_id
            )
        if (
            existing.conversation_ref_id != conversation_ref
            or existing.actor_id != actor_id
            or existing.kind != job.kind.value
            or existing.timeline_revision != job.timeline_revision
            or existing.lease_version != job.lease_version
            or existing.task_id != job.task_id
            or list(existing.source_refs or []) != sources
        ):
            return error_response(
                "IDEMPOTENCY_CONFLICT",
                "key was used for a different copilot request",
                status_code=409,
                trace_id=trace_id,
            )
        return ok_response(
            {
                "job_id": str(existing.job_id),
                "kind": existing.kind,
                "status": existing.status,
                "timeline_revision": existing.timeline_revision,
                "lease_version": existing.lease_version,
            },
            trace_id=trace_id,
        )

    return ok_response(
        {
            "job_id": str(job.job_id),
            "kind": job.kind.value,
            "status": job.status.value,
            "timeline_revision": job.timeline_revision,
            "lease_version": job.lease_version,
        },
        trace_id=trace_id,
    )


async def _resolve_sources(
    session: Any,
    *,
    conversation_ref: uuid.UUID,
    turn_ids: list[str],
) -> list[dict[str, Any]] | None:
    """Resolve source turn ids against this conversation, or None if any miss.

    Read through the tenant session, so a turn belonging to another tenant is
    invisible rather than refused - the two are indistinguishable to the
    caller, which is the point.

    A summary with no sources is refused upstream by `new_job`; this function
    handles the other half, where sources were named but do not exist.
    """
    if not turn_ids:
        return []
    rows = list(
        (
            await session.execute(
                sa_select(ConversationTurn).where(
                    ConversationTurn.conversation_ref_id == conversation_ref,
                    ConversationTurn.id.in_(turn_ids),
                )
            )
        )
        .scalars()
        .all()
    )
    found = {str(r.id) for r in rows}
    if found != set(turn_ids):
        return None
    # Offsets are recorded so a later review can point at the exact span. The
    # text itself stays in `conversation_turns`.
    roles = {str(row.id): row.role for row in rows}
    return [{"turn_id": turn_id, "role": roles[turn_id], "offset": [0, 0]} for turn_id in turn_ids]


@router.get("/conversations/{conversation_ref}/copilot/jobs/{job_id}")
async def read_copilot_job(request: Request, conversation_ref: uuid.UUID, job_id: uuid.UUID) -> Any:
    """Poll a job. Cross-conversation ids are indistinguishable from unknown.

    B1-03 also: the previous revision called `session.get(CopilotDraft,
    job_id)`, which looks the row up by *primary key*. The URL carries the
    `job_id` column, which is a different column with a unique constraint -
    so even once rows were being written, every poll would have 404'd. The
    lookup is by (tenant, job_id) now, and the tenant predicate is
    belt-and-braces on top of RLS rather than a substitute for it.
    """
    ctx, denied = _auth(request, Action.CASE_READ)
    if denied is not None:
        return denied
    assert ctx is not None

    async with tenant_session(ctx) as session:
        row = (
            await session.execute(
                sa_select(CopilotDraft).where(
                    CopilotDraft.tenant_id == ctx.tenant_id,
                    CopilotDraft.job_id == job_id,
                )
            )
        ).scalar_one_or_none()
        if row is None or row.conversation_ref_id != conversation_ref:
            return error_response("NOT_FOUND", "job not found", status_code=404)

        payload: dict[str, Any] = {
            "job_id": str(row.job_id),
            "kind": row.kind,
            "status": row.status,
            "timeline_revision": row.timeline_revision,
            "lease_version": row.lease_version,
            "draft_id": str(row.id) if row.id else None,
            "body": row.body,
            "source_refs": row.source_refs,
            "edited_by_human": row.edited_by_human,
            "error_code": row.error_code,
            "can_insert": False,
        }

        # Staleness is evaluated on read, because the world can change between
        # the worker writing `succeeded` and the agent reading it. A job that
        # went stale while nobody was looking must read as stale.
        if row.status == CopilotJobStatus.SUCCEEDED.value:
            current_revision = await _timeline_revision(session, conversation_ref=conversation_ref)
            lease = await lease_service.lease_snapshot(
                session, tenant_id=ctx.tenant_id, conversation_ref_id=conversation_ref
            )
            if lease is not None:
                refreshed = apply_staleness(
                    _row_to_job(row),
                    current_timeline_revision=current_revision,
                    current_lease_version=lease.lease_version,
                    current_actor_id=ctx.actor_id or uuid.UUID(int=0),
                )
                payload["status"] = refreshed.status.value
                payload["error_code"] = refreshed.error_code
                payload["can_insert"] = refreshed.can_insert()
        elif row.status in (CopilotJobStatus.QUEUED.value, CopilotJobStatus.RUNNING.value):
            # An uncollected job that has aged out is expired, not queued.
            # Reported on read rather than only by the worker's sweep, because
            # the operator is the one watching the spinner.
            job = _row_to_job(row)
            if should_expire(job):
                payload["status"] = CopilotJobStatus.EXPIRED.value
                payload["error_code"] = "COPILOT_JOB_EXPIRED"
            elif row.status == CopilotJobStatus.QUEUED.value:
                event_status = await latest_status_for_aggregate(
                    session,
                    tenant_id=ctx.tenant_id,
                    event_type=COPILOT_EVENT_TYPE,
                    aggregate_id=str(row.job_id),
                )
                if event_status == "processing":
                    payload["status"] = CopilotJobStatus.RUNNING.value

    return ok_response(payload)


async def _timeline_revision(session: Any, *, conversation_ref: uuid.UUID) -> int:
    """The conversation's current revision: how many turns it has.

    Derived rather than stored, because a revision that lives in a column is a
    number something has to remember to increment, and a missed increment
    means a stale draft gets inserted. Turn count cannot drift: every turn is
    a row, and a new customer message is a new row.
    """
    return await chat_service.timeline_revision(session, ref_id=conversation_ref)


def _row_to_job(row: Any) -> Any:
    from platform_core.agent_runtime.copilot import CopilotJob

    return CopilotJob(
        job_id=row.job_id,
        tenant_id=row.tenant_id,
        conversation_ref_id=row.conversation_ref_id,
        actor_id=row.actor_id,
        kind=CopilotKind(row.kind),
        status=CopilotJobStatus(row.status),
        timeline_revision=row.timeline_revision,
        lease_version=row.lease_version,
        task_id=row.task_id,
        source_refs=list(row.source_refs or []),
        body=row.body,
        edited_by_human=row.edited_by_human,
        error_code=row.error_code,
        created_at=row.created_at,
        updated_at=row.updated_at,
        version=row.version,
    )


__all__ = ["ALLOWED_COMMANDS", "router"]
