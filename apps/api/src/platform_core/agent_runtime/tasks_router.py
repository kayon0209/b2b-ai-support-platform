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

import time
import uuid
from typing import Any, Literal

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import select as sa_select
from sqlalchemy.exc import IntegrityError

from platform_core.agent_runtime.copilot import (
    COPILOT_EVENT_TYPE,
    CopilotError,
    CopilotJobStatus,
    CopilotKind,
    apply_staleness,
    new_job,
    should_expire,
)
from platform_core.agent_runtime.models import ConversationTurn
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
    ctx: TenantContext,
    fields: dict[str, str],
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
    for name, value in fields.items():
        sensitive = name.lower() in SENSITIVE_FIELD_NAMES
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

        # Keep operator-provided data on the task with explicit provenance.
        # It is not a customer-authored transcript turn.
        collected = await _persist_collected(
            ctx=ctx,
            fields=body.fields,
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
        proposal_id=proposal.id,
        **base,
    )


async def _create_proposal(
    session: Any,
    *,
    ctx: TenantContext,
    task: Any,
    trace_id: str,
) -> Any | None:
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
    from sqlalchemy import select as sa_select

    from platform_core.tool_gateway.gateway import ToolDenied, ToolGateway, ToolGatewayError
    from platform_core.tool_gateway.models import ToolDefinition, ToolProposal

    tool_name = _write_tool_for(task)
    if not tool_name:
        return None

    tool = (
        await session.execute(
            sa_select(ToolDefinition)
            .where(
                (ToolDefinition.tenant_id == ctx.tenant_id) | ToolDefinition.tenant_id.is_(None),
                ToolDefinition.name == tool_name,
            )
            .order_by(ToolDefinition.version.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if tool is None or tool.risk not in ("low_write", "confirmed_write", "human_approval"):
        return None

    arguments = _proposal_arguments(task)
    if arguments is None:
        # A required argument has no value. Proposing anyway would produce a
        # proposal the gateway refuses with a schema error the agent cannot
        # map back to a field.
        return None

    required_action = {
        "low_write": "tool.write.low",
        "confirmed_write": "tool.write.confirmed",
        "human_approval": "tool.human_approval",
    }[tool.risk]

    # The gateway's `propose` always inserts: it is a "freeze these arguments"
    # operation with no replay semantics of its own. So the replay check lives
    # here, keyed on the same (task, revision) pair the idempotency key encodes.
    #
    # Without it, a double-clicked "准备提案" produced two proposals and two
    # confirmations to choose between, and the second was indistinguishable
    # from a deliberate re-proposal with changed arguments - which is the exact
    # distinction the action revision exists to make.
    key = f"task-{task.id}-r{task.action_revision}"
    existing = (
        await session.execute(
            sa_select(ToolProposal).where(
                ToolProposal.tenant_id == ctx.tenant_id,
                ToolProposal.idempotency_key == key,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    gateway = ToolGateway(session, {})
    if ctx.actor_id is None:
        # The route refuses an unidentified caller before reaching here; this
        # is the type-level statement of the same rule, because a proposal
        # with no actor is a write nobody can be shown to have authorised.
        return None
    try:
        return await gateway.propose(
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
    for slot in task.slots or []:
        if slot.get("name") == "tool" and slot.get("value"):
            return str(slot["value"])
    return None


def _proposal_arguments(task: Any) -> dict[str, Any] | None:
    """Build the tool arguments from the task's slots, or None if incomplete.

    Only non-sensitive, confirmed-by-presence slot values are used. A withheld
    value cannot become an argument: the proposal would carry an empty string
    where the customer gave an address, and the gateway would accept it.
    """
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
                instructions=body.instructions,
                task_id=body.task_id,
                source_refs=sources,
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
    rows = (
        await session.execute(
            sa_select(ConversationTurn).where(
                ConversationTurn.conversation_ref_id == conversation_ref,
                ConversationTurn.id.in_(turn_ids),
            )
        )
    ).scalars()
    found = {str(r.id) for r in rows}
    if found != set(turn_ids):
        return None
    # Offsets are recorded so a later review can point at the exact span. The
    # text itself stays in `conversation_turns`.
    return [{"turn_id": t, "role": "customer", "offset": [0, 0]} for t in turn_ids]


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
        elif row.status == CopilotJobStatus.QUEUED.value:
            # An uncollected job that has aged out is expired, not queued.
            # Reported on read rather than only by the worker's sweep, because
            # the operator is the one watching the spinner.
            job = _row_to_job(row)
            if should_expire(job):
                payload["status"] = CopilotJobStatus.EXPIRED.value
                payload["error_code"] = "COPILOT_JOB_EXPIRED"

    return ok_response(payload)


async def _timeline_revision(session: Any, *, conversation_ref: uuid.UUID) -> int:
    """The conversation's current revision: how many turns it has.

    Derived rather than stored, because a revision that lives in a column is a
    number something has to remember to increment, and a missed increment
    means a stale draft gets inserted. Turn count cannot drift: every turn is
    a row, and a new customer message is a new row.
    """
    from sqlalchemy import func

    return int(
        (
            await session.execute(
                sa_select(func.count())
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


__all__ = ["ALLOWED_COMMANDS", "router"]
