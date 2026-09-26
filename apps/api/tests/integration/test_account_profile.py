"""Feature list 2.4: the customer profile is complete, honest, and narrow.

Against real Postgres because `attributes` is a JSON column whose contents are
whatever a CRM sync wrote, and the claim is about what this projection does
with them.

The three assertions that carry weight:

- **Non-whitelisted attributes do not escape.** The column is free-form and
  will grow; every future field must not become automatically visible to the
  model. Asserted with a fabricated sensitive-looking key.
- **Absent is reported, not defaulted.** Payment terms are what an agent
  quotes to a customer, so a default here is a wrong number said out loud.
- **"No such account" and "nothing on file" are different answers.** A caller
  that cannot tell them apart will show the wrong one.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from platform_core.identity.profile import account_profile, profile_for_contact

pytestmark = pytest.mark.integration

ADMIN_URL = os.environ.get(
    "APP_ADMIN_DATABASE_URL",
    "postgresql+psycopg://platform:platform@localhost:5435/platform",
)
APP_URL = "postgresql+psycopg://platform_app:platform_app@localhost:5435/platform"

TENANT = "01900000-0000-7000-8000-0000000000dc"
SLUG = "agent-account-profile"
ACCOUNT = "01900000-0000-7000-8000-0000000000f1"
CONTACT = "chatwoot-contact-9001"


def _run(coro):
    return asyncio.run(coro, loop_factory=asyncio.SelectorEventLoop)


def _seed_account(attributes: dict) -> None:
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO tenants (id, slug, name, status) VALUES "
                "(:id, :slug, 'Profile', 'active') ON CONFLICT (slug) DO NOTHING"
            ),
            {"id": TENANT, "slug": SLUG},
        )
        conn.execute(
            text(
                "INSERT INTO enterprise_accounts (id, tenant_id, name, tier, "
                "contract_status, attributes, created_at, updated_at) VALUES "
                "(:id, :t, 'Acme Manufacturing', 'strategic', 'active', "
                "CAST(:attrs AS jsonb), :now, :now) ON CONFLICT (id) DO NOTHING"
            ),
            {
                "id": ACCOUNT,
                "t": TENANT,
                "attrs": json.dumps(attributes),
                "now": int(time.time()),
            },
        )
        conn.execute(
            text(
                "INSERT INTO enterprise_account_contacts (id, tenant_id, "
                "enterprise_account_id, external_contact_id, created_at) VALUES "
                "(:id, :t, :acct, :ext, :now) ON CONFLICT DO NOTHING"
            ),
            {
                "id": str(uuid.uuid4()),
                "t": TENANT,
                "acct": ACCOUNT,
                "ext": CONTACT,
                "now": int(time.time()),
            },
        )
    admin.dispose()


def _clear() -> None:
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        conn.execute(
            text("DELETE FROM enterprise_account_contacts WHERE tenant_id = :t"), {"t": TENANT}
        )
        conn.execute(text("DELETE FROM enterprise_accounts WHERE tenant_id = :t"), {"t": TENANT})
        conn.execute(text("DELETE FROM tenants WHERE slug = :slug"), {"slug": SLUG})
    admin.dispose()


async def _profile(account_id):
    from sqlalchemy import text as sa_text

    from platform_core.db import create_engine as async_engine

    engine = async_engine(APP_URL)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        await session.execute(
            sa_text("SELECT set_config('app.tenant_id', :t, true)"), {"t": TENANT}
        )
        return await account_profile(session, tenant_id=uuid.UUID(TENANT), account_id=account_id)


async def _profile_for_contact(external_id: str):
    from sqlalchemy import text as sa_text

    from platform_core.db import create_engine as async_engine

    engine = async_engine(APP_URL)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        await session.execute(
            sa_text("SELECT set_config('app.tenant_id', :t, true)"), {"t": TENANT}
        )
        return await profile_for_contact(
            session, tenant_id=uuid.UUID(TENANT), external_contact_id=external_id
        )


@pytest.fixture(autouse=True)
def tenant() -> None:
    _clear()
    yield
    _clear()


def test_a_complete_profile_carries_tier_and_terms() -> None:
    _seed_account({"payment_terms": "月结 30 天", "credit_limit": 500000, "industry": "PCB"})
    profile = _run(_profile(uuid.UUID(ACCOUNT)))
    assert profile is not None
    assert profile.tier == "strategic"
    assert profile.attributes["payment_terms"] == "月结 30 天"
    assert profile.attributes["credit_limit"] == "500000"
    assert profile.complete


def test_an_attribute_outside_the_whitelist_does_not_escape() -> None:
    """The guard: a free-form column must not become automatically visible."""
    _seed_account(
        {
            "payment_terms": "月结 30 天",
            "internal_margin_note": "do not tell the customer this",
            "crm_secret_flag": "x",
        }
    )
    profile = _run(_profile(uuid.UUID(ACCOUNT)))
    assert profile is not None
    assert "internal_margin_note" not in profile.attributes
    assert "crm_secret_flag" not in profile.attributes
    assert "do not tell the customer this" not in str(profile.attributes)


def test_missing_payment_terms_are_reported_not_defaulted() -> None:
    """An agent quotes these aloud; a default would be a wrong number said."""
    _seed_account({"industry": "PCB"})
    profile = _run(_profile(uuid.UUID(ACCOUNT)))
    assert profile is not None
    assert "payment_terms" in profile.missing
    assert not profile.complete
    assert "payment_terms" not in profile.attributes


def test_an_empty_string_attribute_counts_as_absent() -> None:
    """A blank cell is not a recorded value."""
    _seed_account({"payment_terms": "   "})
    profile = _run(_profile(uuid.UUID(ACCOUNT)))
    assert profile is not None
    assert "payment_terms" in profile.missing


def test_a_missing_account_is_none_not_an_empty_profile() -> None:
    """'Not here' and 'nothing on file' must stay distinguishable."""
    _seed_account({"payment_terms": "月结 30 天"})
    assert _run(_profile(uuid.uuid4())) is None


def test_a_bound_contact_resolves_to_its_account_profile() -> None:
    _seed_account({"payment_terms": "月结 60 天"})
    profile = _run(_profile_for_contact(CONTACT))
    assert profile is not None
    assert profile.name == "Acme Manufacturing"


def test_an_unbound_contact_is_none_not_an_error() -> None:
    """Most contacts are unbound; treating that as a failure breaks the common case."""
    _seed_account({"payment_terms": "月结 60 天"})
    assert _run(_profile_for_contact("no-such-contact")) is None


def test_a_boolean_attribute_is_rendered_readably() -> None:
    _seed_account({"payment_terms": "月结 30 天", "credit_limit": True})
    profile = _run(_profile(uuid.UUID(ACCOUNT)))
    assert profile is not None
    assert profile.attributes["credit_limit"] == "yes"
