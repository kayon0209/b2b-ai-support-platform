"""Feature list 1.5: continuity follows the person, not the account.

The assertion this file exists for is a privacy boundary, not a convenience:
two conversations belonging to **two contacts on the same enterprise account**
must not see each other. Colleagues share an account, so resuming across that
boundary shows one employee's messages to another - the kind of failure that is
found by a customer rather than by a test.

The rest pin the ordinary behaviour: a contact's other conversations are
returned newest-first, the current one is excluded, and a re-delivered event
updates rather than failing the unique constraint.
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from platform_core.support_bridge.continuity import (
    contact_for_conversation,
    link_conversation,
    prior_conversations,
)

pytestmark = pytest.mark.integration

ADMIN_URL = os.environ.get(
    "APP_ADMIN_DATABASE_URL",
    "postgresql+psycopg://platform:platform@localhost:5435/platform",
)
APP_URL = "postgresql+psycopg://platform_app:platform_app@localhost:5435/platform"

TENANT = "01900000-0000-7000-8000-0000000000dd"
SLUG = "agent-continuity"


def _run(coro):
    return asyncio.run(coro, loop_factory=asyncio.SelectorEventLoop)


def _seed() -> None:
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO tenants (id, slug, name, status) VALUES "
                "(:id, :slug, 'Continuity', 'active') ON CONFLICT (slug) DO NOTHING"
            ),
            {"id": TENANT, "slug": SLUG},
        )
    admin.dispose()


def _clear() -> None:
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        conn.execute(text("DELETE FROM conversation_contacts WHERE tenant_id = :t"), {"t": TENANT})
        conn.execute(text("DELETE FROM tenants WHERE slug = :slug"), {"slug": SLUG})
    admin.dispose()


async def _session():
    from sqlalchemy import text as sa_text

    from platform_core.db import create_engine as async_engine

    engine = async_engine(APP_URL)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    session = factory()
    await session.execute(sa_text("SELECT set_config('app.tenant_id', :t, true)"), {"t": TENANT})
    return session


def _link(conversation_id, contact, channel=None):
    async def _inner():
        session = await _session()
        try:
            row = await link_conversation(
                session,
                tenant_id=uuid.UUID(TENANT),
                conversation_ref_id=conversation_id,
                external_contact_id=contact,
                channel=channel,
            )
            await session.commit()
            return row.id, row.channel
        finally:
            await session.close()

    return _run(_inner())


def _prior(contact, exclude=None, limit=5):
    async def _inner():
        session = await _session()
        try:
            return await prior_conversations(
                session,
                tenant_id=uuid.UUID(TENANT),
                external_contact_id=contact,
                exclude_conversation_ref_id=exclude,
                limit=limit,
            )
        finally:
            await session.close()

    return _run(_inner())


@pytest.fixture(autouse=True)
def tenant() -> None:
    _clear()
    _seed()
    yield
    _clear()


def test_a_conversation_remembers_its_contact() -> None:
    conv = uuid.uuid4()
    _link(conv, "contact-a", "wechat")
    assert _run(_contact_of(conv)) == "contact-a"


def test_prior_conversations_are_returned_newest_first() -> None:
    """Across a second boundary, newest really is first."""
    first = uuid.uuid4()
    _link(first, "contact-a")
    # `created_at` is whole seconds, so ordering within one second is a tie the
    # service breaks arbitrarily-but-stably by id. Sleeping is the only way to
    # test the chronological claim rather than the tie-break.
    time.sleep(1.1)
    second = uuid.uuid4()
    _link(second, "contact-a")
    prior = _prior("contact-a")
    assert [p.conversation_ref_id for p in prior] == [second, first]


def test_ordering_within_one_second_is_stable() -> None:
    """Same-second rows must not reshuffle between two identical queries."""
    _link(uuid.uuid4(), "contact-a")
    _link(uuid.uuid4(), "contact-a")
    first_read = [p.conversation_ref_id for p in _prior("contact-a")]
    second_read = [p.conversation_ref_id for p in _prior("contact-a")]
    assert len(first_read) == 2
    assert first_read == second_read


def test_the_current_conversation_is_excluded() -> None:
    """Asking "what else has this person said" must not quote this one."""
    first, second = uuid.uuid4(), uuid.uuid4()
    _link(first, "contact-a")
    _link(second, "contact-a")
    prior = _prior("contact-a", exclude=second)
    assert [p.conversation_ref_id for p in prior] == [first]


def test_two_contacts_on_one_account_do_not_see_each_other() -> None:
    """The privacy boundary: colleagues share an account, not a history."""
    mine, theirs = uuid.uuid4(), uuid.uuid4()
    _link(mine, "contact-a")
    _link(theirs, "contact-b")
    assert _prior("contact-a", exclude=mine) == []
    assert _prior("contact-b", exclude=theirs) == []


def test_a_contact_with_no_history_gets_an_empty_list() -> None:
    assert _prior("never-seen") == []


def test_relinking_is_idempotent_on_the_conversation() -> None:
    """A duplicate delivery is normal and must not look like an error."""
    conv = uuid.uuid4()
    first_id, _ = _link(conv, "contact-a")
    second_id, _ = _link(conv, "contact-a")
    assert first_id == second_id


def test_a_later_event_can_supply_the_missing_channel() -> None:
    """The first event often cannot name a channel; the second one can."""
    conv = uuid.uuid4()
    _link(conv, "contact-a", None)
    _id, channel = _link(conv, "contact-a", "email")
    assert channel == "email"


def test_a_known_channel_is_not_overwritten_by_a_later_blank() -> None:
    conv = uuid.uuid4()
    _link(conv, "contact-a", "wechat")
    _id, channel = _link(conv, "contact-a", None)
    assert channel == "wechat"


def test_an_unlinked_conversation_reports_no_contact() -> None:
    """None, not a placeholder: 'unknown' must not read as 'same person'."""
    assert _run(_contact_of(uuid.uuid4())) is None


def test_linking_requires_a_contact() -> None:
    async def _inner():
        session = await _session()
        try:
            await link_conversation(
                session,
                tenant_id=uuid.UUID(TENANT),
                conversation_ref_id=uuid.uuid4(),
                external_contact_id="",
            )
        finally:
            await session.close()

    with pytest.raises(ValueError):
        _run(_inner())


def test_the_limit_bounds_the_answer() -> None:
    for _ in range(4):
        _link(uuid.uuid4(), "contact-a")
    assert len(_prior("contact-a", limit=2)) == 2


async def _contact_of(conversation_id):
    session = await _session()
    try:
        return await contact_for_conversation(
            session, tenant_id=uuid.UUID(TENANT), conversation_ref_id=conversation_id
        )
    finally:
        await session.close()


def test_prior_conversations_carry_no_message_text() -> None:
    """A reference, not a transcript - redaction must not be bypassed here."""
    conv = uuid.uuid4()
    _link(conv, "contact-a")
    prior = _prior("contact-a")
    assert prior
    fields = set(vars(prior[0]))
    assert not any("text" in name or "content" in name for name in fields)
