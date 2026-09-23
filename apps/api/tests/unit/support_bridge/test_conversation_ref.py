"""Unit tests: one conversation must have exactly one platform identity.

The derivation of a conversation's UUID used to be written out in two
modules and skipped in a third. Nothing failed; instead a run queued by the
trigger endpoint was filed under the raw external id while the run that
answered was filed under the derived id, so an admin listing "runs for this
conversation" saw the queued placeholder and never the run that produced the
answer.

These tests pin the formula and, more importantly, pin the agreement between
every caller. A second copy of the rule is how they drift apart again.
"""

import uuid

import pytest

from platform_core.support_bridge.conversation_ref import conversation_ref_for
from worker.inbox_consumer import ClaimedEvent, _conversation_ref

TENANT_A = uuid.UUID("17ab2c52-7d95-5fba-a06c-b5641393831e")
TENANT_B = uuid.UUID("01900000-0000-7000-8000-000000000041")

# Captured from the live database, where turns and runs are already stored
# under this value. Changing the formula orphans every one of them, so this
# is a frozen constant rather than a recomputed expectation.
KNOWN_EXTERNAL = "362209f6-6727-4e74-82a0-4deea2ee7490"
KNOWN_REF = uuid.UUID("0a68a960-207b-55c2-82c6-7a7dd4d3655b")


def test_derivation_is_frozen() -> None:
    """Guard against a refactor that silently orphans stored rows.

    Every turn, run and control lease already in the database is keyed by
    this exact value. A change here is a data migration, not a refactor.
    """
    assert conversation_ref_for(TENANT_A, KNOWN_EXTERNAL) == KNOWN_REF


def test_derivation_is_deterministic() -> None:
    """The same conversation must map to the same row on every call."""
    first = conversation_ref_for(TENANT_A, "2")
    second = conversation_ref_for(TENANT_A, "2")
    assert first == second
    assert first.version == 5


def test_tenant_is_part_of_the_identity() -> None:
    """Two tenants with the same Chatwoot id are different conversations.

    Chatwoot ids are only unique within an account, and accounts are shared
    infrastructure; without the tenant in the derivation, tenant isolation
    would depend on RLS alone.
    """
    assert conversation_ref_for(TENANT_A, "2") != conversation_ref_for(TENANT_B, "2")


def test_different_conversations_differ() -> None:
    assert conversation_ref_for(TENANT_A, "2") != conversation_ref_for(TENANT_A, "3")


@pytest.mark.parametrize("external", ["", "   "])
def test_an_empty_external_id_is_refused(external: str) -> None:
    """An empty id would derive a valid UUID for 'no conversation'.

    That value then looks like a real conversation in every downstream
    query, which is far worse than failing here.
    """
    with pytest.raises(ValueError):
        conversation_ref_for(TENANT_A, external)


def test_the_worker_and_the_customer_endpoint_agree() -> None:
    """The regression this file exists for, restated for the verbatim contract.

    The path segment the customer endpoint reads and the `conversation_ref`
    payload key the worker reads are the SAME platform id, used verbatim on
    both sides. Neither derives it, so the answer the worker writes under the
    ref is found by the endpoint reading the same ref. The old failure was two
    readers that each derived - and derived differently.
    """
    from platform_core.agent_runtime.customer_router import parse_conversation_ref as api_parse
    from platform_core.support_bridge.conversation_ref import parse_conversation_ref

    # The endpoint and the worker share the one parser - identity, not equality,
    # is what stops them drifting to two rules again.
    assert api_parse is parse_conversation_ref

    # The customer endpoint reads the path verbatim.
    assert parse_conversation_ref(str(KNOWN_REF)) == KNOWN_REF
    # The worker reads the payload's ref verbatim.
    assert _conversation_ref(_event_ref(KNOWN_REF)) == KNOWN_REF
    # And the worker can still derive from a channel id when no ref was handed
    # to it - the one case where derivation is correct.
    assert _conversation_ref(_event(KNOWN_EXTERNAL)) == KNOWN_REF


def test_the_worker_ignores_a_missing_or_non_string_conversation_id() -> None:
    """A payload without a usable conversation id is skipped, not crashed."""
    assert _conversation_ref(_event(None)) is None
    assert _conversation_ref(_event("")) is None
    assert _conversation_ref(_event(2)) is None


def test_the_run_trigger_uses_the_shared_helper() -> None:
    """The trigger endpoint must read the ref the worker answers under.

    The run endpoint receives a platform ref in its path and uses it verbatim
    via the shared `parse_conversation_ref`. Asserted as identity rather than
    equal output: a second copy of the rule that agrees today is free to drift
    tomorrow, and a drift here is exactly the silent mis-file this whole file
    guards against.
    """
    from platform_core.agent_runtime import router as agent_router
    from platform_core.support_bridge.conversation_ref import parse_conversation_ref

    assert agent_router.parse_conversation_ref is parse_conversation_ref
    assert parse_conversation_ref(str(KNOWN_REF)) == KNOWN_REF


def _event(conversation_id: object) -> ClaimedEvent:
    payload: dict[str, object] = {"message_id": "10", "message_type": "incoming"}
    if conversation_id is not None:
        payload["conversation_id"] = conversation_id
    return ClaimedEvent(
        event_id=uuid.uuid4(),
        tenant_id=TENANT_A,
        delivery_id=str(uuid.uuid4()),
        event_type="message_created",
        minimized_payload=payload,
    )


def _event_ref(ref: uuid.UUID) -> ClaimedEvent:
    """An inbox event that already carries the platform ref (the 方案A encoding).

    A run queued by an operator reaches the conversation by its platform ref
    and the API hands that ref to the worker verbatim - no external id to
    derive from. The worker must return it as-is.
    """
    return ClaimedEvent(
        event_id=uuid.uuid4(),
        tenant_id=TENANT_A,
        delivery_id=str(uuid.uuid4()),
        event_type="message_created",
        minimized_payload={
            "message_id": "10",
            "message_type": "incoming",
            "conversation_ref": str(ref),
        },
    )
