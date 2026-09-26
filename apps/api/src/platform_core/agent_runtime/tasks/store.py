"""Task persistence: idempotency, optimistic concurrency, and the event log.

Four behaviours here are load-bearing, and each exists because the obvious
alternative produces a specific, observed failure:

1. **`create_or_get` returns an existing row for a repeated identity.**
   The unique key is (tenant, conversation, source_turn, task_local_key). A
   webhook redelivery, a worker restart and a replay all collide on it and get
   the original task back, so "the customer asked twice" does not become two
   tasks and two external calls.

2. **A same-key-different-content retry is a conflict, not a duplicate.**
   The stored `content_hash` is compared; a mismatch raises rather than
   silently keeping the old row. Without that check, a retry carrying a
   corrected intent would be discarded as a duplicate and the correction would
   vanish with no trace - the worst of both outcomes.

3. **Terminal transitions require evidence.** `mark_terminal` refuses to write
   `succeeded` without a `completion_evidence` value, and that value can only
   be a verified tool receipt id or a recorded human action. This is the point
   at which "the LLM thinks it is done" is structurally unable to become
   "the platform believes it is done".

4. **Every transition appends an event.** The current status alone cannot
   answer "why did this become needs_human", and after a handoff the actor who
   moved it is the first thing an operator asks about.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import dataclass
from typing import Any, cast

from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from platform_core.agent_runtime.tasks.models import (
    ConversationTask,
    ConversationTaskEvent,
    SemanticAssessmentRow,
)
from platform_core.agent_runtime.tasks.state_machine import (
    TaskKind,
    TaskStatus,
    TaskTransitionError,
    check_transition,
)

# What may appear in `completion_evidence`. A prefix convention rather than a
# foreign key, because the two real sources live in different modules
# (tool_gateway executions, audit-recorded human actions) and a cross-module
# FK would couple them.
EVIDENCE_VERIFIED_RECEIPT = "tool_receipt:"
EVIDENCE_HUMAN_ACTION = "human_action:"

EVIDENCE_PREFIXES = (EVIDENCE_VERIFIED_RECEIPT, EVIDENCE_HUMAN_ACTION)


class TaskConflict(Exception):
    """A concurrent or contradictory write. The caller re-reads and retries."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


def _value_digest(value: Any) -> str:
    """A stable digest of a slot value, or a marker for its absence.

    Why the value has to be in the hash: the hash is what makes a *replayed or
    corrected* turn distinguishable from the original. Excluding values meant
    `SO-1` and `SO-2` - two different orders from the same customer in the same
    turn - hashed identically, so `create_or_get` returned the first task and
    the second order's query was silently dropped. The comment on an earlier
    revision claimed values were excluded on purpose; the reasoning was about
    the *stored* row, not the *hash*, and applying it to the hash is what
    produced that defect.

    Why a digest and not the value: `content_hash` is stored beside the task
    and compared on every redelivery, so a plain value would be a copy of
    customer data in a column nothing needs to read. The digest is one-way, so
    a hash comparison detects "this is a different order" without being able
    to say which order it was.

    A value that was never stored - a withheld sensitive slot, or an inferred
    one - hashes as the absence marker. That is correct rather than lossy: two
    tasks that both withheld their address really are indistinguishable from
    each other by their content, and the identity of the withheld value is
    carried by the transcript, not by this column.
    """
    if value is None:
        return "-"
    canonical = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()[:32]


