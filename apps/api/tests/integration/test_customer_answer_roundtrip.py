"""Integration tests: an answer the worker writes is the one the customer reads.

This is the round trip that broke, quietly, twice.

A customer types into the platform's own chat surface. The worker answers and
writes the turn back; the customer timeline then reads it. Both ends derive
the conversation id independently - the worker from the inbox payload, the
customer endpoint from the path - and when they disagree the answer is
written, then never found. No error anywhere: the run completes, the answer
exists, and the customer is still staring at an empty thread.

A unit test cannot catch that. Both derivations can be correct in isolation
and the two sides can still be talking about different rows, so this drives
the real database and reads back through RLS, which is what the endpoint
actually does.
"""

import asyncio
import os
import uuid

import pytest
from sqlalchemy import create_engine, select, text

from platform_core.agent_runtime.models import ConversationTurn, RunStatus
from platform_core.agent_runtime.orchestrator import RunOutcome
from platform_core.db import app_role_url, session_scope_with_url
from platform_core.identity.tenant_context import TenantContext, apply_rls_tenant
from platform_core.support_bridge.conversation_ref import conversation_ref_for
from worker.inbox_consumer import ClaimedEvent, _persist_memory

pytestmark = pytest.mark.integration

ADMIN_URL = os.environ.get(
    "APP_ADMIN_DATABASE_URL",
    "postgresql+psycopg://platform:platform@localhost:5435/platform",
)
TENANT = uuid.UUID("01900000-0000-7000-8000-00000000c0de")
QUESTION = "What are your support hours?"
ANSWER = "09:00 to 18:00, Monday to Friday."


@pytest.fixture
def tenant() -> object:
    engine = create_engine(ADMIN_URL)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO tenants (id, slug, name, status) VALUES "
                "(:id, 'roundtrip-test', 'Round Trip Tenant', 'active') "
                "ON CONFLICT (slug) DO NOTHING"
            ),
            {"id": str(TENANT)},
        )
        conn.execute(
            text("DELETE FROM conversation_turns WHERE tenant_id = :tid"), {"tid": str(TENANT)}
        )
    yield engine
    with engine.begin() as conn:
        conn.execute(
            text("DELETE FROM conversation_turns WHERE tenant_id = :tid"), {"tid": str(TENANT)}
        )
    engine.dispose()


def _event(external: str) -> ClaimedEvent:
    """A platform-native event: a conversation id, but no Chatwoot account."""
    return ClaimedEvent(
        event_id=uuid.uuid4(),
        tenant_id=TENANT,
        delivery_id=str(uuid.uuid4()),
        event_type="message_created",
        minimized_payload={
            "conversation_id": external,
            "message_id": str(uuid.uuid4()),
            "message_type": "incoming",
        },
    )


def _completed() -> RunOutcome:
    """What `_dispatch` now returns for a platform-native conversation.

    It used to return OUTBOUND_TARGET_MISSING and the run was FAILED, which
    is why nothing was ever written for the customer to read.
    """
    return RunOutcome(
        run_id=uuid.uuid4(),
        status=RunStatus.COMPLETED,
        route="knowledge_qa",
        answer_text=ANSWER,
    )


async def _worker_writes(external: str) -> None:
    async with session_scope_with_url(app_role_url()) as session:
        await apply_rls_tenant(
            session, TenantContext(tenant_id=TENANT, actor_id=None, actor_kind="system")
        )
        await _persist_memory(
            session, event=_event(external), question=QUESTION, outcome=_completed()
        )


async def _timeline_reads(conversation_ref_id: uuid.UUID) -> list[tuple[str, str]]:
    """Read the way the customer endpoint does: tenant-bound, under RLS."""
    async with session_scope_with_url(app_role_url()) as session:
        await apply_rls_tenant(
            session, TenantContext(tenant_id=TENANT, actor_id=None, actor_kind="system")
        )
        rows = (
            (
                await session.execute(
                    select(ConversationTurn)
                    .where(ConversationTurn.conversation_ref_id == conversation_ref_id)
                    .order_by(ConversationTurn.ts.asc(), ConversationTurn.id.asc())
                )
            )
            .scalars()
            .all()
        )
        return [(str(r.role), str(r.text_redacted)) for r in rows]


def test_the_answer_the_worker_wrote_is_the_one_the_customer_reads(tenant: object) -> None:
    """The round trip, against the real database."""
    external = str(uuid.uuid4())
    derived = conversation_ref_for(TENANT, external)

    asyncio.run(_worker_writes(external), loop_factory=asyncio.SelectorEventLoop)
    seen = asyncio.run(_timeline_reads(derived), loop_factory=asyncio.SelectorEventLoop)

    assert seen == [("customer", QUESTION), ("agent", ANSWER)]


def test_reading_under_the_raw_external_id_finds_nothing(tenant: object) -> None:
    """Why the round trip broke: the two ends used different ids.

    The raw external id looks like a perfectly good conversation id, which is
    exactly what made the mismatch invisible - a read under it returns an
    empty timeline rather than an error.
    """
    external = str(uuid.uuid4())
    asyncio.run(_worker_writes(external), loop_factory=asyncio.SelectorEventLoop)

    raw = uuid.UUID(external)
    seen = asyncio.run(_timeline_reads(raw), loop_factory=asyncio.SelectorEventLoop)

    assert seen == []


def test_another_tenant_cannot_read_the_round_trip(tenant: object) -> None:
    """RLS, not just the id: the answer is private to its tenant."""
    external = str(uuid.uuid4())
    derived = conversation_ref_for(TENANT, external)
    asyncio.run(_worker_writes(external), loop_factory=asyncio.SelectorEventLoop)

    other = uuid.UUID("01900000-0000-7000-8000-00000000beef")

    async def _read_as_other() -> list[tuple[str, str]]:
        async with session_scope_with_url(app_role_url()) as session:
            await apply_rls_tenant(
                session, TenantContext(tenant_id=other, actor_id=None, actor_kind="system")
            )
            rows = (
                (
                    await session.execute(
                        select(ConversationTurn).where(
                            ConversationTurn.conversation_ref_id == derived
                        )
                    )
                )
                .scalars()
                .all()
            )
            return [(str(r.role), str(r.text_redacted)) for r in rows]

    assert asyncio.run(_read_as_other(), loop_factory=asyncio.SelectorEventLoop) == []
