"""SHD-01: shadow mode changes no business state.

The claim under test is precise, and it is the kind of claim that is easy to
assert and hard to prove: given the same input, `off` and `shadow` produce the
same customer messages, the same conversation owner, the same number of
business tool calls and the same Case state - and `shadow` adds only
evaluation rows.

So this file measures those four things directly, before and after, in a real
database, rather than asserting that the code "does not write". A shadow path
that accidentally called the gateway would show up as a nonzero tool-execution
count; one that enqueued an outbox event would show up in the event count.

The negative half matters as much: the same comparison run with a model that
proposes an unknown tool and a write must still leave the counts unchanged,
because that is the output most likely to tempt a later change into acting.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid

import pytest
from sqlalchemy import func, select, text

from platform_core.agent_runtime.intent import classify
from platform_core.agent_runtime.semantic.contracts import SemanticMode
from platform_core.agent_runtime.semantic.shadow import (
    MAX_SAMPLES_PER_CONVERSATION,
    REASON_SHADOW_EXPIRED,
    REASON_SHADOW_QUOTA,
    SHADOW_TTL_SECONDS,
    ShadowRequest,
    capabilities_for_shadow,
    record_shadow,
)
from platform_core.agent_runtime.semantic.validator import CapabilityView
from platform_core.agent_runtime.tasks.models import SemanticAssessmentRow
from platform_core.llm.provider import ChatResult

pytestmark = pytest.mark.integration

ADMIN_URL = os.environ.get(
    "APP_ADMIN_DATABASE_URL",
    "postgresql+psycopg://platform:platform@localhost:5435/platform",
)

TURN_TEXT = "查一下 SO-240918 到哪了，没发货就改成上海办公室，另外补一下发票"

# A model output that is deliberately unhelpful: it proposes a tool the tenant
# does not have, and a write the platform must not perform. If the shadow path
# ever started honouring a suggestion, this is the input that would expose it.
HOSTILE_OUTPUT = json.dumps(
    {
        "primary_intent": "business_action",
        "secondary_intents": [],
        "scene": "order_fulfilment",
        "business_line": "unspecified",
        "intents": [
            {
                "task_kind": "write",
                "source_turn_id": "t-1",
                "evidence": [{"turn_id": "t-1", "start": 0, "end": 3}],
                "slots": [
                    {
                        "name": "address",
                        "value": "上海办公室",
                        "origin": "customer_stated",
                        "confirmed": True,
                    }
                ],
                "missing_slots": [],
                "depends_on": [],
            }
        ],
        "evidence_spans": [],
        "confidence_band": "high",
        "needs_clarification": False,
        "emotion_signal": None,
        "tool_candidates": [
            {"tool_name": "crm.update_account", "reason": "customer asked"},
            {"tool_name": "rm_rf_slash", "reason": "injected"},
        ],
    },
    ensure_ascii=False,
)


class _StubProvider:
    def __init__(self, text: str) -> None:
        self._text = text
        self.calls = 0

    async def complete(self, messages: list[object], **kwargs: object) -> ChatResult:
        self.calls += 1
        return ChatResult(text=self._text, model=str(kwargs.get("model") or "stub"))


def _run(coro: object) -> object:
    return asyncio.run(coro, loop_factory=asyncio.SelectorEventLoop)  # type: ignore[arg-type]


async def _dispose(engine: object) -> None:
    await engine.dispose()  # type: ignore[attr-defined]


def _engine() -> object:
    from platform_core.db import create_engine

    return create_engine(str(ADMIN_URL))


async def _seed(engine: object) -> tuple[uuid.UUID, uuid.UUID]:
    from sqlalchemy import text as _text

    tenant_id = uuid.uuid4()
    conv = uuid.uuid4()
    async with engine.begin() as conn:  # type: ignore[attr-defined]
        await conn.execute(
            _text(
                "INSERT INTO tenants (id, name, slug, status) VALUES (:i, 'Shadow', :s, 'active')"
            ),
            {"i": tenant_id, "s": f"shadow-{tenant_id.hex[:10]}"},
        )
        await conn.execute(
            _text(
                "INSERT INTO conversation_control_leases (id, tenant_id, conversation_ref_id, "
                "owner_type, mode, lease_version, changed_reason, updated_at) "
                "VALUES (:lease, :t, :c, 'ai', 'AI_ACTIVE', 1, 'test', 0)"
            ),
            {"lease": uuid.uuid4(), "t": tenant_id, "c": conv},
        )
    return tenant_id, conv


async def _business_snapshot(
    engine: object, tenant_id: uuid.UUID, conv: uuid.UUID
) -> dict[str, int]:
    """The four things SHD-01 says must not change."""
    async with engine.begin() as conn:  # type: ignore[attr-defined]
        counts: dict[str, int] = {}
        for label, sql in (
            (
                "conversation_turns",
                "SELECT count(*) FROM conversation_turns WHERE conversation_ref_id = :c",
            ),
            ("tool_proposals", "SELECT count(*) FROM tool_proposals WHERE tenant_id = :t"),
            ("tool_executions", "SELECT count(*) FROM tool_executions WHERE tenant_id = :t"),
            ("outbox_events", "SELECT count(*) FROM outbox_events WHERE tenant_id = :t"),
            ("cases", "SELECT count(*) FROM cases WHERE tenant_id = :t"),
            (
                "lease_owner",
                "SELECT count(*) FROM conversation_control_leases "
                "WHERE tenant_id = :t AND conversation_ref_id = :c AND owner_type = 'ai'",
            ),
        ):
            result = await conn.execute(text(sql), {"t": tenant_id, "c": conv})
            counts[label] = int(result.scalar() or 0)
    return counts


async def _assessment_count(engine: object, tenant_id: uuid.UUID, conv: uuid.UUID) -> int:
    async with engine.begin() as conn:  # type: ignore[attr-defined]
        result = await conn.execute(
            text(
                "SELECT count(*) FROM semantic_assessments "
                "WHERE tenant_id = :t AND conversation_ref_id = :c"
            ),
            {"t": tenant_id, "c": conv},
        )
        return int(result.scalar() or 0)


def _request(tenant_id: uuid.UUID, conv: uuid.UUID, *, age_seconds: int = 0) -> ShadowRequest:
    import time

    return ShadowRequest(
        tenant_id=tenant_id,
        conversation_ref_id=conv,
        turn_id="t-1",
        turn_text=TURN_TEXT,
        history=[],
        lease_owner_type="ai",
        capabilities=capabilities_for_shadow(
            {"order.get_status": CapabilityView("order.get_status", "read")}
        ),
        turn_created_at=int(time.time()) - age_seconds,
        detection=classify(TURN_TEXT),
    )


async def _run_shadow(engine: object, request: ShadowRequest, provider: object) -> object:
    from platform_core.db import session_scope_with_url

    async with session_scope_with_url(str(ADMIN_URL)) as session:  # type: ignore[attr-defined]
        return await record_shadow(session, request, provider)


# --- the comparison ---------------------------------------------------------


def test_shadow_changes_no_business_state() -> None:
    """The whole claim: same input, same business state, one extra row."""
    engine = _engine()
    tenant_id, conv = _run(_seed(engine))

    before = _run(_business_snapshot(engine, tenant_id, conv))
    assessments_before = _run(_assessment_count(engine, tenant_id, conv))
    assert assessments_before == 0

    provider = _StubProvider(HOSTILE_OUTPUT)
    outcome = _run(_run_shadow(engine, _request(tenant_id, conv), provider))

    after = _run(_business_snapshot(engine, tenant_id, conv))

    assert outcome.recorded is True
    assert before == after, f"shadow changed business state: {before} -> {after}"
    # The only permitted difference.
    assert _run(_assessment_count(engine, tenant_id, conv)) == 1
    assert provider.calls == 1
    _run(_dispose(engine))


def test_a_hostile_shadow_output_still_changes_nothing() -> None:
    """A model proposing a write and an injected tool name must not move a
    single business row - that is the output most likely to tempt a future
    change into acting on it."""
    engine = _engine()
    tenant_id, conv = _run(_seed(engine))
    before = _run(_business_snapshot(engine, tenant_id, conv))

    _run(_run_shadow(engine, _request(tenant_id, conv), _StubProvider(HOSTILE_OUTPUT)))

    after = _run(_business_snapshot(engine, tenant_id, conv))
    assert before == after
    _run(_dispose(engine))


def test_the_stored_assessment_refused_the_unknown_tools() -> None:
    """SEC-04 + TOOL-01: the record shows refusals without storing the values."""
    engine = _engine()
    tenant_id, conv = _run(_seed(engine))
    _run(_run_shadow(engine, _request(tenant_id, conv), _StubProvider(HOSTILE_OUTPUT)))

    row = _run(_read_assessment(engine, tenant_id, conv))
    assert row is not None
    assert "SEMANTIC_TOOL_NOT_AVAILABLE" in row["reason_codes"]
    assert row["effective_decision"] == "shadow_recorded"
    assert row["agreement"] == "disagree"
    # The address the model proposed is nowhere in the stored record.
    assert "上海办公室" not in json.dumps(row, ensure_ascii=False)
    _run(_dispose(engine))


async def _read_assessment(
    engine: object, tenant_id: uuid.UUID, conv: uuid.UUID
) -> dict[str, object] | None:
    async with engine.begin() as conn:  # type: ignore[attr-defined]
        result = await conn.execute(
            text(
                "SELECT mode, agreement, effective_decision, reason_codes, snapshot "
                "FROM semantic_assessments WHERE tenant_id = :t AND conversation_ref_id = :c"
            ),
            {"t": tenant_id, "c": conv},
        )
        row = result.mappings().first()
        if row is None:
            return None
        return {
            "mode": row["mode"],
            "agreement": row["agreement"],
            "effective_decision": row["effective_decision"],
            "reason_codes": list(row["reason_codes"]),
            "snapshot": dict(row["snapshot"]),
        }


# --- off mode ---------------------------------------------------------------


def test_off_mode_makes_no_provider_call() -> None:
    """The baseline half of the comparison: in `off` there is no call at all,
    so the two modes differ only by the evaluation row."""
    engine = _engine()
    tenant_id, conv = _run(_seed(engine))
    provider = _StubProvider(HOSTILE_OUTPUT)

    # `off` short-circuits inside `analyze` before the provider is touched, so
    # this needs no session and no database write of any kind.
    _run(_analyze_off(_request(tenant_id, conv), provider))

    assert provider.calls == 0
    assert _run(_assessment_count(engine, tenant_id, conv)) == 0
    _run(_dispose(engine))


async def _analyze_off(request: ShadowRequest, provider: object) -> object:
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
        mode=SemanticMode.OFF,
        capabilities=request.capabilities,
    )
    return await analyze(
        AnalysisRequest(
            context=ctx,
            lease_owner_type=request.lease_owner_type,
            detection=request.detection,
        ),
        provider=provider,
        capabilities=request.capabilities,
        budget=SemanticBudget(),
    )


# --- expiry and quota -------------------------------------------------------


def test_an_expired_sample_is_skipped_not_analysed() -> None:
    """An assessment of a conversation state that has moved on looks
    comparable and is not, so it is dropped."""
    engine = _engine()
    tenant_id, conv = _run(_seed(engine))
    provider = _StubProvider(HOSTILE_OUTPUT)

    outcome = _run(
        _run_shadow(
            engine, _request(tenant_id, conv, age_seconds=SHADOW_TTL_SECONDS + 60), provider
        )
    )

    assert outcome.recorded is False
    assert outcome.reason == REASON_SHADOW_EXPIRED
    assert provider.calls == 0
    assert _run(_assessment_count(engine, tenant_id, conv)) == 0
    _run(_dispose(engine))


def test_a_fresh_sample_is_recorded() -> None:
    engine = _engine()
    tenant_id, conv = _run(_seed(engine))
    outcome = _run(_run_shadow(engine, _request(tenant_id, conv), _StubProvider(HOSTILE_OUTPUT)))
    assert outcome.recorded is True
    _run(_dispose(engine))


def test_the_per_conversation_quota_stops_sampling() -> None:
    """One verbose customer must not crowd out every other tenant's samples."""
    engine = _engine()
    tenant_id, conv = _run(_seed(engine))

    async def _fill() -> None:
        from platform_core.db import session_scope_with_url

        async with session_scope_with_url(str(ADMIN_URL)) as session:  # type: ignore[attr-defined]
            now = 1_700_000_000
            for i in range(MAX_SAMPLES_PER_CONVERSATION):
                session.add(
                    SemanticAssessmentRow(
                        tenant_id=tenant_id,
                        conversation_ref_id=conv,
                        turn_id=f"t-{i}",
                        mode="shadow",
                        effective_decision="shadow_recorded",
                        agreement="rules_only",
                        reason_codes=[],
                        validation_status="not_attempted",
                        snapshot={},
                        created_at=now,
                    )
                )

    _run(_fill())
    provider = _StubProvider(HOSTILE_OUTPUT)
    outcome = _run(_run_shadow(engine, _request(tenant_id, conv), provider))

    assert outcome.recorded is False
    assert outcome.reason == REASON_SHADOW_QUOTA
    assert provider.calls == 0, "a quota-exceeded sample still spent a model call"
    _run(_dispose(engine))


