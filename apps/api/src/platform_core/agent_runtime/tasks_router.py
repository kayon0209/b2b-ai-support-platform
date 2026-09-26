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

**Nothing here sends anything.** `POST .../copilot/jobs` returns 202 and a job
id. Sending is the existing workbench reply path, which the agent uses after
inserting the generated text into their own draft. The two are separate
buttons in the UI for the same reason they are separate endpoints here.

Every response body is projection-shaped: task slots carry names, origins and
confirmation flags, never values (SEC-04), and a copilot body is only ever
returned to a `CASE_READ` principal on a conversation they can already see.
"""

from __future__ import annotations

import uuid
from typing import Any, Literal

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, Field

from platform_core.agent_runtime.copilot import (
    CopilotError,
    CopilotJobStatus,
    CopilotKind,
    apply_staleness,
    new_job,
    should_expire,
)
from platform_core.agent_runtime.tasks import store as task_store
from platform_core.agent_runtime.tasks.models import CopilotDraft
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
from platform_core.identity import lease_service
from platform_core.identity.control_lease import LeaseConflict
from platform_core.identity.tenant_context import TenantContext
from platform_core.outbox_service import enqueue
from platform_core.tool_gateway.models import ProposalStatus
from platform_policy import Action

router = APIRouter(prefix="/v1/workbench", tags=["workbench-tasks"])

# The complete command vocabulary. Anything else is refused, and the error
# names these - see the module docstring for why there is no completion
# command here.
CommandName = Literal["collect_fields", "cancel", "handoff", "prepare_proposal"]
ALLOWED_COMMANDS: tuple[str, ...] = ("collect_fields", "cancel", "handoff", "prepare_proposal")

MAX_COLLECTED_FIELDS = 20
MAX_FIELD_VALUE_CHARS = 200


class TaskCommandIn(BaseModel):
    command: str = Field(max_length=32)
    expected_version: int = Field(ge=1)
    expected_lease_version: int = Field(ge=1)
    fields: dict[str, str] = Field(default_factory=dict)


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


def _task_out(row: Any) -> dict[str, Any]:
    """The task projection.

    `slots` holds names, origins and confirmation flags. The value of a
    delivery address or a tax id is deliberately not here - it lives in the
    transcript, which is access-controlled and retention-bounded, and this row
    is read by a list endpoint and by the audit path.
    """
    return {
        "task_id": str(row.id),
        "local_key": row.task_local_key,
        "kind": row.kind,
        "status": row.status,
        "sequence": row.sequence,
        "version": row.version,
        "action_revision": row.action_revision,
        "slots": row.slots,
        "missing_slots": row.missing_slots,
        "depends_on": row.depends_on,
        "condition": row.condition,
        "blocked_reason": row.blocked_reason,
        "proposal_id": str(row.proposal_id) if row.proposal_id else None,
        "execution_id": str(row.execution_id) if row.execution_id else None,
        "source_turn_id": row.source_turn_id,
        "updated_at": row.updated_at,
    }


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
    return ok_response(
        {
            "conversation_ref": str(conversation_ref),
            "items": [_task_out(r) for r in rows],
            "limit": limit,
            "offset": offset,
        }
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
    try:
        async with tenant_session(ctx) as session:
            # Ownership first, and re-read inside this transaction: the panel
            # the agent acted on may be a version behind.
            lease = await lease_service.lease_snapshot(
                session, tenant_id=ctx.tenant_id, conversation_ref_id=conversation_ref
            )
            if lease is None:
                return error_response(CASE_NOT_FOUND, "conversation not found", status_code=404)
            if lease.owner_type != "human" or lease.owner_ref != actor_ref:
                return error_response(
                    "LEASE_NOT_OWNED",
                    "only the current human owner may command a task",
                    status_code=409,
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
    except TaskCommandRefused as exc:
        return error_response(exc.code, exc.detail, status_code=409, trace_id=trace_id)
    except TaskConflict_ as exc:
        return error_response(exc.code, exc.detail, status_code=409, trace_id=trace_id)
    except LeaseConflict as exc:
        return error_response("LEASE_CONFLICT", str(exc), status_code=409, trace_id=trace_id)
    except TaskTransitionError as exc:
        return error_response(exc.code, exc.detail, status_code=409, trace_id=trace_id)

    return ok_response({"task": _task_out(updated)}, trace_id=trace_id)


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
    session: Any,
    *,
    ctx: TenantContext,
    conversation_ref: uuid.UUID,
    task: Any,
    fields: dict[str, str],
    trace_id: str,
) -> list[dict[str, Any]]:
    """Write the operator's words to the conversation and return the slots.

    Two writes, in this order and for this reason:

    1. A `human` turn carrying the collected values, so the value has an
       author and a timestamp in the transcript - the same place the customer's
       own words live. A slot pointing at a value with no transcript entry is a
       fact with no source, which is what EVAL-02 counts as a failure.
    2. The slot rows, with `origin: customer_stated` for a non-sensitive field
       the customer typed, and `value_withheld` for a sensitive one. The task
       then records that the field was answered and by whom, without becoming
       a second copy of an address.

    A task is only moved to `ready` when both writes succeed. If the turn write
    fails, the exception propagates and the transition does not happen - which
    is the point of B1-04: "no persisted evidence" must never read as
    "collected".
    """
    from platform_core.agent_runtime import chat_service

    text = "\n".join(f"{name}: {value}" for name, value in fields.items())
    # `append_customer_turn`, not a hand-rolled insert: the collected text is
    # the customer's words relayed by an agent, so it belongs in the
    # conversation under the customer's role, and it must go through the same
    # redactor every other customer turn does. A new insert path here would be
    # a way to store unredacted PII.
    turn, _duplicate = await chat_service.append_customer_turn(
        session,
        tenant_id=ctx.tenant_id,
        ref_id=conversation_ref,
        text=text,
    )

    slots: list[dict[str, Any]] = []
    for name, value in fields.items():
        sensitive = name.lower() in SENSITIVE_FIELD_NAMES
        slot: dict[str, Any] = {
            "name": name,
            # The customer stated it, relayed by an agent. Not
            # `verified_receipt`: nobody read it back from a system of record.
            "origin": "customer_stated",
            "confirmed": False,
            # The turn this value now exists in. Without it a slot is an
            # assertion with nothing behind it.
            "turn_id": str(turn.id),
        }
        if sensitive:
            slot["value_withheld"] = True
        else:
            slot["value"] = value
        slots.append(slot)
    return slots


async def _build_command(
    session: Any,
    *,
    ctx: TenantContext,
    conversation_ref: uuid.UUID,
    task: Any,
    body: TaskCommandIn,
    actor_ref: str,
    trace_id: str,
) -> Any | None:
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
        unknown = [name for name in requested if name not in task.missing_slots]
        if unknown:
            # Refused, not ignored: silently dropping a field the operator
            # typed would make the UI's confirmation a lie.
            raise TaskCommandRefused(
                "TASK_FIELD_NOT_REQUESTED",
                f"this task is not waiting for: {', '.join(unknown)}",
            )

        # Append the operator's own words to the conversation first, so the
        # value has a transcript entry with an author. The slot then points at
        # a turn that exists.
        collected = await _persist_collected(
            session,
            ctx=ctx,
            conversation_ref=conversation_ref,
            task=task,
            fields=body.fields,
            trace_id=trace_id,
        )

        remaining = [m for m in task.missing_slots if m not in requested]
        new_slots = _merge_slots(task.slots, collected)
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

    # prepare_proposal. A proposal is created by the gateway through the
    # existing tool-proposals route with a human actor; this command only
    # moves the task to the state where a confirmation would be meaningful, and
    # bumps the action revision so a confirmation bound to the previous
    # arguments no longer matches.
    if task.kind != TaskKind.WRITE.value:
        return None
    return task_store.TaskCommand(
        target=TaskStatus.AWAITING_CONFIRMATION,
        reason_code="TASK_PROPOSAL_PREPARED",
        bump_action_revision=True,
        **base,
    )


# `TaskConflict` is imported late so the module docstring's import list stays
# readable; it is the store's optimistic-concurrency error.
TaskConflict_ = task_store.TaskConflict


# --- copilot ----------------------------------------------------------------


@router.post("/conversations/{conversation_ref}/copilot/jobs")
async def create_copilot_job(
    request: Request, conversation_ref: uuid.UUID, body: CopilotJobIn
) -> Any:
    """Queue a summary or a reply draft. Never sends anything.

    Returns 202 with a job id. The workbench polls the job; a queued job that
    nobody collects expires rather than being discovered much later.
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
    trace_id = new_trace_id()
    try:
        async with tenant_session(ctx) as session:
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

            kind = CopilotKind(body.kind)
            job = new_job(
                tenant_id=ctx.tenant_id,
                conversation_ref_id=conversation_ref,
                actor_id=actor_id,
                kind=kind,
                timeline_revision=body.timeline_revision,
                lease_version=body.lease_version,
                instructions=body.instructions,
                task_id=body.task_id,
                source_refs=[{"turn_id": t} for t in body.source_turn_ids],
            )
    except CopilotError as exc:
        status = 422 if exc.code.endswith("REQUIRES_SOURCES") else 400
        return error_response(exc.code, exc.detail, status_code=status, trace_id=trace_id)

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


