"""Unit tests: the agent must never answer its own replies.

Regression tests for a live loop. Chatwoot emits `message_created` for
outbound messages, so when the agent's reply lands, it arrives back as a
new inbox event. The consumer only checked `event_type`, never the
direction, so each answer became the next question.

Observed against a real Chatwoot before the fix: one customer message
("What is the refund window for annual enterprise plans?") produced runs
until message id 17 — an unbounded stream of LLM calls and
customer-visible replies from a single question.

The guard is deliberately asymmetric: only an explicit `incoming`
message is answered. A missing or unrecognised `message_type` is
treated as non-actionable, because silently ignoring something we should
have answered is recoverable by a human, whereas a reply loop is not.
"""

import uuid

import pytest

from worker.inbox_consumer import (
    ACTIONABLE_EVENT_TYPES,
    CUSTOMER_MESSAGE_TYPES,
    ClaimedEvent,
    is_customer_message,
)


def _event(message_type: object, *, event_type: str = "message_created") -> ClaimedEvent:
    payload: dict[str, object] = {"conversation_id": "2", "message_id": "10"}
    if message_type is not None:
        payload["message_type"] = message_type
    return ClaimedEvent(
        event_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        delivery_id=str(uuid.uuid4()),
        event_type=event_type,
        minimized_payload=payload,
    )


def test_incoming_message_is_actionable() -> None:
    """The customer's own message is the whole point of the worker."""
    assert is_customer_message(_event("incoming")) is True


def test_outgoing_message_is_not_actionable() -> None:
    """This is the loop: our reply must not be read as the next question."""
    assert is_customer_message(_event("outgoing")) is False


@pytest.mark.parametrize("message_type", [None, "", "   ", "activity", "template", 0, 1])
def test_unknown_message_type_fails_safe(message_type: object) -> None:
    """Anything not explicitly inbound stays unanswered.

    `0`/`1` matter specifically: Chatwoot's REST API uses integers for
    message_type (0=incoming, 1=outgoing) while webhooks send strings. A
    payload from either shape must not slip past this guard.
    """
    assert is_customer_message(_event(message_type)) is False


def test_message_type_matching_is_case_and_whitespace_insensitive() -> None:
    """Provider casing is not a security boundary worth failing on."""
    assert is_customer_message(_event("  Incoming ")) is True
    assert is_customer_message(_event("OUTGOING")) is False


def test_actionable_types_and_customer_types_are_distinct_concepts() -> None:
    """`event_type` says what happened; `message_type` says who did it.

    Conflating them is exactly the bug: both filters must pass for a run.
    """
    assert ACTIONABLE_EVENT_TYPES == {"message_created"}
    assert CUSTOMER_MESSAGE_TYPES == {"incoming"}
    assert not (ACTIONABLE_EVENT_TYPES & CUSTOMER_MESSAGE_TYPES)
