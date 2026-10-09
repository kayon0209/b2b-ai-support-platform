"""Tenant-scoped persistence and aggregation for customer resolution feedback."""

from __future__ import annotations

import hashlib
import json
import time
import uuid

from sqlalchemy import and_, exists, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from platform_core.evaluation.customer_feedback_models import CustomerResolutionFeedbackEvent
from platform_core.evaluation.customer_outcomes import (
    CustomerOutcomeObservation,
    summarize_customer_outcomes,
)

MAX_OUTCOME_OBSERVATIONS = 20_000
MAX_IDEMPOTENCY_KEY_LENGTH = 255


class CustomerFeedbackError(Exception):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(detail or code)
        self.code = code
        self.detail = detail or code


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _request_hash(
    *, event_type: str, conversation_ref_id: uuid.UUID, case_id: uuid.UUID | None
) -> str:
    canonical = json.dumps(
        {
            "case_id": str(case_id) if case_id else None,
            "conversation_ref_id": str(conversation_ref_id),
            "event_type": event_type,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return _sha256(canonical)


async def record_feedback_event(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    conversation_ref_id: uuid.UUID,
    case_id: uuid.UUID | None,
    event_type: str,
    idempotency_key: str,
) -> tuple[CustomerResolutionFeedbackEvent, bool]:
    """Append one prompt/answer event, returning `(row, replayed)`.

    The prompt is unique per tenant conversation. Answers are append-only so a
    changed answer remains auditable; dashboard aggregation uses the latest
    answer after the prompt. No free text or raw idempotency value is stored.
    """
    if event_type not in {"requested", "confirmed", "rejected"}:
        raise CustomerFeedbackError("FEEDBACK_EVENT_INVALID")
    if (
        not isinstance(idempotency_key, str)
        or not 1 <= len(idempotency_key) <= MAX_IDEMPOTENCY_KEY_LENGTH
    ):
        raise CustomerFeedbackError("IDEMPOTENCY_KEY_INVALID")

    request_event = (
        await session.execute(
            select(CustomerResolutionFeedbackEvent).where(
                CustomerResolutionFeedbackEvent.tenant_id == tenant_id,
                CustomerResolutionFeedbackEvent.conversation_ref_id == conversation_ref_id,
                CustomerResolutionFeedbackEvent.event_type == "requested",
            )
        )
    ).scalar_one_or_none()
    if event_type != "requested" and request_event is None:
        raise CustomerFeedbackError("FEEDBACK_NOT_REQUESTED")
    effective_case_id = request_event.case_id if request_event is not None else case_id
    key_hash = _sha256(idempotency_key)
    payload_hash = _request_hash(
        event_type=event_type,
        conversation_ref_id=conversation_ref_id,
        case_id=effective_case_id,
    )
    existing = (
        await session.execute(
            select(CustomerResolutionFeedbackEvent).where(
                CustomerResolutionFeedbackEvent.tenant_id == tenant_id,
                CustomerResolutionFeedbackEvent.idempotency_key_hash == key_hash,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        if existing.request_hash != payload_hash:
            raise CustomerFeedbackError("IDEMPOTENCY_CONFLICT")
        return existing, True

    if event_type == "requested" and request_event is not None:
        # A remount may have a fresh HTTP key. Preserve one exposure event per
        # conversation rather than inflating the no-response denominator.
        return request_event, True

    statement = (
        pg_insert(CustomerResolutionFeedbackEvent)
        .values(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            conversation_ref_id=conversation_ref_id,
            case_id=effective_case_id,
            event_type=event_type,
            idempotency_key_hash=key_hash,
            request_hash=payload_hash,
            occurred_at=int(time.time()),
            source="support_surface",
        )
        .on_conflict_do_nothing()
        .returning(CustomerResolutionFeedbackEvent.id)
    )
    created_id = (await session.execute(statement)).scalar_one_or_none()
    if created_id is not None:
        created = (
            await session.execute(
                select(CustomerResolutionFeedbackEvent).where(
                    CustomerResolutionFeedbackEvent.tenant_id == tenant_id,
                    CustomerResolutionFeedbackEvent.id == created_id,
                )
            )
        ).scalar_one()
        return created, False

    # The unique prompt or idempotency constraint may have won concurrently.
    concurrent = (
        await session.execute(
            select(CustomerResolutionFeedbackEvent).where(
                CustomerResolutionFeedbackEvent.tenant_id == tenant_id,
                CustomerResolutionFeedbackEvent.idempotency_key_hash == key_hash,
            )
        )
    ).scalar_one_or_none()
    if concurrent is not None and concurrent.request_hash == payload_hash:
        return concurrent, True
    if event_type == "requested":
        concurrent_request = (
            await session.execute(
                select(CustomerResolutionFeedbackEvent).where(
                    CustomerResolutionFeedbackEvent.tenant_id == tenant_id,
                    CustomerResolutionFeedbackEvent.conversation_ref_id == conversation_ref_id,
                    CustomerResolutionFeedbackEvent.event_type == "requested",
                )
            )
        ).scalar_one_or_none()
        if concurrent_request is not None:
            return concurrent_request, True
    raise CustomerFeedbackError("IDEMPOTENCY_CONFLICT")


async def feedback_state(
    session: AsyncSession, *, tenant_id: uuid.UUID, conversation_ref_id: uuid.UUID
) -> dict[str, object]:
    rows = (
        await session.execute(
            select(
                CustomerResolutionFeedbackEvent.event_type,
                CustomerResolutionFeedbackEvent.occurred_at,
            )
            .where(
                CustomerResolutionFeedbackEvent.tenant_id == tenant_id,
                CustomerResolutionFeedbackEvent.conversation_ref_id == conversation_ref_id,
            )
            .order_by(
                CustomerResolutionFeedbackEvent.occurred_at, CustomerResolutionFeedbackEvent.id
            )
        )
    ).all()
    request_at = next((int(row.occurred_at) for row in rows if row.event_type == "requested"), None)
    answers = [
        row
        for row in rows
        if row.event_type in {"confirmed", "rejected"}
        and request_at is not None
        and int(row.occurred_at) >= request_at
    ]
    latest_answer = answers[-1] if answers else None
    return {
        "requested": request_at is not None,
        "confirmed": (
            latest_answer.event_type == "confirmed" if latest_answer is not None else None
        ),
    }


async def customer_outcome_summary(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    window_seconds: int,
    confirmation_window_seconds: int,
    now: int | None = None,
) -> dict[str, object]:
    """Aggregate exposed prompts, explicit answers, and mature silence only."""
    current_time = int(time.time()) if now is None else now
    cutoff = current_time - window_seconds
    # These are application-interface reads at the evaluation boundary. The
    # same-case relationship and contact mapping are both tenant-scoped; text
    # similarity is never used to guess that two conversations are the same issue.
    from platform_core.agent_runtime.models import ConversationTurn
    from platform_core.cases.models import Case, CaseConversation
    from platform_core.support_bridge.continuity_models import ConversationContact

    request_event = CustomerResolutionFeedbackEvent
    origin_contact = aliased(ConversationContact)
    followup_contact = aliased(ConversationContact)
    followup_link = aliased(CaseConversation)
    followup_turn = aliased(ConversationTurn)
    resolution_at_expression = func.coalesce(Case.resolved_at, request_event.occurred_at)
    contact_link_available = exists(
        select(1).where(
            origin_contact.tenant_id == tenant_id,
            origin_contact.conversation_ref_id == request_event.conversation_ref_id,
            origin_contact.external_contact_id != "",
        )
    )
    same_case_recontact_at = (
        select(func.min(followup_turn.ts))
        .select_from(origin_contact)
        .join(
            followup_link,
            and_(
                followup_link.tenant_id == tenant_id,
                followup_link.case_id == request_event.case_id,
                followup_link.conversation_ref_id != request_event.conversation_ref_id,
            ),
        )
        .join(
            followup_contact,
            and_(
                followup_contact.tenant_id == tenant_id,
                followup_contact.conversation_ref_id == followup_link.conversation_ref_id,
                followup_contact.external_contact_id == origin_contact.external_contact_id,
                followup_contact.external_contact_id != "",
            ),
        )
        .join(
            followup_turn,
            and_(
                followup_turn.tenant_id == tenant_id,
                followup_turn.conversation_ref_id == followup_link.conversation_ref_id,
                followup_turn.role == "customer",
            ),
        )
        .where(
            origin_contact.tenant_id == tenant_id,
            origin_contact.conversation_ref_id == request_event.conversation_ref_id,
            origin_contact.external_contact_id != "",
            followup_turn.ts >= resolution_at_expression,
            followup_turn.ts <= resolution_at_expression + confirmation_window_seconds,
            followup_turn.ts <= current_time,
        )
        .correlate(request_event, Case)
        .scalar_subquery()
    )
    requests_result = await session.execute(
        select(
            request_event,
            Case.id,
            Case.resolved_at,
            contact_link_available,
            same_case_recontact_at,
        )
        .outerjoin(
            Case,
            and_(Case.id == request_event.case_id, Case.tenant_id == tenant_id),
        )
        .where(
            request_event.tenant_id == tenant_id,
            request_event.event_type == "requested",
            request_event.occurred_at >= cutoff,
        )
        .order_by(
            request_event.occurred_at,
            request_event.id,
        )
        .limit(MAX_OUTCOME_OBSERVATIONS + 1)
    )
    request_rows = requests_result.all()
    if len(request_rows) > MAX_OUTCOME_OBSERVATIONS:
        raise CustomerFeedbackError("OUTCOME_WINDOW_TOO_LARGE")
    requests = [row[0] for row in request_rows]
    link_data = {
        request.id: {
            "linked_case": row[1] is not None,
            "resolved_at": int(row[2]) if row[2] is not None else None,
            "contact_link_available": bool(row[3]),
            "recontact_at": int(row[4]) if row[4] is not None else None,
        }
        for row in request_rows
        for request in (row[0],)
    }
    conversation_ids = [row.conversation_ref_id for row in requests]
    answers_by_conversation: dict[uuid.UUID, list[CustomerResolutionFeedbackEvent]] = {}
    if conversation_ids:
        answers_result = await session.execute(
            select(CustomerResolutionFeedbackEvent)
            .where(
                CustomerResolutionFeedbackEvent.tenant_id == tenant_id,
                CustomerResolutionFeedbackEvent.conversation_ref_id.in_(conversation_ids),
                CustomerResolutionFeedbackEvent.event_type.in_(("confirmed", "rejected")),
                CustomerResolutionFeedbackEvent.occurred_at >= cutoff,
            )
            .order_by(
                CustomerResolutionFeedbackEvent.occurred_at,
                CustomerResolutionFeedbackEvent.id,
            )
        )
        answer_rows = answers_result.scalars().all()
        for row in answer_rows:
            answers_by_conversation.setdefault(row.conversation_ref_id, []).append(row)

    observations: list[CustomerOutcomeObservation] = []
    for request in requests:
        answer_rows = [
            row
            for row in answers_by_conversation.get(request.conversation_ref_id, [])
            if row.occurred_at >= request.occurred_at
        ]
        if answer_rows:
            latest = answer_rows[-1]
            status = "confirmed" if latest.event_type == "confirmed" else "rejected"
        elif current_time - request.occurred_at >= confirmation_window_seconds:
            status = "no_response"
        else:
            status = "pending"
        link = link_data[request.id]
        resolved_at = link["resolved_at"]
        if resolved_at is None or resolved_at > current_time:
            resolved_at = int(request.occurred_at)
        link_available = bool(link["linked_case"] and link["contact_link_available"])
        recontact_at = link["recontact_at"] if link_available else None
        observations.append(
            CustomerOutcomeObservation(
                observation_id=str(request.id),
                resolution_at=resolved_at,
                observed_through=current_time,
                confirmation_status=status,  # type: ignore[arg-type]
                # The support UI asks after the interaction ends but does not
                # auto-resolve a Case from silence; keep those facts separate.
                platform_marked_resolved=False,
                same_issue_recontact_at=recontact_at,
                recontact_link_verified=recontact_at is not None,
                recontact_link_available=link_available,
            )
        )

    summary = summarize_customer_outcomes(
        observations, recontact_window_seconds=confirmation_window_seconds
    )
    return {
        **summary,
        "source": "customer_resolution_feedback_events",
        "confirmation_window_seconds": confirmation_window_seconds,
        "recontact_linkage_status": (
            "measured_for_verified_same_contact_same_case_links"
            if summary["recontact_link_available_count"]
            else "not_measured_without_verified_same_case_contact_links"
        ),
    }


__all__ = [
    "CustomerFeedbackError",
    "customer_outcome_summary",
    "feedback_state",
    "record_feedback_event",
]
