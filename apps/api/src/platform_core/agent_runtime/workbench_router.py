"""Conversation-first operator inbox.

The lease is the authority for what needs a human. A handoff may have no Case,
so a Case list is not a valid inbox. Case, account and channel facts are read
through their owning modules' application interfaces.
"""

from __future__ import annotations

import uuid
from typing import Any, Literal

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import text

from observability_metrics import get_metrics
from platform_core.agent_runtime import chat_service, conversation_store
from platform_core.agent_runtime.emotion import Emotion
from platform_core.agent_runtime.emotion_advice import (
    FLAG_EMOTION_PRIORITY_ADVICE,
    EmotionPriorityAdvice,
    EmotionTurn,
    recommend_emotion_priority,
)
from platform_core.agent_runtime.emotion_review_service import (
    EmotionReviewError,
    record_correction,
    replay_correction,
)
from platform_core.agent_runtime.semantic.contracts import SemanticTaskKind
from platform_core.agent_runtime.tasks.capability import tenant_capabilities
from platform_core.agent_runtime.tasks.standard_flows import (
    STANDARD_FLOW_TEMPLATES,
    resolve_flow_availability,
)
from platform_core.api import (
    AUTH_UNRESOLVED,
    CASE_NOT_FOUND,
    VALIDATION_FAILED,
    error_response,
    get_context,
    new_trace_id,
    ok_response,
    require_idempotency_key,
    require_policy,
    require_write_idempotency,
    tenant_session,
)
from platform_core.audit import service as audit_service
from platform_core.cases.assignment import list_agents
from platform_core.cases.service import (
    search_case_conversation_refs,
    set_workbench_case_owner,
    workbench_cases_for_conversations,
)
from platform_core.identity import lease_service, org
from platform_core.identity.control_lease import LeaseConflict
from platform_core.identity.profile import account_profile
from platform_core.integrations.readiness import active_connector_capabilities
from platform_core.knowledge import flag_service
from platform_core.outbox_service import enqueue
from platform_core.support_bridge.continuity import conversation_channels
from platform_policy import Action, PolicyEngine

router = APIRouter(prefix="/v1/workbench", tags=["workbench"])


class CaseInfo(BaseModel):
    case_id: str
    subject: str
    status: str
    priority: str
    category: str
    assignee_ref: str | None
    team_ref: str | None
    enterprise_account_id: str | None
    version: int
    first_response_due_at: int | None
    first_responded_at: int | None
    resolution_due_at: int | None
    opened_at: int


class LeaseInfo(BaseModel):
    owner: str
    owner_ref: str | None
    mode: str
    version: int
    updated_at: int


class QueueItem(BaseModel):
    conversation_ref: str
    title: str
    preview: str
    last_at: int | None
    channel: str | None
    contact_ref: str | None
    case: CaseInfo | None
    lease: LeaseInfo
    emotion_advice: EmotionPriorityAdvice | None = None


class QueueResponse(BaseModel):
    items: list[QueueItem]
    counts: dict[str, int]
    total: int
    limit: int
    offset: int
    actor_ref: str | None
    agent_name: str | None
    agent_status: str | None
    sort_mode: Literal["activity", "emotion"] = "activity"
    sort_scope: Literal["queue", "current_page"] = "queue"
    emotion_advice_enabled: bool = False


class AccountInfo(BaseModel):
    name: str
    tier: str
    contract_status: str
    attributes: dict[str, str]
    missing: list[str]
    contacts: list[dict[str, str | None]]


class DetailResponse(BaseModel):
    conversation_ref: str
    timeline_revision: int
    lease: LeaseInfo
    case: CaseInfo | None
    channel: str | None
    contact_ref: str | None
    account: AccountInfo | None
    turns: list[dict[str, Any]]
    older_before: str | None
    ai_suggestion: dict[str, Any] | None
    emotion_advice: EmotionPriorityAdvice | None = None
    can_review_emotion_advice: bool = False