def content_hash(
    *,
    kind: TaskKind,
    slots: list[dict[str, Any]],
    missing_slots: list[str],
    condition: dict[str, Any] | None,
) -> str:
    """Hash of what the task *means*, not of the row.

    Sequence, version and timestamps are excluded on purpose: they change when
    the same logical task is re-derived, and including them would make every
    redelivery look like a different task. Slot *values* are included as
    digests - see `_value_digest` for why, and what it costs.
    """
    canonical = json.dumps(
        {
            "kind": kind.value,
            "slots": [
                {
                    "name": s.get("name"),
                    "origin": s.get("origin"),
                    "confirmed": s.get("confirmed"),
                    # `None` for a withheld or inferred slot, which is exactly
                    # what `_value_digest` then hashes as the absence marker.
                    "value": _value_digest(s.get("value")),
                }
                for s in slots
            ],
            "missing_slots": sorted(missing_slots),
            "condition": condition,
        },
        sort_keys=True,
        ensure_ascii=False,
        default=str,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def make_local_key(kind: TaskKind, ordinal: int) -> str:
    """Server-generated, stable for a given (turn, kind, ordinal).

    Not model-chosen: a model that picked this key could collide with another
    task's key and make one of them disappear into the idempotency path.
    """
    return f"{kind.value}-{ordinal}"


@dataclass(frozen=True)
class TaskCommand:
    """A requested state change."""

    target: TaskStatus
    reason_code: str
    actor_type: str = "system"
    actor_ref: str | None = None
    trace_id: str | None = None
    expected_version: int | None = None
    completion_evidence: str | None = None
    blocked_reason: str | None = None
    missing_slots: list[str] | None = None
    # Replace the slot set. Only `collect_fields` sets it, and only with slots
    # that carry a source and a confirmation flag - a caller that could write
    # a bare value here would be a way to assert a fact with no provenance.
    slots: list[dict[str, Any]] | None = None
    # When set, the action's arguments changed: bump the revision so any
    # confirmation bound to the old one stops matching.
    bump_action_revision: bool = False


async def create_or_get(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    conversation_ref_id: uuid.UUID,
    source_turn_id: str,
    task_local_key: str,
    kind: TaskKind,
    status: TaskStatus,
    slots: list[dict[str, Any]] | None = None,
    missing_slots: list[str] | None = None,
    depends_on: list[str] | None = None,
    condition: dict[str, Any] | None = None,
    blocked_reason: str | None = None,
    assessment_id: uuid.UUID | None = None,
    sequence: int = 0,
    trace_id: str | None = None,
) -> tuple[ConversationTask, bool]:
    """Create a task, or return the existing one for this identity.

    Returns `(task, created)`. `created` is False for a redelivery, and the
    caller must not treat that as an error - it is the normal path for a
    replayed turn.
    """
    slots = slots or []
    missing = missing_slots or []
    digest = content_hash(kind=kind, slots=slots, missing_slots=missing, condition=condition)

    existing = (
        await session.execute(
            select(ConversationTask).where(
                ConversationTask.tenant_id == tenant_id,
                ConversationTask.conversation_ref_id == conversation_ref_id,
                ConversationTask.source_turn_id == source_turn_id,
                ConversationTask.task_local_key == task_local_key,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        if existing.content_hash != digest:
            # Same identity, different meaning. Silently keeping the old row
            # would drop a correction; overwriting would rewrite history.
            raise TaskConflict(
                "TASK_IDENTITY_CONTENT_MISMATCH",
                f"{task_local_key} exists with different content",
            )
        return existing, False

    now = int(time.time())
    task = ConversationTask(
        tenant_id=tenant_id,
        conversation_ref_id=conversation_ref_id,
        source_turn_id=source_turn_id,
        assessment_id=assessment_id,
        task_local_key=task_local_key,
        sequence=sequence,
        kind=kind.value,
        status=status.value,
        version=1,
        action_revision=1,
        content_hash=digest,
        depends_on=depends_on or [],
        condition=condition,
        slots=slots,
        missing_slots=missing,
        blocked_reason=blocked_reason,
        created_at=now,
        updated_at=now,
    )
    session.add(task)
    try:
        await session.flush()
    except IntegrityError as exc:
        # A concurrent creator won the race. Their row is authoritative; ours
        # is discarded and theirs returned.
        await session.rollback()
        again = (
            await session.execute(
                select(ConversationTask).where(
                    ConversationTask.tenant_id == tenant_id,
                    ConversationTask.conversation_ref_id == conversation_ref_id,
                    ConversationTask.source_turn_id == source_turn_id,
                    ConversationTask.task_local_key == task_local_key,
                )
            )
        ).scalar_one_or_none()
        if again is None:
            raise TaskConflict("TASK_CREATE_RACE", str(exc)) from exc
        return again, False

    await append_event(
        session,
        tenant_id=tenant_id,
        task=task,
        from_status=None,
        to_status=status,
        reason_code="TASK_CREATED",
        actor_type="system",
        trace_id=trace_id,
    )
    return task, True


async def append_event(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    task: ConversationTask,
    from_status: str | None,
    to_status: str,
    reason_code: str,
    actor_type: str,
    actor_ref: str | None = None,
    trace_id: str | None = None,
) -> ConversationTaskEvent:
    """Append one transition. The only writer of the event log."""
    current_max = (
        await session.execute(
            select(ConversationTaskEvent.sequence)
            .where(
                ConversationTaskEvent.tenant_id == tenant_id,
                ConversationTaskEvent.task_id == task.id,
            )
            .order_by(ConversationTaskEvent.sequence.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    event = ConversationTaskEvent(
        tenant_id=tenant_id,
        task_id=task.id,
        conversation_ref_id=task.conversation_ref_id,
        sequence=(current_max or 0) + 1,
        from_status=from_status,
        to_status=to_status,
        actor_type=actor_type,
        actor_ref=actor_ref,
        reason_code=reason_code,
        trace_id=trace_id,
        from_version=task.version,
        to_version=task.version + 1,
        created_at=int(time.time()),
    )
    session.add(event)
    await session.flush()
    return event


async def transition(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    task: ConversationTask,
    command: TaskCommand,
) -> ConversationTask:
    """Apply a command, enforcing the state machine and optimistic concurrency.

    The UPDATE is a compare-and-set on `version`, so two workers racing on the
    same task produce one winner and one `TASK_VERSION_CONFLICT` rather than
    two transitions and a duplicated external call.
    """
    current = TaskStatus(task.status)
    kind = TaskKind(task.kind)

    if command.expected_version is not None and command.expected_version != task.version:
        raise TaskConflict(
            "TASK_VERSION_CONFLICT", f"expected {command.expected_version}, have {task.version}"
        )

    # The state machine owns the transition rules, including the terminal
    # guarantee - re-checking here would be a second copy that could drift.
    check_transition(current, command.target, kind)

    if command.target is TaskStatus.SUCCEEDED:
        _require_evidence(command.completion_evidence)

    new_version = task.version + 1
    values: dict[str, Any] = {
        "status": command.target.value,
        "version": new_version,
        "updated_at": int(time.time()),
    }
    if command.blocked_reason is not None:
        values["blocked_reason"] = command.blocked_reason
    if command.missing_slots is not None:
        values["missing_slots"] = command.missing_slots
    if command.slots is not None:
        # Guarded here rather than at the router: a slot without an origin is
        # an unsourced value, and this is the last place every write passes
        # through.
        for slot in command.slots:
            if not slot.get("origin"):
                raise TaskConflict(
                    "TASK_SLOT_WITHOUT_ORIGIN",
                    f"slot {slot.get('name')!r} has no origin",
                )
        values["slots"] = command.slots
    if command.bump_action_revision:
        values["action_revision"] = task.action_revision + 1
    if command.completion_evidence is not None:
        values["completion_evidence"] = command.completion_evidence

    result = await session.execute(
        update(ConversationTask)
        .where(
            ConversationTask.tenant_id == tenant_id,
            ConversationTask.id == task.id,
            ConversationTask.version == task.version,
        )
        .values(**values)
    )
    # `AsyncSession.execute` is typed as returning `Result[Any]`, which does
    # not expose `rowcount`; a DML statement actually returns a `CursorResult`.
    # Same cast as `agent_runtime.abandoned._affected`.
    affected = int(cast(CursorResult[Any], result).rowcount or 0)
    if affected != 1:
        # Someone else advanced the version between our read and our write.
        await session.refresh(task)
        raise TaskConflict("TASK_VERSION_CONFLICT", "concurrent update")

    for key, value in values.items():
        setattr(task, key, value)

    await append_event(
        session,
        tenant_id=tenant_id,
        task=task,
        from_status=current.value,
        to_status=command.target.value,
        reason_code=command.reason_code,
        actor_type=command.actor_type,
        actor_ref=command.actor_ref,
        trace_id=command.trace_id,
    )
    return task


def _require_evidence(evidence: str | None) -> None:
    """A task cannot be marked done without proof of what actually happened."""
    if not evidence:
        raise TaskTransitionError(
            "TASK_COMPLETION_EVIDENCE_REQUIRED",
            "succeeded requires a verified receipt or a recorded human action",
        )
    if not evidence.startswith(EVIDENCE_PREFIXES):
        raise TaskTransitionError(
            "TASK_COMPLETION_EVIDENCE_INVALID",
            f"evidence must start with one of {EVIDENCE_PREFIXES}",
        )


async def list_tasks(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    conversation_ref_id: uuid.UUID,
    limit: int = 50,
    offset: int = 0,
) -> list[ConversationTask]:
    stmt = (
        select(ConversationTask)
        .where(
            ConversationTask.tenant_id == tenant_id,
            ConversationTask.conversation_ref_id == conversation_ref_id,
        )
        .order_by(ConversationTask.sequence, ConversationTask.created_at)
        .limit(limit)
        .offset(offset)
    )
    return list((await session.execute(stmt)).scalars())


async def get_task(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    task_id: uuid.UUID,
) -> ConversationTask | None:
    return (
        await session.execute(
            select(ConversationTask).where(
                ConversationTask.tenant_id == tenant_id,
                ConversationTask.id == task_id,
            )
        )
    ).scalar_one_or_none()


async def record_assessment(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    conversation_ref_id: uuid.UUID,
    turn_id: str,
    assessment: Any,
) -> SemanticAssessmentRow:
    """Persist one assessment. The only writer of `semantic_assessments`.

    The snapshot comes from `SemanticAssessment.as_snapshot`, which is
    label-only by construction. Nothing here reads a model field directly, so
    a future field carrying customer text cannot reach this table without
    changing that projection first.
    """
    from platform_core.agent_runtime.semantic.contracts import SemanticAssessment

    assert isinstance(assessment, SemanticAssessment)
    snapshot = assessment.as_snapshot()
    row = SemanticAssessmentRow(
        tenant_id=tenant_id,
        conversation_ref_id=conversation_ref_id,
        turn_id=turn_id,
        mode=assessment.mode.value,
        rule_route=assessment.rule_route,
        rule_action=assessment.rule_action,
        model_primary_intent=(
            assessment.model_output.primary_intent.value if assessment.model_output else None
        ),
        agreement=assessment.agreement,
        effective_decision=assessment.effective_decision,
        reason_codes=list(assessment.reason_codes),
        validation_status=assessment.validation_status,
        prompt_version=assessment.prompt_version,
        model_name=assessment.model_name,
        latency_ms=assessment.latency_ms,
        prompt_tokens=assessment.prompt_tokens,
        completion_tokens=assessment.completion_tokens,
        truncated=assessment.truncated,
        snapshot=snapshot,
        created_at=int(time.time()),
    )
    session.add(row)
    await session.flush()
    return row


__all__ = [
    "EVIDENCE_HUMAN_ACTION",
    "EVIDENCE_PREFIXES",
    "EVIDENCE_VERIFIED_RECEIPT",
    "TaskCommand",
    "TaskConflict",
    "append_event",
    "content_hash",
    "create_or_get",
    "get_task",
    "list_tasks",
    "make_local_key",
    "record_assessment",
    "transition",
]
