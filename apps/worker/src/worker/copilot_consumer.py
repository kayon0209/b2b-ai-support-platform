"""Copilot draft consumer: outbox row -> generated body on a draft row.

The workbench route persists a `queued` draft and enqueues an outbox row; this
turns it into a body. Three properties are the acceptance criteria:

- **It never sends.** There is no outbound channel, no sender and no dispatch
  in this module. The result is written to `copilot_drafts.body` and the
  workbench decides what to do with it. A generation that could reach a
  customer by itself would make "the AI drafted this" indistinguishable from
  "the AI said this".
- **It never overwrites a human edit.** `edited_by_human` is checked before the
  write, so a person typing in the box while a generation is in flight keeps
  their text.
- **Staleness is decided by the server.** The row is marked `stale` when the
  timeline moved, the lease changed or the actor differs - the same three
  conditions `copilot.staleness` names - so a result that arrived too late is
  readable but not insertable.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from observability import JsonLogger
from observability_metrics import get_metrics
from platform_core.agent_runtime.copilot import (
    COPILOT_EVENT_TYPE,
    JOB_TTL_SECONDS,
    CopilotJob,
    CopilotJobStatus,
    CopilotKind,
    apply_staleness,
    should_expire,
)
from platform_core.agent_runtime.semantic.modes import FLAG_COPILOT
from platform_core.identity import lease_service
from platform_core.identity.tenant_context import TenantContext
from platform_core.outbox import OutboxEvent, OutboxStatus

logger = JsonLogger("platform.worker")

# Same reasoning as the classification budget: a draft an agent is waiting on
# is worth more than a shadow comparison, and neither is worth a stalled queue.
COPILOT_DEADLINE_SECONDS = 8.0
COPILOT_MAX_RETRIES = 0

STALE_COPILOT_SECONDS = 600
COPILOT_BATCH = 5
COPILOT_IN_FLIGHT = "processing"

# The prompts. Kept here rather than in configuration so a prompt change is a
# visible diff, and versioned so an evaluation can name what it measured.
SUMMARY_SYSTEM_PROMPT = (
    "Summarise the conversation for a support agent who has not read it. "
    "State only what the customer asked, what has been verified, and what is "
    "still outstanding. Do not promise anything and do not invent facts. "
    "Reply with plain text."
)
REPLY_SYSTEM_PROMPT = (
    "Draft a reply to the customer's most recent message. Use only the "
    "verified facts provided. If something is missing, say so rather than "
    "guessing. Do not commit to a commercial term. Reply with plain text."
)


@dataclass(frozen=True)
class ClaimedCopilot:
    event_id: uuid.UUID
    tenant_id: uuid.UUID


@dataclass(frozen=True)
class CopilotWorkItem:
    event_id: uuid.UUID
    tenant_id: uuid.UUID
    job_id: uuid.UUID
    conversation_ref_id: uuid.UUID
    kind: CopilotKind
    timeline_revision: int
    lease_version: int


def chat_provider(deps: Any) -> Any | None:
    """The `ChatProvider` from the worker's dependency bundle.

    Same rule as the shadow consumer: `generator` is an answer generator and
    has no `complete`. Returns None rather than guessing.
    """
    extra = getattr(deps, "extra", None) or {}
    provider = extra.get("chat")
    if provider is not None and hasattr(provider, "complete"):
        return provider
    return None


async def claim_copilot_jobs(
    session: AsyncSession, *, batch: int = COPILOT_BATCH
) -> list[ClaimedCopilot]:
    """Claim queued generation requests with SKIP LOCKED."""
    rows = (
        await session.execute(
            select(OutboxEvent.id, OutboxEvent.event_id, OutboxEvent.tenant_id)
            .where(
                OutboxEvent.event_type == COPILOT_EVENT_TYPE,
                OutboxEvent.status == OutboxStatus.QUEUED.value,
            )
            .order_by(OutboxEvent.created_at, OutboxEvent.id)
            .limit(batch)
            .with_for_update(skip_locked=True)
        )
    ).all()
    claimed: list[ClaimedCopilot] = []
    for row in rows:
        await session.execute(
            update(OutboxEvent)
            .where(OutboxEvent.id == row.id)
            .values(status=COPILOT_IN_FLIGHT, processing_started_at=int(time.time()))
        )
        claimed.append(ClaimedCopilot(event_id=row.event_id, tenant_id=row.tenant_id))
    return claimed


async def reclaim_stale_copilot(session: AsyncSession) -> int:
    """Return long-claimed rows to the queue after a consumer died."""
    cutoff = int(time.time()) - STALE_COPILOT_SECONDS
    result = await session.execute(
        update(OutboxEvent)
        .where(
            OutboxEvent.event_type == COPILOT_EVENT_TYPE,
            OutboxEvent.status == COPILOT_IN_FLIGHT,
            OutboxEvent.processing_started_at < cutoff,
        )
        .values(status=OutboxStatus.QUEUED.value, processing_started_at=None)
    )
    return int(getattr(result, "rowcount", 0) or 0)


async def process_copilot_job(
    session: AsyncSession,
    claimed: ClaimedCopilot,
    *,
    provider: Any | None,
    max_tokens: int = 900,
) -> str:
    """Generate one draft. Returns a closed-vocabulary outcome code.

    Never raises: a failure is recorded on the draft row so the operator sees
    why the spinner stopped, rather than a job stuck in `queued` forever.
    """
    metrics = get_metrics()
    # Queue claims select only metadata on the owner connection. Read the
    # customer-bearing payload after tenant_session has applied FORCE-RLS.
    event = (
        await session.execute(
            select(OutboxEvent).where(
                OutboxEvent.tenant_id == claimed.tenant_id,
                OutboxEvent.event_id == claimed.event_id,
            )
        )
    ).scalar_one_or_none()
    if event is None:
        return "missing"
    work = _work_item(claimed, dict(event.payload or {}))
    if work is None:
        await _finish_event(
            session, claimed, status=OutboxStatus.FAILED.value, error="copilot_payload_unreadable"
        )
        return "failed"

    draft = await _load_draft(session, work)
    if draft is None:
        await _finish_event(
            session, claimed, status=OutboxStatus.FAILED.value, error="draft_missing"
        )
        metrics.inbox_events_total.labels(result="copilot_draft_missing").inc()
        return "failed"

    from platform_core.knowledge import flag_service

    copilot_flag = await flag_service.evaluate(
        session,
        flag_key=FLAG_COPILOT,
        tenant_id=work.tenant_id,
        default=False,
    )
    if not copilot_flag.enabled:
        await _mark_draft(
            session,
            work,
            status=CopilotJobStatus.FAILED.value,
            error="COPILOT_DISABLED",
        )
        await _finish_event(
            session,
            work,
            status=OutboxStatus.FAILED.value,
            error="COPILOT_DISABLED",
        )
        metrics.inbox_events_total.labels(result="copilot_disabled").inc()
        return "failed"

    if draft.edited_by_human:
        # The person has already typed. Their text wins; the generation is
        # discarded rather than merged, because merging generated text into a
        # half-typed reply is neither of the two things anyone wanted.
        await _mark_draft(
            session, work, status=CopilotJobStatus.STALE.value, error="COPILOT_DRAFT_EDITED"
        )
        await _finish_event(session, work, status=OutboxStatus.SENT.value, error="")
        metrics.inbox_events_total.labels(result="copilot_edited").inc()
        return "edited"

    if provider is None:
        await _mark_draft(
            session, work, status=CopilotJobStatus.FAILED.value, error="no_chat_provider"
        )
        await _finish_event(
            session, work, status=OutboxStatus.FAILED.value, error="no_chat_provider"
        )
        metrics.inbox_events_total.labels(result="copilot_no_provider").inc()
        return "failed"

    stale = await _refresh_staleness(
        session,
        work,
        draft,
        body="",
        status=CopilotJobStatus.QUEUED,
    )
    if stale.status is CopilotJobStatus.STALE:
        await _mark_draft(session, work, status=stale.status.value, error=stale.error_code)
        await _finish_event(session, work, status=OutboxStatus.SENT.value, error="")
        metrics.inbox_events_total.labels(result="copilot_stale_before_generation").inc()
        return stale.status.value

    from platform_core.agent_runtime.chat_service import read_timeline_page
    from platform_core.llm.provider import ChatMessage, ProviderRole

    turns, _older = await read_timeline_page(session, ref_id=work.conversation_ref_id, limit=40)
    if not turns:
        await _mark_draft(
            session, work, status=CopilotJobStatus.FAILED.value, error="COPILOT_NO_CONTEXT"
        )
        await _finish_event(session, work, status=OutboxStatus.FAILED.value, error="no_context")
        metrics.inbox_events_total.labels(result="copilot_no_context").inc()
        return "failed"

    system = SUMMARY_SYSTEM_PROMPT if work.kind is CopilotKind.SUMMARY else REPLY_SYSTEM_PROMPT
    transcript = "\n".join(f"[{t.get('role')}] {t.get('text')}" for t in turns if t.get("text"))
    try:
        completion = await provider.complete(
            [
                ChatMessage(role=ProviderRole.SYSTEM, content=system),
                ChatMessage(role=ProviderRole.USER, content=transcript[:6000]),
            ],
            max_tokens=max_tokens,
            temperature=0.0,
        )
    except Exception as exc:  # noqa: BLE001 - recorded on the row, not raised
        code = type(exc).__name__
        await _mark_draft(session, work, status=CopilotJobStatus.FAILED.value, error=code)
        await _finish_event(session, work, status=OutboxStatus.FAILED.value, error=code)
        metrics.inbox_events_total.labels(result="copilot_error").inc()
        logger.warning("copilot_generation_failed", error_code=code)
        return "failed"

    body = (completion.text or "").strip()
    if not body:
        await _mark_draft(
            session, work, status=CopilotJobStatus.FAILED.value, error="COPILOT_EMPTY_OUTPUT"
        )
        await _finish_event(session, work, status=OutboxStatus.FAILED.value, error="empty_output")
        metrics.inbox_events_total.labels(result="copilot_empty").inc()
        return "failed"

    # Staleness decided here rather than only on read: a result that is stale
    # on arrival should not be recorded as `succeeded` and then flip, because a
    # poller that saw `succeeded` may already have rendered it.
    refreshed = await _refresh_staleness(
        session,
        work,
        draft,
        body=body,
        status=CopilotJobStatus.SUCCEEDED,
    )
    await _mark_draft(
        session,
        work,
        status=refreshed.status.value,
        error=refreshed.error_code,
        body=body,
    )
    await _finish_event(session, work, status=OutboxStatus.SENT.value, error="")
    metrics.inbox_events_total.labels(result=f"copilot_{refreshed.status.value}").inc()
    return refreshed.status.value


async def _refresh_staleness(
    session: AsyncSession,
    work: CopilotWorkItem,
    draft: Any,
    *,
    body: str,
    status: CopilotJobStatus,
) -> CopilotJob:
    """Compare the job with the current timeline and human owner."""
    from platform_core.agent_runtime.chat_service import timeline_revision

    current_revision = await timeline_revision(session, ref_id=work.conversation_ref_id)
    lease = await lease_service.lease_snapshot(
        session,
        tenant_id=work.tenant_id,
        conversation_ref_id=work.conversation_ref_id,
    )
    current_lease_version = lease.lease_version if lease is not None else -1
    current_actor_id = uuid.UUID(int=0)
    if lease is not None and lease.owner_type == "human" and lease.owner_ref:
        try:
            current_actor_id = uuid.UUID(lease.owner_ref)
        except ValueError:
            pass
    job = _row_to_copilot(draft, body=body, now=int(time.time()), status=status)
    return apply_staleness(
        job,
        current_timeline_revision=current_revision,
        current_lease_version=current_lease_version,
        current_actor_id=current_actor_id,
        now=int(time.time()),
    )


def _work_item(claimed: ClaimedCopilot, payload: dict[str, Any]) -> CopilotWorkItem | None:
    try:
        job_id = uuid.UUID(str(payload.get("job_id")))
        conversation_ref = uuid.UUID(str(payload.get("conversation_ref")))
        kind = CopilotKind(str(payload.get("kind") or "summary"))
        timeline_revision = int(payload.get("timeline_revision") or 0)
        lease_version = int(payload.get("lease_version") or 0)
    except (TypeError, ValueError):
        return None
    return CopilotWorkItem(
        event_id=claimed.event_id,
        tenant_id=claimed.tenant_id,
        job_id=job_id,
        conversation_ref_id=conversation_ref,
        kind=kind,
        timeline_revision=timeline_revision,
        lease_version=lease_version,
    )


async def _load_draft(session: AsyncSession, claimed: CopilotWorkItem) -> Any | None:
    from platform_core.agent_runtime.tasks.models import CopilotDraft

    return (
        await session.execute(
            select(CopilotDraft).where(
                CopilotDraft.tenant_id == claimed.tenant_id,
                CopilotDraft.job_id == claimed.job_id,
            )
        )
    ).scalar_one_or_none()


async def _mark_draft(
    session: AsyncSession,
    claimed: CopilotWorkItem,
    *,
    status: str,
    error: str | None = None,
    body: str | None = None,
) -> None:
    from platform_core.agent_runtime.tasks.models import CopilotDraft

    values: dict[str, Any] = {"status": status, "updated_at": int(time.time())}
    if error is not None:
        values["error_code"] = error[:63]
    if body is not None:
        values["body"] = body
    await session.execute(
        update(CopilotDraft)
        .where(
            CopilotDraft.tenant_id == claimed.tenant_id,
            CopilotDraft.job_id == claimed.job_id,
        )
        .values(**values)
    )


async def _finish_event(
    session: AsyncSession,
    claimed: ClaimedCopilot | CopilotWorkItem,
    *,
    status: str,
    error: str,
) -> None:
    await session.execute(
        update(OutboxEvent)
        .where(OutboxEvent.tenant_id == claimed.tenant_id, OutboxEvent.event_id == claimed.event_id)
        .values(
            status=status,
            published_at=int(time.time()) if status == OutboxStatus.SENT.value else None,
            processing_started_at=None,
            last_error=error[:255] if error else None,
        )
    )


def _row_to_copilot(
    draft: Any,
    *,
    body: str,
    now: int,
    status: CopilotJobStatus | None = None,
) -> CopilotJob:
    return CopilotJob(
        job_id=draft.job_id,
        tenant_id=draft.tenant_id,
        conversation_ref_id=draft.conversation_ref_id,
        actor_id=draft.actor_id,
        kind=CopilotKind(draft.kind),
        status=status or CopilotJobStatus(draft.status),
        timeline_revision=draft.timeline_revision,
        lease_version=draft.lease_version,
        task_id=draft.task_id,
        source_refs=list(draft.source_refs or []),
        body=body,
        edited_by_human=draft.edited_by_human,
        error_code=draft.error_code,
        created_at=draft.created_at,
        updated_at=now,
        version=draft.version,
    )


def context_for(claimed: ClaimedCopilot) -> TenantContext:
    """The tenant binding a consumer session must be opened with."""
    return TenantContext(
        tenant_id=claimed.tenant_id,
        actor_id=None,
        actor_kind="system",
        role="integration_service",
    )


__all__ = [
    "COPILOT_BATCH",
    "COPILOT_DEADLINE_SECONDS",
    "COPILOT_MAX_RETRIES",
    "JOB_TTL_SECONDS",
    "REPLY_SYSTEM_PROMPT",
    "STALE_COPILOT_SECONDS",
    "SUMMARY_SYSTEM_PROMPT",
    "ClaimedCopilot",
    "chat_provider",
    "claim_copilot_jobs",
    "context_for",
    "process_copilot_job",
    "reclaim_stale_copilot",
    "should_expire",
]
