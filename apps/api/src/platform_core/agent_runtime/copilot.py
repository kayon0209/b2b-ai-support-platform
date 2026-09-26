"""Copilot job lifecycle: queued, running, succeeded, failed, stale, expired.

COP-01 and COP-02 in one module, because the rules below are the same rules
seen from two sides.

**Generation never sends.** There is no code path from a completed job to an
outbound channel. The job produces a `copilot_drafts` row; an agent inserts it
into their own draft and sends through the existing workbench flow, which
re-checks the lease. A summary that could reach a customer by itself would make
"the AI drafted this" indistinguishable from "the AI said this".

**A job is bound to the state it was generated from.** Two bindings:

- `timeline_revision` - bumped when a new customer message lands. A summary of
  a conversation that has since gained a message is a summary of a
  conversation that no longer exists, and inserting it would answer a question
  the customer has moved on from.
- `lease_version` - bumped when ownership changes. A handoff between queueing
  and completion means the result belongs to a conversation somebody else now
  owns.

Either mismatch marks the job `stale`. Stale is not failed: the work was done
and the answer may still be useful to read, it just may not be inserted. The
distinction matters to an agent deciding whether to re-generate.

**An edited draft is never overwritten.** `edited_by_human` is checked before
any regeneration writes, because the one thing worse than a stale suggestion
is losing what a person typed.

**A replayed request does not pay twice.** `job_id` is derived from
(tenant, conversation, actor, kind, timeline_revision, lease_version), and the
unique constraint on (tenant_id, job_id) means the second request returns the
first job.
"""

from __future__ import annotations

import hashlib
import time
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class CopilotJobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    STALE = "stale"
    EXPIRED = "expired"


class CopilotKind(StrEnum):
    SUMMARY = "summary"
    REPLY = "reply"


# The outbox event type the workbench route writes and the worker's copilot
# consumer reads. Named here, like the shadow one, so a producer that invented
# its own string would enqueue work nothing consumes - a silent no-op that
# looks exactly like "the model is slow".
COPILOT_EVENT_TYPE = "copilot.generate_requested"

# A queued job nobody has picked up within this window is expired rather than
# left to be discovered later. It is well beyond any observed generation time
# and short enough that the workbench does not show a "generating" spinner for
# a request whose result is never coming.
JOB_TTL_SECONDS = 120

# A succeeded result older than this is stale on read even if nothing changed:
# the agent may have moved on, and a two-hour-old summary inserted into a fresh
# reply is a mistake waiting to happen.
RESULT_FRESH_SECONDS = 30 * 60

MAX_INSTRUCTIONS_CHARS = 500

REASON_TIMELINE_MOVED = "COPILOT_TIMELINE_MOVED"
REASON_LEASE_CHANGED = "COPILOT_LEASE_CHANGED"
REASON_ACTOR_CHANGED = "COPILOT_ACTOR_CHANGED"
REASON_EDITED = "COPILOT_DRAFT_EDITED"
REASON_EXPIRED = "COPILOT_JOB_EXPIRED"


class CopilotError(Exception):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class CopilotJob:
    """A queued generation request and whatever became of it."""

    job_id: uuid.UUID
    tenant_id: uuid.UUID
    conversation_ref_id: uuid.UUID
    actor_id: uuid.UUID
    kind: CopilotKind
    status: CopilotJobStatus
    timeline_revision: int
    lease_version: int
    instructions: str = ""
    task_id: uuid.UUID | None = None
    source_refs: list[dict[str, Any]] = field(default_factory=list)
    draft_id: uuid.UUID | None = None
    body: str = ""
    edited_by_human: bool = False
    error_code: str | None = None
    created_at: int = 0
    updated_at: int = 0
    version: int = 1

    def as_dict(self) -> dict[str, Any]:
        """The workbench payload.

        `instructions` is the agent's own text, so returning it is safe;
        `body` is model output that may quote the customer, which is exactly
        why it lives in a table with RLS rather than in a log or a URL.
        """
        return {
            "job_id": str(self.job_id),
            "kind": self.kind.value,
            "status": self.status.value,
            "timeline_revision": self.timeline_revision,
            "lease_version": self.lease_version,
            "draft_id": str(self.draft_id) if self.draft_id else None,
            "body": self.body,
            "source_refs": self.source_refs,
            "edited_by_human": self.edited_by_human,
            "error_code": self.error_code,
            "can_insert": self.can_insert(),
        }

    def can_insert(self) -> bool:
        """Whether the workbench may offer "insert into draft".

        Only a succeeded, unedited, still-current job. Everything else needs a
        human decision, and the button is disabled with the reason shown
        rather than hidden.
        """
        return self.status is CopilotJobStatus.SUCCEEDED and not self.edited_by_human


def derive_job_id(
    *,
    tenant_id: uuid.UUID,
    conversation_ref_id: uuid.UUID,
    actor_id: uuid.UUID,
    kind: CopilotKind,
    timeline_revision: int,
    lease_version: int,
) -> uuid.UUID:
    """A deterministic job id, so a replayed request is the same job.

    Derived from the state the generation depends on, not from a fresh uuid:
    a double-clicked "generate" must not queue two model calls for one
    revision, and a retry after a client timeout must not either.
    """
    canonical = "|".join(
        (
            str(tenant_id),
            str(conversation_ref_id),
            str(actor_id),
            kind.value,
            str(timeline_revision),
            str(lease_version),
        )
    )
    digest = hashlib.sha256(canonical.encode()).hexdigest()
    return uuid.UUID(hex=digest[:32])