# --- capability projection --------------------------------------------------


def test_shadow_sees_write_tools_so_over_proposing_is_measurable() -> None:
    """Withholding write tools would make the comparison unable to detect a
    model that proposes one - which is the failure worth measuring.

    The risk class still travels, so arbitration can refuse it; the point is
    that the refusal is recorded rather than made invisible.
    """
    caps = capabilities_for_shadow(
        {
            "order.get_status": CapabilityView("order.get_status", "read"),
            "crm.update_account": CapabilityView("crm.update_account", "confirmed_write"),
        }
    )
    assert "crm.update_account" in caps
    assert caps["crm.update_account"].risk_class == "confirmed_write"
    # And the task kinds it may serve are the read/write vocabulary, not a
    # blanket "anything goes".
    assert caps["crm.update_account"].allowed_task_kinds == frozenset({"read", "write", "clarify"})


# --- accounting -------------------------------------------------------------


def test_analyses_are_counted_per_tenant() -> None:
    """The rows are tenant-scoped, so one tenant's volume cannot be read as
    another's."""
    engine = _engine()
    tenant_a, conv_a = _run(_seed(engine))
    tenant_b, conv_b = _run(_seed(engine))

    _run(_run_shadow(engine, _request(tenant_a, conv_a), _StubProvider(HOSTILE_OUTPUT)))

    assert _run(_assessment_count(engine, tenant_a, conv_a)) == 1
    assert _run(_assessment_count(engine, tenant_b, conv_b)) == 0
    _run(_dispose(engine))


# Referenced by the assertions above through the ORM class; kept explicit so a
# future rename fails here rather than silently skipping the registry import.
_ = (select, func, SemanticAssessmentRow)