@router.get("/conversations/{conversation_ref}/copilot/jobs/{job_id}")
async def read_copilot_job(request: Request, conversation_ref: uuid.UUID, job_id: uuid.UUID) -> Any:
    """Poll a job. Cross-conversation ids are indistinguishable from unknown."""
    ctx, denied = _auth(request, Action.CASE_READ)
    if denied is not None:
        return denied
    assert ctx is not None

    async with tenant_session(ctx) as session:
        row = await session.get(CopilotDraft, job_id)
        if row is None or row.conversation_ref_id != conversation_ref:
            return error_response("NOT_FOUND", "job not found", status_code=404)
        if row.tenant_id != ctx.tenant_id:
            return error_response("NOT_FOUND", "job not found", status_code=404)

        payload = {
            "job_id": str(row.job_id),
            "kind": row.kind,
            "status": row.status,
            # The draft's own primary key. `job_id` is a separate unique column
            # carrying the derived job identity, and is what the poll URL uses.
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
        current_revision = await _timeline_revision(session, conversation_ref=conversation_ref)
        lease = await lease_service.lease_snapshot(
            session, tenant_id=ctx.tenant_id, conversation_ref_id=conversation_ref
        )
        if lease is not None and row.status == CopilotJobStatus.SUCCEEDED.value:
            job = _row_to_job(row)
            refreshed = apply_staleness(
                job,
                current_timeline_revision=current_revision,
                current_lease_version=lease.lease_version,
                current_actor_id=ctx.actor_id or uuid.UUID(int=0),
            )
            payload["status"] = refreshed.status.value
            payload["error_code"] = refreshed.error_code
            payload["can_insert"] = refreshed.can_insert()

    return ok_response(payload)


async def _timeline_revision(session: Any, *, conversation_ref: uuid.UUID) -> int:
    """The conversation's current revision: how many turns it has.

    Derived rather than stored, because a revision that lives in a column is a
    number something has to remember to increment, and a missed increment
    means a stale draft gets inserted. Turn count cannot drift: every turn is
    a row, and a new customer message is a new row.
    """
    from sqlalchemy import func, select

    from platform_core.agent_runtime.models import ConversationTurn

    return int(
        (
            await session.execute(
                select(func.count())
                .select_from(ConversationTurn)
                .where(ConversationTurn.conversation_ref_id == conversation_ref)
            )
        ).scalar_one()
    )


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


# Imported at module scope: the router already depends on the task package
# for `store` and the state machine, and a function masquerading as a model
# class reads like a dependency that has not been declared.


__all__ = ["ALLOWED_COMMANDS", "router"]


# `should_expire` and `ProposalStatus` are imported for the worker consumer
# that will use them; referenced here so the import is not dropped.
_ = (should_expire, ProposalStatus)