def new_job(
    *,
    tenant_id: uuid.UUID,
    conversation_ref_id: uuid.UUID,
    actor_id: uuid.UUID,
    kind: CopilotKind,
    timeline_revision: int,
    lease_version: int,
    instructions: str = "",
    task_id: uuid.UUID | None = None,
    source_refs: list[dict[str, Any]] | None = None,
    now: int | None = None,
) -> CopilotJob:
    """Build a queued job. Refuses input it cannot honour rather than
    truncating it silently."""
    text = (instructions or "").strip()
    if len(text) > MAX_INSTRUCTIONS_CHARS:
        # Refused, not truncated: a silently shortened instruction produces a
        # summary of something the agent did not ask for, and there is no way
        # for them to tell from the result.
        raise CopilotError("COPILOT_INSTRUCTIONS_TOO_LONG", f"{len(text)} characters")

    if kind is CopilotKind.SUMMARY and not source_refs:
        # A summary with no sources is an assertion about the conversation
        # that cannot be checked. COP-01 requires a visible source for every
        # fact, so this is refused at creation rather than rendered as an
        # unsourced paragraph.
        raise CopilotError("COPILOT_SUMMARY_REQUIRES_SOURCES", "a summary must name its turns")

    stamp = int(time.time()) if now is None else now
    return CopilotJob(
        job_id=derive_job_id(
            tenant_id=tenant_id,
            conversation_ref_id=conversation_ref_id,
            actor_id=actor_id,
            kind=kind,
            timeline_revision=timeline_revision,
            lease_version=lease_version,
        ),
        tenant_id=tenant_id,
        conversation_ref_id=conversation_ref_id,
        actor_id=actor_id,
        kind=kind,
        status=CopilotJobStatus.QUEUED,
        timeline_revision=timeline_revision,
        lease_version=lease_version,
        instructions=text,
        task_id=task_id,
        source_refs=list(source_refs or []),
        created_at=stamp,
        updated_at=stamp,
    )


def staleness(
    job: CopilotJob,
    *,
    current_timeline_revision: int,
    current_lease_version: int,
    current_actor_id: uuid.UUID,
    now: int | None = None,
) -> str | None:
    """Why this job can no longer be inserted, or None if it can.

    Checked in a fixed order so the reported reason is the first thing that
    actually changed - an operator reading "the conversation moved" when the
    real cause was a handoff would look in the wrong place.
    """
    if job.timeline_revision != current_timeline_revision:
        return REASON_TIMELINE_MOVED
    if job.lease_version != current_lease_version:
        return REASON_LEASE_CHANGED
    if job.actor_id != current_actor_id:
        return REASON_ACTOR_CHANGED
    if job.edited_by_human:
        return REASON_EDITED
    stamp = int(time.time()) if now is None else now
    if job.status is CopilotJobStatus.SUCCEEDED and stamp - job.updated_at > RESULT_FRESH_SECONDS:
        return REASON_EXPIRED
    return None


def should_expire(job: CopilotJob, *, now: int | None = None) -> bool:
    """Whether a job nobody collected has aged out."""
    if job.status not in (CopilotJobStatus.QUEUED, CopilotJobStatus.RUNNING):
        return False
    stamp = int(time.time()) if now is None else now
    return stamp - job.created_at > JOB_TTL_SECONDS


def apply_staleness(
    job: CopilotJob,
    *,
    current_timeline_revision: int,
    current_lease_version: int,
    current_actor_id: uuid.UUID,
    now: int | None = None,
) -> CopilotJob:
    """Mark a job stale when the world moved under it.

    A `failed` or `expired` job keeps its status: it did not produce anything,
    and calling it stale would suggest a result exists.
    """
    if job.status in (CopilotJobStatus.FAILED, CopilotJobStatus.EXPIRED):
        return job
    reason = staleness(
        job,
        current_timeline_revision=current_timeline_revision,
        current_lease_version=current_lease_version,
        current_actor_id=current_actor_id,
        now=now,
    )
    if reason is None:
        return job
    return replace(
        job,
        status=CopilotJobStatus.STALE,
        error_code=reason,
        updated_at=int(time.time()) if now is None else now,
    )


def replace(job: CopilotJob, **changes: Any) -> CopilotJob:
    from dataclasses import replace as _replace

    return _replace(job, **changes)


__all__ = [
    "COPILOT_EVENT_TYPE",
    "JOB_TTL_SECONDS",
    "MAX_INSTRUCTIONS_CHARS",
    "REASON_ACTOR_CHANGED",
    "REASON_EDITED",
    "REASON_EXPIRED",
    "REASON_LEASE_CHANGED",
    "REASON_TIMELINE_MOVED",
    "RESULT_FRESH_SECONDS",
    "CopilotError",
    "CopilotJob",
    "CopilotJobStatus",
    "CopilotKind",
    "apply_staleness",
    "derive_job_id",
    "new_job",
    "should_expire",
    "staleness",
]
