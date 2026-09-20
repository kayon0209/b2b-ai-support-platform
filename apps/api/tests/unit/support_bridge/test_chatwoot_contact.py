"""Reading the contact id off a Chatwoot message.

This exists because the webhook does not carry it. Measured over every stored
`inbox_events` row: `contact_id` present 0/29, `sender_id` 1/29 - so
`minimize.py`'s extraction never fires in this deployment and the contact has
to come from the API. That makes the *shape* the part worth testing: it is the
difference between binding a customer and binding one of our own agents.
"""

from platform_core.support_bridge.chatwoot_client import _contact_id_from_message


def test_a_message_from_a_contact_yields_that_contact() -> None:
    assert _contact_id_from_message({"sender": {"id": 4242, "type": "contact"}}) == "4242"


def test_a_message_from_an_agent_is_not_a_customer() -> None:
    """The failure this guard exists for.

    A reply sent by one of our own agents also has a `sender`. Binding it would
    make the platform treat staff as a key account's contact, and every agent
    reply would then route as if the customer had spoken.
    """
    assert _contact_id_from_message({"sender": {"id": 7, "type": "user"}}) is None


def test_a_message_with_no_sender_yields_nothing() -> None:
    assert _contact_id_from_message({}) is None
    assert _contact_id_from_message({"sender": None}) is None
    assert _contact_id_from_message({"sender": {"type": "contact"}}) is None
