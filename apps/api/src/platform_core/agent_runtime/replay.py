"""Conversation replay (feature list 8.3).

An operator reading a conversation needs three things at once: what was said,
what the system decided, and why. The case workbench already shows the first
one; this module assembles all three per *conversation*, which is the unit a
replay is about.

How a decision is tied to an utterance - not by timestamp. A run's
`input_hash` is `sha256(question)` and a turn's `text_hash` is
`sha256(raw turn text)`, both taken *before* redaction, so the customer turn
that triggered a run matches it exactly. Joining on `ts` would be a guess that
degrades exactly under load, which is the condition someone opens a replay to
explain.

The reply side is deliberately not attributed the same way. `output_hash` is
`sha256(draft)[:16]` - truncated, and the draft may differ from what was
actually sent - so no reply is claimed to belong to a run. The run is attached
to the utterance that triggered it and the answer appears in order, with the
strength of the link stated in the payload (`matched_by`). A replay that
asserted "this reply came from that run" on a truncated hash would be wrong
precisely when the two disagreed, which is the case worth replaying.

What the text is: `conversation_turns` has never stored raw text - the column
is `text_redacted`, and both write paths hash the raw text before discarding
it. A replay therefore shows the customer's own wording with PII-shaped tokens
masked (`[EMAIL]`/`[CARD]`/`[PHONE]`): the sentence as written, minus three
value classes. Showing more would need raw text at rest, which is a storage
policy change rather than a UI one.

Runs that never executed - an empty `input_hash`, whether still `queued` or
already closed as `abandoned` - hold no utterance and no outcome, so there is
nothing to replay. They are excluded from the listing but counted and returned
as `nothing_to_replay`, because a view that silently drops rows reads as
"this is all there is".
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import func, select, union_all
from sqlalchemy.ext.asyncio import AsyncSession

from platform_core.agent_runtime.models import AgentRun, Citation, ConversationTurn
from platform_core.agent_runtime.tool_card import build_card
from platform_core.cases.models import Case, CaseConversation

# Chronological, so a long conversation is cut at the front rather than losing
# its most recent exchange.
DEFAULT_TURN_LIMIT = 200
DEFAULT_RUN_LIMIT = 50
MAX_LIST_LIMIT = 100

# What "the system decided", as few fields as an operator can hold in view.
_DECISION_KEYS = (
    "scene",
    "business_line",
    "primary_kind",
    "secondary_kinds",
    "confidence",
    "action",
    "spelling_corrections",
    "multi_intent",
)


def _decision(run: AgentRun) -> dict[str, Any]:
    """The run's routing decision, narrowed to the explainable fields.

    `model_config` also carries conversation budgeting and sampling settings;
    forwarding it wholesale would make the replay a JSON viewer. The keys kept
    are the ones a "why did it answer that" question is actually answered by.
    """
    model_config = run.model_config or {}
    intent = model_config.get("intent")
    intent = intent if isinstance(intent, dict) else {}
    return {
        "run_id": str(run.id),
        "route": run.route,
        "status": run.status,
        "abstain_reason": run.abstain_reason,
        "latency_ms": run.latency_ms,
        "trace_id": run.trace_id or "",
        "case_id": str(run.case_id) if run.case_id else None,
        "model": model_config.get("model"),
        "intent": {key: intent.get(key) for key in _DECISION_KEYS if key in intent},
        # Stated rather than implied: this link is a hash equality on the
        # untruncated text, not an ordering guess.
        "matched_by": "input_hash",
    }


def _run_row(run: AgentRun, sources: list[dict[str, Any]]) -> dict[str, Any]:
    row = _decision(run)
    row["started_at"] = run.started_at
    row["sources"] = sources
    row["token_usage"] = run.token_usage or {}
    return row


async def list_conversations(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    limit: int,
    offset: int,
) -> dict[str, Any]:
    """Recent conversations, most recently active first.

    A conversation is anything that was *said* or *decided on* - the union of
    the two, not either one. Neither table alone is complete: turns exist for
    exchanges where a run was refused, and runs exist for exchanges whose turns
    were written under a different conversation id. Taking one side would hide
    real conversations from the only screen that can show them.
    """
    turns = (
        select(
            ConversationTurn.conversation_ref_id.label("ref"),
            func.max(ConversationTurn.ts).label("last_ts"),
            func.count().label("turn_count"),
        )
        .where(ConversationTurn.tenant_id == tenant_id)
        .group_by(ConversationTurn.conversation_ref_id)
        .subquery()
    )
    # Only runs that ran: a queued placeholder has an empty input_hash and no
    # utterance, so it is not a conversation anybody can open.
    runs = (
        select(
            AgentRun.conversation_ref_id.label("ref"),
            func.max(func.coalesce(AgentRun.started_at, 0)).label("last_ts"),
        )
        .where(AgentRun.tenant_id == tenant_id, AgentRun.input_hash != "")
        .group_by(AgentRun.conversation_ref_id)
        .subquery()
    )

    merged = union_all(
        select(turns.c.ref, turns.c.last_ts, turns.c.turn_count),
        select(runs.c.ref, runs.c.last_ts, func.cast(0, runs.c.last_ts.type)),
    ).subquery()

    rows = (
        await session.execute(
            select(
                merged.c.ref,
                func.max(merged.c.last_ts).label("last_at"),
                func.sum(merged.c.turn_count).label("turn_count"),
            )
            .group_by(merged.c.ref)
            # The ref breaks ties: several conversations can share a second,
            # and an unstable order makes paging skip rows.
            .order_by(func.max(merged.c.last_ts).desc(), merged.c.ref.desc())
            .limit(limit)
            .offset(offset)
        )
    ).all()

    refs = [row.ref for row in rows]
    latest = await _latest_runs(session, tenant_id=tenant_id, refs=refs)

    items = [
        {
            "conversation_ref_id": str(row.ref),
            "turn_count": int(row.turn_count or 0),
            "last_at": int(row.last_at or 0) or None,
            "latest_run": latest.get(row.ref),
        }
        for row in rows
    ]

    return {
        "items": items,
        "limit": limit,
        "offset": offset,
        "nothing_to_replay": await _nothing_to_replay(session, tenant_id=tenant_id),
    }


async def _latest_runs(
    session: AsyncSession, *, tenant_id: uuid.UUID, refs: list[uuid.UUID]
) -> dict[uuid.UUID, dict[str, Any]]:
    """The newest run per conversation, for the list column.

    Newest of *all* runs, placeholders included: "a run is queued right now" is
    the state an operator needs to see, and filtering to executed runs would
    report the previous answer as if it were the current one.
    """
    if not refs:
        return {}
    rows = (
        (
            await session.execute(
                select(AgentRun)
                .where(AgentRun.tenant_id == tenant_id, AgentRun.conversation_ref_id.in_(refs))
                .distinct(AgentRun.conversation_ref_id)
                .order_by(AgentRun.conversation_ref_id, AgentRun.id.desc())
            )
        )
        .scalars()
        .all()
    )
    return {
        run.conversation_ref_id: {
            "run_id": str(run.id),
            "route": run.route,
            "status": run.status,
            "abstain_reason": run.abstain_reason,
            "started_at": run.started_at,
        }
        for run in rows
    }


async def _nothing_to_replay(session: AsyncSession, *, tenant_id: uuid.UUID) -> int:
    """Conversations whose only trace is a run that never ran.

    Counted rather than ignored. These rows accumulate - one per queue call
    whose worker never advanced it - and an operator dashboard that shows only
    replayable conversations cannot tell anyone that they are there.

    Selected on `input_hash`, not on status, so it covers both states a
    never-executed row can be in: still `queued` (a worker may yet take it) and
    `abandoned` (the retention sweep closed it). Keying on `queued` would make
    this number fall to zero as the sweep ran, i.e. the disclosure would
    disappear exactly when the pile got tidied up.
    """
    placeholders = select(AgentRun.conversation_ref_id).where(
        AgentRun.tenant_id == tenant_id, AgentRun.input_hash == ""
    )
    known = (
        select(ConversationTurn.conversation_ref_id)
        .where(ConversationTurn.tenant_id == tenant_id)
        .union(
            select(AgentRun.conversation_ref_id).where(
                AgentRun.tenant_id == tenant_id, AgentRun.input_hash != ""
            )
        )
    )
    count = (
        await session.execute(
            select(func.count()).select_from(placeholders.except_(known).subquery())
        )
    ).scalar_one()
    return int(count or 0)


async def build_replay(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    conversation_ref_id: uuid.UUID,
    turn_limit: int = DEFAULT_TURN_LIMIT,
    run_limit: int = DEFAULT_RUN_LIMIT,
) -> dict[str, Any] | None:
    """The full exchange plus every decision taken in it, or None if unknown.

    Returns None only when the ref has neither a turn nor a run: a conversation
    that is real to this tenant but has been pruned by retention is
    indistinguishable from one that never existed, and both are honestly "not
    here" rather than an empty exchange that looks like a bug in the UI.
    """
    turns = (
        (
            await session.execute(
                select(ConversationTurn)
                .where(
                    ConversationTurn.tenant_id == tenant_id,
                    ConversationTurn.conversation_ref_id == conversation_ref_id,
                )
                .order_by(ConversationTurn.ts.asc(), ConversationTurn.id.asc())
                .limit(turn_limit)
            )
        )
        .scalars()
        .all()
    )
    runs = (
        (
            await session.execute(
                select(AgentRun)
                .where(
                    AgentRun.tenant_id == tenant_id,
                    AgentRun.conversation_ref_id == conversation_ref_id,
                )
                # No created_at column; the UUIDv7 primary key is time-ordered.
                .order_by(AgentRun.id.asc())
                .limit(run_limit)
            )
        )
        .scalars()
        .all()
    )
    if not turns and not runs:
        return None

    sources = await _sources_by_run(session, tenant_id=tenant_id, run_ids=[run.id for run in runs])
    # Hash equality, and only for customer turns: an agent turn's hash is the
    # reply text, which no run's input_hash can equal.
    by_input_hash = {run.input_hash: run for run in runs if run.input_hash}

    turn_rows: list[dict[str, Any]] = []
    for turn in turns:
        match = by_input_hash.get(turn.text_hash) if turn.role == "customer" else None
        turn_rows.append(
            {
                "role": turn.role,
                "text": turn.text_redacted,
                "at": int(turn.ts or 0) or None,
                "source": turn.source or "",
                "card": build_card(turn.text_redacted) if turn.role == "tool" else None,
                "decision": _decision(match) if match is not None else None,
            }
        )

    return {
        "conversation_ref_id": str(conversation_ref_id),
        "turn_count": len(turn_rows),
        "run_count": len(runs),
        "first_at": turn_rows[0]["at"] if turn_rows else None,
        "last_at": turn_rows[-1]["at"] if turn_rows else None,
        "turns": turn_rows,
        "runs": [_run_row(run, sources.get(run.id, [])) for run in runs],
        "cases": await _cases(
            session, tenant_id=tenant_id, conversation_ref_id=conversation_ref_id, runs=runs
        ),
    }


async def _sources_by_run(
    session: AsyncSession, *, tenant_id: uuid.UUID, run_ids: list[uuid.UUID]
) -> dict[uuid.UUID, list[dict[str, Any]]]:
    """What each run cited, in claim order.

    This is the evidence half of "why did it say that". The excerpt itself is
    not stored here (only its hash) - the citation is a pointer to a document
    version, and the replay shows the pointer rather than inventing text.
    """
    if not run_ids:
        return {}
    rows = (
        (
            await session.execute(
                select(Citation)
                .where(Citation.tenant_id == tenant_id, Citation.agent_run_id.in_(run_ids))
                .order_by(Citation.agent_run_id, Citation.claim_index)
            )
        )
        .scalars()
        .all()
    )
    out: dict[uuid.UUID, list[dict[str, Any]]] = {}
    for row in rows:
        out.setdefault(row.agent_run_id, []).append(
            {
                "source_uri": row.source_uri,
                "excerpt_hash": row.excerpt_hash,
                "claim_index": row.claim_index,
                "retrieval_score": row.retrieval_score,
                "document_version_id": str(row.document_version_id)
                if row.document_version_id
                else None,
            }
        )
    return out


async def _cases(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    conversation_ref_id: uuid.UUID,
    runs: list[AgentRun],
) -> list[dict[str, Any]]:
    """Cases this conversation produced.

    Two links, unioned because either can be the only one present: the explicit
    `case_conversations` origin link, and `agent_runs.case_id` for the case a
    run escalated into. Neither is derivable from the other.
    """
    case_ids = {run.case_id for run in runs if run.case_id}
    linked = (
        (
            await session.execute(
                select(CaseConversation.case_id).where(
                    CaseConversation.tenant_id == tenant_id,
                    CaseConversation.conversation_ref_id == conversation_ref_id,
                )
            )
        )
        .scalars()
        .all()
    )
    case_ids.update(linked)
    if not case_ids:
        return []
    rows = (
        (
            await session.execute(
                select(Case)
                .where(Case.tenant_id == tenant_id, Case.id.in_(case_ids))
                # Deterministic: opened_at is second-granular, so ties happen.
                .order_by(Case.opened_at.asc(), Case.id.asc())
            )
        )
        .scalars()
        .all()
    )
    return [
        {
            "case_id": str(row.id),
            "subject": row.subject,
            "status": row.status,
            "category": row.category,
            "team_ref": row.team_ref,
            "version": row.version,
            "opened_at": row.opened_at,
        }
        for row in rows
    ]
