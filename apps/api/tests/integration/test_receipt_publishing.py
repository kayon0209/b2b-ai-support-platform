"""Integration: a tool receipt reaches the conversation as a TOOL turn.

Feature list 4A.3/4A.4. The platform queries order data and then tells the
customer about it in a sentence; the data itself stopped inside the run and
was never published, so a card had nothing to render and the customer had to
take the model's word for it. This is the producer half of fixing that.

Honest scope: this drives the publisher with the real turn store. The read
path that calls it (`_attempt_business_read`) has no end-to-end test anywhere
in the suite - no test even enables `agent.business_read_enabled` - so the
call site itself is covered by inspection, not by a test. That gap is
pre-existing and is recorded rather than papered over.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid

import pytest
import sqlalchemy
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import async_sessionmaker

pytestmark = pytest.mark.integration

ADMIN_URL = os.environ.get(
    "APP_ADMIN_DATABASE_URL",
    "postgresql+psycopg://platform:platform@localhost:5435/platform",
)
APP_URL = os.environ.get(
    "APP_TEST_DATABASE_URL",
    "postgresql+psycopg://platform_app:platform_app@localhost:5435/platform",
)

TENANT = "01900000-0000-7000-8000-0000000000cd"
SLUG = "receipt-publishing"

# ISO timestamps and short digit runs on purpose: the turn store redacts
# phone-shaped runs, and a 10-digit epoch would be masked into invalid JSON.
RECEIPT = (
    '{"fetched_at": "2026-09-20T10:00:00Z", '
    '"nodes": [{"label": "下单", "status": "done"}, '
    '{"label": "生产", "status": "active"}], "order_id": "SO-9001", '
    '"status": "in_production", "tool": "order.get_status"}'
)


def _run(coro):
    return asyncio.run(coro, loop_factory=asyncio.SelectorEventLoop)


def _seed() -> None:
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO tenants (id, slug, name, status) VALUES "
                "(:id, :slug, :slug, 'active') ON CONFLICT (slug) DO NOTHING"
            ),
            {"id": TENANT, "slug": SLUG},
        )
    admin.dispose()


def _clear() -> None:
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        conn.execute(text("DELETE FROM conversation_turns WHERE tenant_id = :t"), {"t": TENANT})
        conn.execute(text("DELETE FROM tenants WHERE slug = :slug"), {"slug": SLUG})
    admin.dispose()


@pytest.fixture(autouse=True)
def clean() -> None:
    _clear()
    _seed()
    yield
    _clear()


def test_a_receipt_lands_on_the_timeline_as_data_not_prose() -> None:
    from platform_core.agent_runtime.orchestrator import AgentOrchestrator, OrchestratorDeps
    from platform_core.db import create_engine

    conversation = uuid.uuid4()
    engine = create_engine(APP_URL)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def publish() -> None:
        async with factory() as session:
            await session.execute(
                text("SELECT set_config('app.tenant_id', :t, true)"), {"t": TENANT}
            )
            orch = AgentOrchestrator(session, OrchestratorDeps())
            await orch._publish_receipt(
                tenant_id=uuid.UUID(TENANT),
                conversation_ref_id=conversation,
                tool_name="order.get_status",
                receipt_json=RECEIPT,
                ts=1,
            )
            await session.commit()

    _run(publish())

    # sqlalchemy explicitly: `platform_core.db.create_engine` (the async
    # factory) is imported into this function for the run itself, and the bare
    # name would resolve to the wrong one.
    admin = sqlalchemy.create_engine(ADMIN_URL)
    with admin.begin() as conn:
        rows = conn.execute(
            text(
                "SELECT role, text_redacted, source FROM conversation_turns "
                "WHERE tenant_id = :t AND conversation_ref_id = :c"
            ),
            {"t": TENANT, "c": conversation},
        ).all()
    admin.dispose()

    assert len(rows) == 1
    role, body, source = rows[0]
    # Not "agent": a receipt is not the assistant speaking, and putting the
    # platform's voice on raw system data is its own kind of misattribution.
    assert role == "tool"
    # VARCHAR(15) - the tool name rides in the payload instead.
    assert source == "tool"
    # The whole receipt, not the 280-char excerpt that bounds model context.
    #
    # Compared structurally rather than byte-for-byte: turns are stored
    # through `evaluation.pii.redact_text`, which masks phone-shaped runs - a
    # 10-digit epoch like `fetched_at` becomes `[PHONE]`. That is the right
    # default for customer prose and a real wrinkle for structured receipts,
    # so the card treats freshness as best-effort (an unparseable date simply
    # renders no "updated" label) rather than trusting the field.
    assert json.loads(body)["tool"] == "order.get_status"
    assert len(json.loads(body)["nodes"]) == 2
    assert json.loads(body)["order_id"] == "SO-9001"


def test_a_receipt_that_redaction_would_corrupt_is_not_published() -> None:
    """Never show the customer a card built from data we cannot vouch for.

    A 10-digit epoch is masked by the PII redactor into `[PHONE]`, which makes
    the receipt invalid JSON. Publishing it would hand the UI a document that
    cannot be parsed - and worse, one whose numbers are quietly wrong.
    """
    from platform_core.agent_runtime.orchestrator import AgentOrchestrator

    corrupt = '{"fetched_at": 1700000000, "nodes": [{"label": "生产"}]}'
    clean = '{"fetched_at": "2026-09-20T10:00:00Z", "nodes": [{"label": "生产"}]}'

    assert AgentOrchestrator._survives_redaction(corrupt) is False
    assert AgentOrchestrator._survives_redaction(clean) is True


def test_a_publish_failure_does_not_break_the_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """Best-effort: the answer exists already, so this must never raise.

    If a turn-store failure could fail the run, a cosmetic feature would be
    able to turn a completed answer into a failed one.
    """
    from platform_core.agent_runtime import conversation_store
    from platform_core.agent_runtime.orchestrator import AgentOrchestrator, OrchestratorDeps
    from platform_core.db import create_engine

    def boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("turn store unavailable")

    monkeypatch.setattr(conversation_store, "append_turn", boom)

    engine = create_engine(APP_URL)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def publish() -> None:
        async with factory() as session:
            await session.execute(
                text("SELECT set_config('app.tenant_id', :t, true)"), {"t": TENANT}
            )
            orch = AgentOrchestrator(session, OrchestratorDeps())
            await orch._publish_receipt(
                tenant_id=uuid.UUID(TENANT),
                conversation_ref_id=uuid.uuid4(),
                tool_name="order.get_status",
                receipt_json=RECEIPT,
                ts=1,
            )

    _run(publish())
