"""Integration: a contact's contract tier reaches the handoff decision.

The research report (难点 5) wants tier to drive 转人工优先级: a key account's
complaint goes to the people who own that relationship. `sla_policy_for_tier`
already covered the SLA half, but nothing could say *which* account a
conversation belongs to, so tier never reached routing.

This file covers the whole slice against real Postgres, because both halves are
claims about rows: that a binding is writable and visible to its tenant, that
it is invisible to another tenant (RLS), and that the routing outcome changes
because of it.

Two of these tests are mutation guards for the two judgement calls, and one
records a boundary that is deliberately not implemented.
"""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import async_sessionmaker

pytestmark = pytest.mark.integration

ADMIN_URL = os.environ.get(
    "APP_ADMIN_DATABASE_URL",
    "postgresql+psycopg://platform:platform@localhost:5435/platform",
)
APP_URL = "postgresql+psycopg://platform_app:platform_app@localhost:5435/platform"

TENANT = "01900000-0000-7000-8000-0000000000c5"
OTHER_TENANT = "01900000-0000-7000-8000-0000000000c6"
SLUG = "account-contact-routing"
OTHER_SLUG = "account-contact-routing-other"

CONTACT = "chatwoot-contact-9001"
COMPLAINT = "板子短路了，我要索赔"

STRATEGIC = "STRATEGIC_ACCOUNT_REQUIRES_HUMAN"
GENERIC = "COMPLAINT_REQUIRES_HUMAN"


def _run(coro):
    return asyncio.run(coro, loop_factory=asyncio.SelectorEventLoop)


def _seed_tenant(tenant_id: str, slug: str) -> None:
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO tenants (id, slug, name, status) VALUES "
                "(:id, :slug, 'Account Contact Routing', 'active') "
                "ON CONFLICT (slug) DO NOTHING"
            ),
            {"id": tenant_id, "slug": slug},
        )
    admin.dispose()


def _clear() -> None:
    statements = (
        # Children before parents: the binding carries a composite FK to
        # enterprise_accounts, so it has to go first or the delete poisons the
        # next run.
        "DELETE FROM enterprise_account_contacts WHERE tenant_id = :t",
        "DELETE FROM citations WHERE tenant_id = :t",
        "DELETE FROM agent_runs WHERE tenant_id = :t",
        "DELETE FROM audit_events WHERE tenant_id = :t",
        "DELETE FROM conversation_control_leases WHERE tenant_id = :t",
        "DELETE FROM enterprise_accounts WHERE tenant_id = :t",
    )
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        for tenant in (TENANT, OTHER_TENANT):
            for statement in statements:
                conn.execute(text(statement), {"t": tenant})
        for slug in (SLUG, OTHER_SLUG):
            conn.execute(text("DELETE FROM tenants WHERE slug = :slug"), {"slug": slug})
    admin.dispose()


@pytest.fixture(autouse=True)
def clean_tenant() -> None:
    _seed_tenant(TENANT, SLUG)
    _seed_tenant(OTHER_TENANT, OTHER_SLUG)
    _clear()
    yield
    _clear()


def _seed_account(*, tenant_id: str, tier: str, contract_status: str = "active") -> str:
    account_id = str(uuid.uuid4())
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO enterprise_accounts (id, tenant_id, name, tier, "
                "contract_status, attributes, created_at, updated_at) VALUES "
                "(:i, :t, 'Key Account', :tier, :cs, '{}'::jsonb, 0, 0)"
            ),
            {"i": account_id, "t": tenant_id, "tier": tier, "cs": contract_status},
        )
    admin.dispose()
    return account_id


async def _bind(*, tenant_id: str, account_id: str, external_contact_id: str = CONTACT) -> None:
    from platform_core.db import create_engine as async_engine
    from platform_core.identity import org
    from platform_core.identity.tenant_context import TenantContext

    engine = async_engine(APP_URL)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    ctx = TenantContext(tenant_id=uuid.UUID(tenant_id), actor_id=None, actor_kind="system")
    async with factory() as session:
        await session.execute(
            text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant_id}
        )
        await org.bind_contact(
            session,
            ctx=ctx,
            account_id=uuid.UUID(account_id),
            external_contact_id=external_contact_id,
        )
        await session.commit()
    await engine.dispose()


class _RecordingSender:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def send_message(
        self, *, account_id, conversation_id, content, command_id, private: bool = False
    ):
        self.calls.append({"content": content, "command_id": command_id, "private": private})

        class _Result:
            ambiguous = False

        return _Result()


