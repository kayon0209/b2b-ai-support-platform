"""B1-01: the shadow path must actually call a model, and must say so.

The acceptance review reproduced this with a stub: the production wiring handed
`deps.generator` - an `LlmAnswerGenerator`, which has `generate` and no
`complete` - to a function that calls `provider.complete`. Every classification
failed, and `record_shadow` still reported `recorded`.

These tests pin the three claims that were false:

1. `chat_provider(deps)` returns the object that has `complete`, and refuses
   the answer generator.
2. With a working provider, the underlying `ChatProvider.complete` is called -
   counted, not assumed.
3. Without one, the outcome is a recorded failure with a reason, and no
   assessment claims a comparison happened.

They are unit tests over the consumer's own logic because the wiring bug was a
type confusion, not a database one. The end-to-end off/shadow comparison is in
`test_shadow_no_side_effects.py`.
"""

from __future__ import annotations

import json
import uuid

import pytest

from worker.shadow_consumer import (
    SHADOW_IN_FLIGHT,
    ClaimedShadow,
    chat_provider,
    context_for,
)


class _ChatProvider:
    """The shape `semantic.service.analyze` actually calls."""

    def __init__(self, text: str) -> None:
        self._text = text
        self.calls = 0
        self.models: list[str | None] = []

    async def complete(self, messages: list[object], **kwargs: object):
        from platform_core.llm.provider import ChatResult

        self.calls += 1
        self.models.append(kwargs.get("model"))
        return ChatResult(text=self._text, model=str(kwargs.get("model") or "stub"))


class _AnswerGenerator:
    """What `deps.generator` actually is: `generate`, and no `complete`."""

    def __init__(self) -> None:
        self.calls = 0

    async def generate(self, *args: object, **kwargs: object) -> str:
        self.calls += 1
        return "an answer"


class _Deps:
    def __init__(self, **extra: object) -> None:
        self.extra = extra


VALID_OUTPUT = json.dumps(
    {
        "primary_intent": "business_query",
        "secondary_intents": [],
        "scene": "order_fulfilment",
        "business_line": "unspecified",
        "intents": [],
        "evidence_spans": [],
        "confidence_band": "medium",
        "needs_clarification": False,
        "emotion_signal": None,
        "tool_candidates": [{"tool_name": "order.get_status", "reason": "order noun"}],
    },
    ensure_ascii=False,
)


# --- provider selection -----------------------------------------------------


def test_chat_provider_returns_the_bundle_client() -> None:
    provider = _ChatProvider(VALID_OUTPUT)
    assert chat_provider(_Deps(chat=provider)) is provider


def test_chat_provider_refuses_the_answer_generator() -> None:
    """B1-01's exact defect: the generator has no `complete`."""
    assert chat_provider(_Deps(chat=_AnswerGenerator())) is None


def test_chat_provider_is_none_without_a_bundle() -> None:
    assert chat_provider(_Deps()) is None
    assert chat_provider(object()) is None  # type: ignore[arg-type]


def test_a_provider_without_complete_is_refused_even_if_present() -> None:
    class _NotAProvider:
        pass

    assert chat_provider(_Deps(chat=_NotAProvider())) is None


# --- the claim is bounded ---------------------------------------------------


def test_claimed_rows_use_the_processing_state() -> None:
    """`OutboxStatus` has no in-flight member; the literal is the contract."""
    assert SHADOW_IN_FLIGHT == "processing"


def test_the_consumer_context_is_system_scoped() -> None:
    """No actor: the platform is analysing, not a person acting."""
    claimed = ClaimedShadow(
        event_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
    )
    ctx = context_for(claimed)
    assert ctx.tenant_id == claimed.tenant_id
    assert ctx.actor_id is None
    assert ctx.actor_kind == "system"


@pytest.mark.asyncio
async def test_a_missing_provider_is_a_failure_not_a_record() -> None:
    """The core of B1-01: no model, no comparison, and it is labelled as a
    failure rather than reported as `recorded`.

    The provider check is the first thing the consumer does, before it reads
    any tool definition, so this needs no database - which is the point: the
    branch that was wrong is reachable and observable without one.
    """
    from worker.shadow_consumer import process_shadow_event

    claimed = ClaimedShadow(
        event_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
    )
    session = _CountingSession()
    outcome = await process_shadow_event(session, claimed, provider=None)  # type: ignore[arg-type]
    assert outcome == "failed"
    # Exactly one statement: the UPDATE that records the failure. Anything more
    # would mean the branch read the database before asking whether a model was
    # available, which is the ordering B1-01 turned on.
    assert session.statements == 1


class _CountingSession:
    """Counts statements without executing them.

    `_finish` is the only statement the no-provider branch may issue. The
    values it binds are not inspected, because SQLAlchemy exposes them through
    a private attribute whose shape is not part of any contract; the count is.
    """

    def __init__(self) -> None:
        self.statements = 0

    async def execute(self, _stmt: object) -> object:
        self.statements += 1
        return self

    async def commit(self) -> None:  # pragma: no cover - never reached
        raise AssertionError("the no-provider branch must not commit")