class ActionIn(BaseModel):
    operation: Literal["claim", "release", "transfer", "close"]
    expected_version: int = Field(ge=1)
    target_ref: str | None = Field(default=None, max_length=255)


class EmotionAdviceReviewIn(BaseModel):
    advice_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    corrected_level: Emotion
    reason_code: Literal[
        "overstated",
        "understated",
        "quoted_or_negated",
        "sarcasm_or_mixed_tone",
        "context_missing",
        "other",
    ]


def _lease_out(row: lease_service.LeaseSnapshot) -> LeaseInfo:
    return LeaseInfo(
        owner=row.owner_type,
        owner_ref=row.owner_ref,
        mode=row.mode,
        version=row.lease_version,
        updated_at=row.updated_at,
    )


def _auth(request: Request, action: Action) -> tuple[Any | None, Any | None]:
    ctx = get_context(request)
    if ctx is None:
        return None, error_response(AUTH_UNRESOLVED, "tenant context not resolved", status_code=401)
    return ctx, require_policy(ctx, action)


@router.get("/standard-flows")
async def list_standard_flows(request: Request) -> Any:
    """List operator flow templates with current tenant/role readiness.

    This endpoint is a read-only guide. It does not create a task, call a
    connector, or promise that an external action will succeed. The Tool
    Gateway re-checks every tool at execution time.
    """
    ctx, denied = _auth(request, Action.CASE_READ)
    if denied is not None:
        return denied
    assert ctx is not None
    actor_role = ctx.role or "support_viewer"
    principal_id = str(ctx.actor_id) if ctx.actor_id else "unidentified"
    policy = PolicyEngine()
    async with tenant_session(ctx) as session:
        read_filter = await tenant_capabilities(
            session,
            tenant_id=ctx.tenant_id,
            kind=SemanticTaskKind.READ,
            actor_role=actor_role,
            principal_id=principal_id,
            policy=policy,
        )
        write_filter = await tenant_capabilities(
            session,
            tenant_id=ctx.tenant_id,
            kind=SemanticTaskKind.WRITE,
            actor_role=actor_role,
            principal_id=principal_id,
            policy=policy,
            allow_semantic_write=True,
        )
        capabilities = {**read_filter.available, **write_filter.available}
        connector_caps = await active_connector_capabilities(session, tenant_id=ctx.tenant_id)
        owner_groups = await org.routable_support_department_slugs(session, tenant_id=ctx.tenant_id)
        items = [
            {
                "template": template.model_dump(mode="json"),
                "availability": resolve_flow_availability(
                    template,
                    capabilities=capabilities,
                    active_connector_capabilities=connector_caps,
                    configured_owner_groups=owner_groups,
                ).model_dump(mode="json"),
            }
            for template in STANDARD_FLOW_TEMPLATES
        ]
    return ok_response({"items": items, "execution_requires_tool_gateway": True})