async def _execute(*, question: str = COMPLAINT, contact_id: str | None = CONTACT):
    from platform_core.agent_runtime.orchestrator import AgentOrchestrator, OrchestratorDeps
    from platform_core.db import create_engine
    from platform_core.identity import lease_service
    from platform_core.retrieval.hybrid import PrincipalScope

    tid = uuid.UUID(TENANT)
    conv = uuid.uuid4()
    sender = _RecordingSender()
    engine = create_engine(APP_URL)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async with factory() as session:
        await session.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": TENANT})
        lease = await lease_service.acquire_or_get(session, tenant_id=tid, conversation_ref_id=conv)
        await session.commit()
    expected_version = int(lease.lease_version)

    async with factory() as session:
        await session.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": TENANT})
        orch = AgentOrchestrator(session, OrchestratorDeps(sender=sender))
        outcome = await orch.run(
            tenant_id=tid,
            conversation_ref_id=conv,
            question=question,
            principal=PrincipalScope(principal_types=("role",), principal_ids=("ai_agent",)),
            expected_lease_version=expected_version,
            chatwoot_account_id="1",
            chatwoot_conversation_id="1",
            contact_id=contact_id,
        )
        await session.commit()
    await engine.dispose()
    return outcome, sender.calls


@pytest.fixture
def handoff_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    """Turn on the private handoff note, which is off by default.

    The note is the only place the account reaches the receiving human, so the
    test that asserts on it has to have it on. `get_settings` is lru_cached, so
    setting the environment alone does nothing without clearing it - and it is
    cleared again on the way out so this test cannot change the next one.
    """
    from platform_core.config import get_settings

    monkeypatch.setenv("APP_HANDOFF_EVIDENCE_ENABLED", "true")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_a_bound_strategic_contact_hands_off_to_the_account_team(
    handoff_evidence: None,
) -> None:
    """The consumer: tier changes who is told, not whether a handoff happens."""
    account_id = _seed_account(tenant_id=TENANT, tier="strategic")
    _run(_bind(tenant_id=TENANT, account_id=account_id))

    outcome, sent = _run(_execute())

    assert outcome.handoff is True
    assert outcome.abstain_reason == STRATEGIC
    # The private note names the account, so the receiving team is not left
    # guessing which strategic account this was.
    notes = [c["content"] for c in sent if "account_id" in c["content"]]
    assert notes and account_id in notes[0]


def test_an_unbound_contact_gets_the_ordinary_handoff() -> None:
    """Most contacts are unbound; that is normal, not an error.

    Also the negative half of the feature: without this assertion, "strategic"
    could simply be hardcoded and nothing would fail.
    """
    outcome, _sent = _run(_execute(contact_id="contact-that-is-not-bound"))

    assert outcome.abstain_reason == GENERIC


def test_no_contact_id_at_all_still_hands_off() -> None:
    """The contact is an enrichment, never a dependency of the handoff.

    If the API lookup fails or the payload has no contact, the customer must
    still not be answered - losing the tier must not lose the handoff.
    """
    outcome, _sent = _run(_execute(contact_id=None))

    assert outcome.abstain_reason == GENERIC
    assert outcome.handoff is True


def test_a_churned_contract_does_not_get_the_dedicated_route() -> None:
    """A contract that has ended no longer buys the dedicated route.

    Mirrors `sla_policy_for_tier`, which already reads `contract_status`: one
    contract attribute should not mean two different things in two places.
    """
    account_id = _seed_account(tenant_id=TENANT, tier="strategic", contract_status="churned")
    _run(_bind(tenant_id=TENANT, account_id=account_id))

    outcome, _sent = _run(_execute())

    assert outcome.abstain_reason == GENERIC


def test_a_standard_tier_contact_gets_the_ordinary_handoff() -> None:
    """Mutation guard for the tier set: 'enterprise' is in, 'standard' is not."""
    account_id = _seed_account(tenant_id=TENANT, tier="standard")
    _run(_bind(tenant_id=TENANT, account_id=account_id))

    outcome, _sent = _run(_execute())

    assert outcome.abstain_reason == GENERIC


def test_another_tenants_binding_is_invisible() -> None:
    """RLS, asserted rather than assumed.

    The binding is what turns an ordinary customer into a key account, so a
    cross-tenant read here is not just a data leak - it is a routing decision
    made from another tenant's contracts.
    """
    from platform_core.db import create_engine
    from platform_core.identity import org

    account_id = _seed_account(tenant_id=OTHER_TENANT, tier="strategic")
    _run(_bind(tenant_id=OTHER_TENANT, account_id=account_id))

    async def read_as_this_tenant() -> object:
        engine = create_engine(APP_URL)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            await session.execute(
                text("SELECT set_config('app.tenant_id', :t, true)"), {"t": TENANT}
            )
            facts = await org.account_facts_for_contact(
                session,
                tenant_id=uuid.UUID(TENANT),
                external_contact_id=CONTACT,
            )
        await engine.dispose()
        return facts

    assert _run(read_as_this_tenant()) is None
