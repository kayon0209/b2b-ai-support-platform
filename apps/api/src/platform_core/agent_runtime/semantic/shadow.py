"""Shadow mode: compare without touching anything.

SHD-01 asks for proof that the same input produces the same customer messages,
the same owner, the same number of business tool calls and the same Case state
in `off` and in `shadow`. That proof is only meaningful if the shadow path is
*structurally* incapable of changing any of those, so this module's design
constraint is that it takes no write handle on business state at all:

- it reads the lease, the turns and the tool definitions, and it writes exactly
  one row in `semantic_assessments`;
- it never calls `tool_gateway`, never enqueues an outbox event, never touches
  a Case, a turn or a lease;
- it runs after the customer response has already been decided, from the
  inbox consumer, so it cannot delay or alter the answer.

The last point is the one that makes "shadow" honest rather than aspirational.
A shadow classification that ran inline on the request path would be a
latency cost on every message in order to produce a record nobody reads yet -
and a timeout there would be a customer-visible failure of a feature that is
supposed to change nothing.

Expiry is a first-class outcome. A sample older than `shadow_ttl_seconds` is
recorded as expired and skipped: an assessment of a conversation state that has
since moved on is worse than no assessment, because it looks comparable and is
not.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from platform_core.agent_runtime.semantic.contracts import (
    SemanticAssessment,
    SemanticMode,
)
from platform_core.agent_runtime.semantic.service import SemanticBudget
from platform_core.agent_runtime.semantic.validator import CapabilityView
from platform_core.agent_runtime.tasks import store as task_store

# How long a shadow sample stays comparable to its conversation. A week is
# longer than any evaluation window and short enough that a stale comparison
# cannot quietly become evidence months later.
SHADOW_TTL_SECONDS = 7 * 24 * 3600

# A cap per conversation, so one verbose customer cannot fill the table and
# crowd out every other tenant's samples.
MAX_SAMPLES_PER_CONVERSATION = 50

# The outbox event type the inbox consumer writes and `shadow_consumer` reads.
#
# Named here rather than at the producer so the two halves cannot drift: a
# producer that invented its own string would enqueue work nothing consumes,
# and the symptom - a silent no-op - is exactly what this module exists to
# make impossible to miss.
SHADOW_EVENT_TYPE = "semantic.shadow_requested"

REASON_SHADOW_EXPIRED = "SEMANTIC_SHADOW_SAMPLE_EXPIRED"
REASON_SHADOW_QUOTA = "SEMANTIC_SHADOW_CONVERSATION_QUOTA"


@dataclass(frozen=True)
class ShadowRequest:
    """What the worker passes in. All of it already authorized and redacted."""

    tenant_id: uuid.UUID
    conversation_ref_id: uuid.UUID
    turn_id: str
    turn_text: str
    history: list[tuple[str, str]]
    lease_owner_type: str
    capabilities: dict[str, CapabilityView]
    turn_created_at: int
    detection: Any | None = None


@dataclass(frozen=True)
class ShadowOutcome:
    """What the worker should log. Counts and codes, never content."""

    recorded: bool
    reason: str
    assessment: SemanticAssessment | None = None

    def as_log_fields(self) -> dict[str, Any]:
        return {
            "recorded": self.recorded,
            "reason": self.reason,
            "mode": SemanticMode.SHADOW.value,
        }


async def record_shadow(
    session: AsyncSession,
    request: ShadowRequest,
    provider: Any | None,
    *,
    budget: SemanticBudget | None = None,
) -> ShadowOutcome:
    """Run one shadow classification and persist the comparison record.

    Called from `worker.shadow_consumer` in its own transaction, so a failure
    here cannot roll back the customer-facing run that queued the work.

    `budget` is a parameter rather than fixed so the consumer can set a
    deadline appropriate to a background classification. A shadow record is
    worth less than a fast customer answer; spending the interactive budget on
    it would let a background comparison delay the queue.
    """
    now = int(time.time())
    if now - request.turn_created_at > SHADOW_TTL_SECONDS:
        return ShadowOutcome(recorded=False, reason=REASON_SHADOW_EXPIRED)

    count = await _conversation_sample_count(
        session, tenant_id=request.tenant_id, conversation_ref_id=request.conversation_ref_id
    )
    if count >= MAX_SAMPLES_PER_CONVERSATION:
        return ShadowOutcome(recorded=False, reason=REASON_SHADOW_QUOTA)

    from platform_core.agent_runtime.semantic.context import build_context
    from platform_core.agent_runtime.semantic.service import (
        AnalysisRequest,
        SemanticBudget,
        analyze,
    )

    ctx = build_context(
        current_turn_id=request.turn_id,
        current_text=request.turn_text,
        history=request.history,
        mode=SemanticMode.SHADOW,
        capabilities=request.capabilities,
    )
    assessment = await analyze(
        AnalysisRequest(
            context=ctx,
            lease_owner_type=request.lease_owner_type,
            detection=request.detection,
        ),
        provider=provider,
        capabilities=request.capabilities,
        # The caller's budget when given, otherwise a short one. Shadow shares
        # the classification budget with the live path and the live path has
        # already spent its deadline; a shadow record is worth less than a fast
        # answer.
        budget=budget or SemanticBudget(deadline_seconds=1.0, max_retries=0),
    )

    await task_store.record_assessment(
        session,
        tenant_id=request.tenant_id,
        conversation_ref_id=request.conversation_ref_id,
        turn_id=request.turn_id,
        assessment=assessment,
    )
    return ShadowOutcome(recorded=True, reason="SHADOW_RECORDED", assessment=assessment)


async def _conversation_sample_count(
    session: AsyncSession, *, tenant_id: uuid.UUID, conversation_ref_id: uuid.UUID
) -> int:
    from sqlalchemy import func, select

    from platform_core.agent_runtime.tasks.models import SemanticAssessmentRow

    return int(
        (
            await session.execute(
                select(func.count())
                .select_from(SemanticAssessmentRow)
                .where(
                    SemanticAssessmentRow.tenant_id == tenant_id,
                    SemanticAssessmentRow.conversation_ref_id == conversation_ref_id,
                )
            )
        ).scalar_one()
    )


def capabilities_for_shadow(
    available: dict[str, Any],
) -> dict[str, CapabilityView]:
    """Project a tenant's tool definitions into the model's capability set.

    A shadow run is told what *exists*, not what the AI may call, because the
    point is to compare what the model would have proposed against what the
    rules did. Withholding the write tools would make the comparison unable to
    detect a model that over-proposes - which is the failure worth measuring.
    The risk class travels with each one so arbitration can still refuse it.
    """
    out: dict[str, CapabilityView] = {}
    for name, cap in available.items():
        risk = getattr(cap, "risk", None) or getattr(cap, "risk_class", "read")
        out[name] = CapabilityView(
            tool_name=name,
            risk_class=str(risk),
            allowed_task_kinds=frozenset({"read", "write", "clarify"}),
        )
    return out


__all__ = [
    "MAX_SAMPLES_PER_CONVERSATION",
    "REASON_SHADOW_EXPIRED",
    "REASON_SHADOW_QUOTA",
    "SHADOW_TTL_SECONDS",
    "ShadowOutcome",
    "ShadowRequest",
    "capabilities_for_shadow",
    "record_shadow",
]