@router.get("/conversations")
async def list_workbench_conversations(
    request: Request,
    tab: Literal["queue", "mine", "waiting"] = "queue",
    sort: Literal["activity", "emotion"] = "activity",
    q: str = Query(default="", max_length=80),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> Any:
    ctx, denied = _auth(request, Action.CASE_READ)
    if denied is not None:
        return denied
    assert ctx is not None
    actor_ref = str(ctx.actor_id) if ctx.actor_id else ""
    async with tenant_session(ctx) as session:
        emotion_flag = await flag_service.evaluate(
            session,
            flag_key=FLAG_EMOTION_PRIORITY_ADVICE,
            tenant_id=ctx.tenant_id,
            default=False,
        )
        if sort == "emotion" and not emotion_flag.enabled:
            return error_response(
                "FEATURE_DISABLED",
                "emotion priority advice is not enabled for this tenant",
                status_code=409,
            )
        matches: set[uuid.UUID] | None = None
        if q.strip():
            term = q.strip()
            matches = await chat_service.search_conversation_refs(
                session, tenant_id=ctx.tenant_id, term=term
            )
            matches |= await search_case_conversation_refs(
                session, tenant_id=ctx.tenant_id, term=term
            )
            try:
                matches.add(uuid.UUID(term))
            except ValueError:
                pass
        leases, counts = await lease_service.workbench_leases(
            session,
            tenant_id=ctx.tenant_id,
            actor_ref=actor_ref,
            tab=tab,
            limit=limit,
            offset=offset,
            matching_refs=matches,
        )
        refs = [row.conversation_ref_id for row in leases]
        previews = await chat_service.latest_previews(session, tenant_id=ctx.tenant_id, refs=refs)
        cases = await workbench_cases_for_conversations(
            session, tenant_id=ctx.tenant_id, conversation_refs=refs
        )
        channels = await conversation_channels(session, tenant_id=ctx.tenant_id, refs=refs)
        agents = await list_agents(session, tenant_id=ctx.tenant_id)
        emotion_advice_by_ref: dict[uuid.UUID, EmotionPriorityAdvice] = {}
        if emotion_flag.enabled and refs:
            recent = await chat_service.recent_customer_turns_by_ref(
                session,
                tenant_id=ctx.tenant_id,
                refs=refs,
                limit_per_conversation=5,
            )
            revisions = await chat_service.timeline_revisions_by_ref(
                session, tenant_id=ctx.tenant_id, refs=refs
            )
            for ref in refs:
                emotion_advice_by_ref[ref] = recommend_emotion_priority(
                    tenant_id=ctx.tenant_id,
                    conversation_ref_id=ref,
                    timeline_revision=revisions.get(ref, 0),
                    turns=[
                        EmotionTurn(
                            turn_id=str(turn["turn_id"]),
                            role="customer",
                            text=str(turn["text"]),
                        )
                        for turn in recent.get(ref, [])
                    ],
                )
            if emotion_advice_by_ref:
                get_metrics().workbench_emotion_advice_total.labels(
                    action="generated", outcome="queue"
                ).inc(len(emotion_advice_by_ref))
    me = next((agent for agent in agents if agent.user_ref == actor_ref), None)
    items: list[QueueItem] = []
    for row in leases:
        ref = row.conversation_ref_id
        preview = previews.get(ref, {})
        case = cases.get(ref)
        contact = channels.get(ref, {})
        summary = str(preview.get("text") or "")
        items.append(
            QueueItem(
                conversation_ref=str(ref),
                title=str(case["subject"])
                if case
                else (str(preview.get("customer_text") or summary)[:50] or "待接待会话"),
                preview=summary,
                last_at=int(preview["at"]) if preview.get("at") else None,
                channel=contact.get("channel") or ("web" if not contact else None),
                contact_ref=contact.get("contact_ref"),
                case=CaseInfo(**case) if case else None,
                lease=_lease_out(row),
                emotion_advice=emotion_advice_by_ref.get(ref),
            )
        )
    if sort == "emotion":
        attention_rank = {"urgent_review": 3, "review": 2, "monitor": 1, "none": 0}
        items.sort(
            key=lambda item: (
                -attention_rank[item.emotion_advice.attention] if item.emotion_advice else 0,
                -(item.last_at or 0),
                item.conversation_ref,
            )
        )
    payload = QueueResponse(
        items=items,
        counts=counts,
        total=counts[tab],
        limit=limit,
        offset=offset,
        actor_ref=actor_ref or None,
        agent_name=me.display_name if me else None,
        agent_status=me.status if me else None,
        sort_mode=sort,
        sort_scope="current_page" if sort == "emotion" else "queue",
        emotion_advice_enabled=emotion_flag.enabled,
    )
    return ok_response(payload.model_dump())


@router.get("/conversations/{conversation_ref}")
async def workbench_conversation(request: Request, conversation_ref: uuid.UUID) -> Any:
    ctx, denied = _auth(request, Action.CASE_READ)
    if denied is not None:
        return denied
    assert ctx is not None
    async with tenant_session(ctx) as session:
        lease = await lease_service.lease_snapshot(
            session, tenant_id=ctx.tenant_id, conversation_ref_id=conversation_ref
        )
        if lease is None:
            return error_response(CASE_NOT_FOUND, "conversation not found", status_code=404)
        turns, older_before = await chat_service.read_timeline_page(
            session, ref_id=conversation_ref, limit=80, include_source_refs=True
        )
        timeline_revision = await chat_service.timeline_revision(session, ref_id=conversation_ref)
        emotion_flag = await flag_service.evaluate(
            session,
            flag_key=FLAG_EMOTION_PRIORITY_ADVICE,
            tenant_id=ctx.tenant_id,
            default=False,
        )
        emotion_advice = (
            recommend_emotion_priority(
                tenant_id=ctx.tenant_id,
                conversation_ref_id=conversation_ref,
                timeline_revision=timeline_revision,
                turns=[
                    EmotionTurn(
                        turn_id=str(turn["turn_id"]),
                        role=str(turn["role"]),
                        text=str(turn["text"]),
                    )
                    for turn in turns
                ],
            )
            if emotion_flag.enabled
            else None
        )
        if emotion_advice is not None:
            get_metrics().workbench_emotion_advice_total.labels(
                action="generated", outcome="detail"
            ).inc()
        cases = await workbench_cases_for_conversations(
            session, tenant_id=ctx.tenant_id, conversation_refs=[conversation_ref]
        )
        case = cases.get(conversation_ref)
        channels = await conversation_channels(
            session, tenant_id=ctx.tenant_id, refs=[conversation_ref]
        )
        channel = channels.get(conversation_ref, {})
        account: AccountInfo | None = None
        if case and case.get("enterprise_account_id"):
            account_id = uuid.UUID(str(case["enterprise_account_id"]))
            profile = await account_profile(session, tenant_id=ctx.tenant_id, account_id=account_id)
            if profile is not None:
                contacts = await org.list_contacts(
                    session, tenant_id=ctx.tenant_id, account_id=account_id
                )
                account = AccountInfo(
                    name=profile.name,
                    tier=profile.tier,
                    contract_status=profile.contract_status,
                    attributes=profile.attributes,
                    missing=list(profile.missing),
                    contacts=[
                        {"external_contact_id": c.external_contact_id, "channel": c.channel}
                        for c in contacts
                    ],
                )
        suggestion = await conversation_store.latest_suggestion(
            session, tenant_id=ctx.tenant_id, conversation_ref_id=conversation_ref
        )
    payload = DetailResponse(
        conversation_ref=str(conversation_ref),
        timeline_revision=timeline_revision,
        lease=_lease_out(lease),
        case=CaseInfo(**case) if case else None,
        channel=channel.get("channel") or ("web" if not channel else None),
        contact_ref=channel.get("contact_ref"),
        account=account,
        turns=turns,
        older_before=older_before,
        ai_suggestion={"text": suggestion[0], "sources": suggestion[1]} if suggestion else None,
        emotion_advice=emotion_advice,
        can_review_emotion_advice=bool(
            emotion_advice is not None and ctx.role in {"support_admin", "tenant_owner"}
        ),
    )
    return ok_response(payload.model_dump())


@router.post("/conversations/{conversation_ref}/emotion-advice/reviews")
async def review_emotion_advice(
    request: Request,
    conversation_ref: uuid.UUID,
    body: EmotionAdviceReviewIn,
) -> Any:
    """Record a supervisor correction; never changes Case priority or routing."""
    ctx, denied = _auth(request, Action.CASE_REVIEW)
    if denied is not None:
        return denied
    assert ctx is not None
    if ctx.role not in {"support_admin", "tenant_owner"}:
        return error_response(
            "EMOTION_REVIEW_DENIED",
            "only a support supervisor may correct emotion advice",
            status_code=403,
        )
    missing = require_write_idempotency(request, Action.CASE_REVIEW)
    if missing is not None:
        return missing
    idempotency_key = require_idempotency_key(request)
    if idempotency_key is None:
        return error_response("IDEMPOTENCY_KEY_REQUIRED", status_code=400)
    trace_id = new_trace_id()

    async with tenant_session(ctx) as session:
        emotion_flag = await flag_service.evaluate(
            session,
            flag_key=FLAG_EMOTION_PRIORITY_ADVICE,
            tenant_id=ctx.tenant_id,
            default=False,
        )
        if not emotion_flag.enabled:
            return error_response(
                "FEATURE_DISABLED",
                "emotion priority advice is not enabled for this tenant",
                status_code=409,
                trace_id=trace_id,
            )
        lease = await lease_service.lease_snapshot(
            session, tenant_id=ctx.tenant_id, conversation_ref_id=conversation_ref
        )
        if lease is None:
            return error_response(
                CASE_NOT_FOUND, "conversation not found", status_code=404, trace_id=trace_id
            )
        try:
            replay = await replay_correction(
                session,
                ctx=ctx,
                conversation_ref_id=conversation_ref,
                advice_id=body.advice_id,
                corrected_level=body.corrected_level,
                reason_code=body.reason_code,
                idempotency_key=idempotency_key,
            )
        except EmotionReviewError as exc:
            return error_response(exc.code, exc.detail, status_code=409, trace_id=trace_id)
        if replay is not None:
            get_metrics().workbench_emotion_advice_total.labels(
                action="review", outcome="replayed"
            ).inc()
            return ok_response(
                {
                    "review_id": str(replay.id),
                    "advice_id": replay.advice_id,
                    "corrected_level": replay.corrected_level,
                    "reason_code": replay.reason_code,
                    "replayed": True,
                },
                trace_id=trace_id,
            )
        turns, _older = await chat_service.read_timeline_page(
            session,
            ref_id=conversation_ref,
            limit=80,
            include_source_refs=False,
        )
        revision = await chat_service.timeline_revision(session, ref_id=conversation_ref)
        current = recommend_emotion_priority(
            tenant_id=ctx.tenant_id,
            conversation_ref_id=conversation_ref,
            timeline_revision=revision,
            turns=[
                EmotionTurn(
                    turn_id=str(turn["turn_id"]),
                    role=str(turn["role"]),
                    text=str(turn["text"]),
                )
                for turn in turns
            ],
        )
        if current.advice_id != body.advice_id:
            get_metrics().workbench_emotion_advice_total.labels(
                action="review", outcome="stale"
            ).inc()
            return error_response(
                "EMOTION_ADVICE_STALE",
                "conversation changed; refresh advice before correcting it",
                status_code=409,
                trace_id=trace_id,
            )
        try:
            review, replayed = await record_correction(
                session,
                ctx=ctx,
                conversation_ref_id=conversation_ref,
                advice_id=current.advice_id,
                suggested_level=current.current_level,
                corrected_level=body.corrected_level,
                reason_code=body.reason_code,
                idempotency_key=idempotency_key,
                trace_id=trace_id,
            )
        except EmotionReviewError as exc:
            code = exc.code
            conflict_codes = {"IDEMPOTENCY_CONFLICT", "EMOTION_ADVICE_ALREADY_REVIEWED"}
            status = 409 if code in conflict_codes else 400
            return error_response(code, exc.detail, status_code=status, trace_id=trace_id)
    get_metrics().workbench_emotion_advice_total.labels(
        action="review", outcome="replayed" if replayed else "created"
    ).inc()
    return ok_response(
        {
            "review_id": str(review.id),
            "advice_id": review.advice_id,
            "corrected_level": review.corrected_level,
            "reason_code": review.reason_code,
            "replayed": replayed,
        },
        trace_id=trace_id,
    )


@router.get("/conversations/{conversation_ref}/timeline")
async def older_workbench_turns(
    request: Request,
    conversation_ref: uuid.UUID,
    before: uuid.UUID,
    limit: int = Query(default=80, ge=1, le=100),
) -> Any:
    ctx, denied = _auth(request, Action.CASE_READ)
    if denied is not None:
        return denied
    assert ctx is not None
    async with tenant_session(ctx) as session:
        lease = await lease_service.lease_snapshot(
            session, tenant_id=ctx.tenant_id, conversation_ref_id=conversation_ref
        )
        if lease is None:
            return error_response(CASE_NOT_FOUND, "conversation not found", status_code=404)
        turns, older_before = await chat_service.read_timeline_page(
            session,
            ref_id=conversation_ref,
            limit=limit,
            before_id=before,
            include_source_refs=True,
        )
    return ok_response({"items": turns, "older_before": older_before})


@router.post("/conversations/{conversation_ref}/actions")
async def change_workbench_owner(
    request: Request, conversation_ref: uuid.UUID, body: ActionIn
) -> Any:
    ctx, denied = _auth(request, Action.CASE_UPDATE)
    if denied is not None:
        return denied
    assert ctx is not None
    missing = require_write_idempotency(request, Action.CASE_UPDATE)
    if missing is not None:
        return missing
    if ctx.actor_id is None:
        return error_response(VALIDATION_FAILED, "an identified agent is required", status_code=400)
    actor_ref = str(ctx.actor_id)
    trace_id = new_trace_id()
    try:
        async with tenant_session(ctx) as session:
            if body.operation in ("claim", "transfer"):
                agents = await list_agents(session, tenant_id=ctx.tenant_id)
                recipient = actor_ref if body.operation == "claim" else body.target_ref
                target_agent = next((a for a in agents if a.user_ref == recipient), None)
                if target_agent is None:
                    return error_response(
                        "AGENT_UNAVAILABLE",
                        "the agent is not active in this tenant",
                        status_code=409,
                    )
                await session.execute(
                    text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                    {"key": f"workbench-capacity:{ctx.tenant_id}:{target_agent.user_ref}"},
                )
                workload = await lease_service.active_human_workload(
                    session, tenant_id=ctx.tenant_id, actor_ref=target_agent.user_ref
                )
                if workload >= int(target_agent.max_concurrent):
                    return error_response(
                        "AGENT_AT_CAPACITY",
                        "the agent is at conversation capacity",
                        status_code=409,
                    )
            lease = await lease_service.workbench_transition(
                session,
                tenant_id=ctx.tenant_id,
                conversation_ref_id=conversation_ref,
                actor_ref=actor_ref,
                expected_version=body.expected_version,
                operation=body.operation,
                target_ref=body.target_ref,
            )
            if body.operation != "close":
                owner = lease.owner_ref if lease.owner_type == "human" else None
                changed = await set_workbench_case_owner(
                    session,
                    tenant_id=ctx.tenant_id,
                    conversation_ref_id=conversation_ref,
                    owner_ref=owner,
                )
                for case_id, version in changed:
                    await enqueue(
                        session,
                        tenant_id=ctx.tenant_id,
                        event_type="case.updated",
                        aggregate_type="case",
                        aggregate_id=str(case_id),
                        payload={"case_id": str(case_id), "command": "assign", "version": version},
                        trace_id=trace_id,
                    )
            await audit_service.record(
                session,
                ctx=ctx,
                action=f"conversation.workbench_{body.operation}",
                resource_type="conversation",
                resource_id=conversation_ref,
                metadata={
                    "target_ref": body.target_ref or "",
                    "lease_version": lease.lease_version,
                },
                trace_id=trace_id,
            )
    except LeaseConflict as exc:
        return error_response("LEASE_CONFLICT", str(exc), status_code=409, trace_id=trace_id)
    return ok_response({"lease": _lease_out(lease).model_dump()}, trace_id=trace_id)


__all__ = ["router"]
