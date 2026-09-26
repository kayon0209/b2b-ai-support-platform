"""The ownership proof that backs the visitor read gate (feature 2.2/2.5).

These are the two pure pieces the worker's gate depends on, tested without a
worker or a database so they cannot be broken silently:

1. `DemoBusinessToolExecutor.verify_ownership` - the provider's own answer to
   "does this proof match this record". It is fail-closed: anything it cannot
   answer is None, and None is what makes the run refuse to publish.
2. The visitor token round-trip - `account` rides on the token only after a
   successful `/verify`, and survives `verify()` intact.
"""

from __future__ import annotations

import asyncio

from platform_core.integrations.demo_erp import DemoBusinessToolExecutor
from platform_core.support_bridge.visitor_token import issue, verify


def test_demo_verify_ownership_accepts_correct_tail() -> None:
    ex = DemoBusinessToolExecutor(context=None)
    assert asyncio.run(ex.verify_ownership("order.get_status", "SO-9001", "8888")) == "acme"
    assert asyncio.run(ex.verify_ownership("order.get_status", "SO-9002", "7777")) == "other-co"


def test_demo_verify_ownership_rejects_wrong_tail_and_unknown() -> None:
    ex = DemoBusinessToolExecutor(context=None)
    # Wrong tail, unknown order, empty proof: all None (refuse, never guess).
    assert asyncio.run(ex.verify_ownership("order.get_status", "SO-9001", "0000")) is None
    assert asyncio.run(ex.verify_ownership("order.get_status", "SO-9999", "8888")) is None
    assert asyncio.run(ex.verify_ownership("order.get_status", "SO-9001", "")) is None


def test_demo_verify_ownership_only_orders_carry_an_owner() -> None:
    ex = DemoBusinessToolExecutor(context=None)
    # Shipments/invoices inherit the order owner at the receipt level (gate 2 of
    # the orchestrator); the demo provider does not answer ownership for them,
    # which the caller must treat as a refusal, not a yes.
    assert asyncio.run(ex.verify_ownership("shipment.track", "SH-7001", "8888")) is None
    assert asyncio.run(ex.verify_ownership("invoice.get", "INV-1", "8888")) is None


def test_token_carries_account_only_after_verify() -> None:
    import uuid

    tid = uuid.uuid4()
    conv = uuid.uuid4()

    anon, _ = issue(tid, conv, "ext", ttl_seconds=3600)
    claim = verify(anon)
    assert claim.account is None, "a fresh session must not carry an account"

    proved, _ = issue(tid, conv, "ext", ttl_seconds=3600, account="acme")
    claim = verify(proved)
    assert claim.account == "acme", "the proven account must survive the round-trip"


def test_token_with_account_is_not_confusable_with_anon() -> None:
    import uuid

    tid = uuid.uuid4()
    conv = uuid.uuid4()
    # An empty-string account is not the same as absent: empty means "anonymous
    # visitor" (gate must fire), absent means "operator run" (no gate). The token
    # must never emit empty as a stored account.
    proved, _ = issue(tid, conv, "ext", ttl_seconds=3600, account="")
    claim = verify(proved)
    assert claim.account is None
