"""Integration: the cross-channel timeline and the per-channel breakdown.

Both are read-side, and both exist because a capability was already built and had
no consumer:

- `continuity.prior_conversations` (migration 0045) was called only from its own
  tests, so feature 1.5's cross-device continuity was unreachable.
- the per-channel rate had no source at all until `conversation_contacts.channel`
  got a writer (migration 0050).

The assertions that matter are the negative ones: a run with no contact row must
be visible as a bucket rather than dropped, and a rate over an empty channel must
be `None` rather than `0.0`.
"""

from __future__ import annotations

import os
import time
import uuid

import pytest
from sqlalchemy import create_engine as _admin_engine
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from platform_core.db import create_engine

pytestmark = pytest.mark.integration

ADMIN_URL = os.environ.get(
    "APP_ADMIN_DATABASE_URL",
    "postgresql+psycopg://platform:platform@localhost:5435/platform",
)
APP_URL = os.environ.get(
    "APP_TEST_DATABASE_URL",
    "postgresql+psycopg://platform_app:platform_app@localhost:5435/platform",
)

_NS = uuid.uuid5(uuid.NAMESPACE_URL, "b2b-ai-support/tests/channel-timeline")
TENANT = str(uuid.uuid5(_NS, "tenant"))
TENANT_OTHER = str(uuid.uuid5(_NS, "tenant-other"))

_TEARDOWN = ("conversation_turns", "conversation_contacts", "agent_runs")


def _run(coro):
    import asyncio

    return asyncio.run(coro, loop_factory=asyncio.SelectorEventLoop)


def _wipe(where: str, params: dict) -> None:
    admin = _admin_engine(ADMIN_URL)
    with admin.begin() as conn:
        for table in _TEARDOWN:
            conn.execute(text(f"DELETE FROM {table} WHERE {where}"), params)  # noqa: S608
    admin.dispose()


@pytest.fixture(autouse=True)
def _clean():
    _wipe("tenant_id IN (:a, :b)", {"a": TENANT, "b": TENANT_OTHER})
    yield
    _wipe("tenant_id IN (:a, :b)", {"a": TENANT, "b": TENANT_OTHER})


@pytest.fixture(scope="module", autouse=True)
def _tenants():
    admin = _admin_engine(ADMIN_URL)
    with admin.begin() as conn:
        for tid, slug in ((TENANT, "chl-a"), (TENANT_OTHER, "chl-b")):
            conn.execute(
                text(
                    "INSERT INTO tenants (id, slug, name, status) VALUES "
                    "(:id, :slug, :slug, 'active') ON CONFLICT (slug) DO NOTHING"
                ),
                {"id": tid, "slug": slug},
            )
    yield
    with admin.begin() as conn:
        sub = "(SELECT id FROM tenants WHERE slug LIKE 'chl-%')"
        for table in _TEARDOWN:
            conn.execute(text(f"DELETE FROM {table} WHERE tenant_id IN {sub}"))  # noqa: S608
        conn.execute(text("DELETE FROM tenants WHERE slug LIKE 'chl-%'"))
    admin.dispose()


async def _with_session(tenant: str, fn):
    engine = create_engine(APP_URL)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            await session.execute(
                text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant}
            )
            try:
                result = await fn(session)
            except Exception:
                await session.rollback()
                raise
            await session.commit()
            return result
    finally:
        await engine.dispose()


def _seed_run(
    tenant: str,
    ref: uuid.UUID,
    *,
    status: str = "completed",
    started_at: int | None = None,
    abstain_reason: str | None = None,
    intent: dict | None = None,
) -> uuid.UUID:
    import json

    run_id = uuid.uuid4()
    admin = _admin_engine(ADMIN_URL)
    with admin.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO agent_runs (id, tenant_id, conversation_ref_id, route, status, "
                "started_at, model_config, retrieval_config, policy_version, code_version, "
                "trace_id, input_hash, token_usage, abstain_reason) VALUES "
                "(:id, :t, :r, 'knowledge_qa', :s, :started, CAST(:cfg AS jsonb), "
                "CAST('{}' AS jsonb), 'v1', 'test', 'tr', 'h', CAST('{}' AS jsonb), :reason)"
            ),
            {
                "id": str(run_id),
                "t": tenant,
                "r": str(ref),
                "s": status,
                "started": started_at if started_at is not None else int(time.time()),
                "cfg": json.dumps(
                    {
                        "intent": intent
                        or {"business_line": "pcb", "scene": "order", "primary_kind": "eta"}
                    }
                ),
                "reason": abstain_reason,
            },
        )
    admin.dispose()
    return run_id


def _link(tenant: str, ref: uuid.UUID, *, contact: str, channel: str | None, key: str) -> None:
    from platform_core.support_bridge.continuity import link_conversation

    _run(
        _with_session(
            tenant,
            lambda s: link_conversation(
                s,
                tenant_id=uuid.UUID(tenant),
                conversation_ref_id=ref,
                external_contact_id=contact,
                channel=channel,
                external_conversation_key=key,
            ),
        )
    )


def _channel_report(tenant: str, window_seconds: int = 7 * 24 * 3600):
    from platform_core.evaluation.channels import aggregate_channel_distribution

    return _run(
        _with_session(
            tenant,
            lambda s: aggregate_channel_distribution(
                s, tenant_id=uuid.UUID(tenant), window_seconds=window_seconds
            ),
        )
    )


# --- the per-channel breakdown ---------------------------------------------


def test_runs_are_counted_under_the_channel_they_arrived_on() -> None:
    email = uuid.uuid4()
    wechat = uuid.uuid4()
    _link(TENANT, email, contact="buyer@example.test", channel="email", key="<r@a.test>")
    _link(TENANT, wechat, contact="openid_abc", channel="wechat", key="openid_abc")
    _seed_run(TENANT, email)
    _seed_run(TENANT, wechat)
    _seed_run(TENANT, wechat)

    dist = _channel_report(TENANT)
    assert dist.by_channel["email"].runs == 1
    assert dist.by_channel["wechat"].runs == 2
    assert dist.total_runs == 3


def test_a_run_with_no_contact_row_is_a_bucket_not_a_drop() -> None:
    """The platform's own surface has no channel. Dropping it would make the
    channels fail to add up, and a reader would assume the numbers were wrong."""
    _seed_run(TENANT, uuid.uuid4())

    dist = _channel_report(TENANT)
    assert dist.unlinked_runs == 1
    assert dist.by_channel["(unlinked)"].runs == 1


def test_a_contact_with_no_channel_is_its_own_bucket() -> None:
    """A CRM sync often cannot name the channel; folding it into a named one
    would invent an attribution."""
    ref = uuid.uuid4()
    _link(TENANT, ref, contact="someone", channel=None, key="k")
    _seed_run(TENANT, ref)

    dist = _channel_report(TENANT)
    assert dist.unnamed_channel_runs == 1
    assert dist.by_channel["(unnamed)"].runs == 1
    assert "email" not in dist.by_channel


def test_the_channel_rate_uses_the_same_strict_definition() -> None:
    """A run the customer had to ask again about is not an automation - if this
    used a looser rule the per-channel and per-category figures would disagree
    and neither would be trusted."""
    ref = uuid.uuid4()
    _link(TENANT, ref, contact="buyer@example.test", channel="email", key="<r@a.test>")
    now = int(time.time())
    # Answered twice on the same category in one conversation: one resolution.
    _seed_run(TENANT, ref, started_at=now - 200)
    _seed_run(TENANT, ref, started_at=now - 100)

    dist = _channel_report(TENANT)
    assert dist.by_channel["email"].runs == 2
    assert dist.by_channel["email"].automated == 1
    assert dist.by_channel["email"].automation_rate == 0.5


def test_an_abstention_counts_as_escalated_not_automated() -> None:
    ref = uuid.uuid4()
    _link(TENANT, ref, contact="buyer@example.test", channel="email", key="<r@a.test>")
    _seed_run(TENANT, ref, status="abstained", abstain_reason="NO_AUTHORIZED_EVIDENCE")

    stat = _channel_report(TENANT).by_channel["email"]
    assert stat.automated == 0
    assert stat.escalated == 1
    assert stat.automation_rate == 0.0


def test_no_runs_at_all_means_no_rate() -> None:
    """None, not 0.0 - "nobody asked" and "everything escalated" are opposite."""
    dist = _channel_report(TENANT)
    assert dist.total_runs == 0
    assert dist.automation_rate is None
    assert dist.by_channel == {}


def test_another_tenant_sees_none_of_our_channels() -> None:
    ref = uuid.uuid4()
    _link(TENANT, ref, contact="buyer@example.test", channel="email", key="<r@a.test>")
    _seed_run(TENANT, ref)

    dist = _channel_report(TENANT_OTHER)
    assert dist.total_runs == 0
    assert dist.by_channel == {}


# --- the cross-channel timeline --------------------------------------------


def test_other_conversations_of_the_same_person_are_returned() -> None:
    """Feature 1.5: asked on WeChat, then wrote an email - one person."""
    from platform_core.support_bridge.continuity import prior_conversations

    wechat = uuid.uuid4()
    email = uuid.uuid4()
    _link(TENANT, wechat, contact="buyer@example.test", channel="wechat", key="k1")
    _link(TENANT, email, contact="buyer@example.test", channel="email", key="k2")

    prior = _run(
        _with_session(
            TENANT,
            lambda s: prior_conversations(
                s,
                tenant_id=uuid.UUID(TENANT),
                external_contact_id="buyer@example.test",
                exclude_conversation_ref_id=email,
            ),
        )
    )
    assert [p.conversation_ref_id for p in prior] == [wechat]
    assert prior[0].channel == "wechat"


def test_the_timeline_carries_no_message_text() -> None:
    """A reference, not content - the privacy rules for conversation text stay in
    one place and are not bypassed by a continuity feature."""
    from platform_core.support_bridge.continuity import prior_conversations

    other = uuid.uuid4()
    current = uuid.uuid4()
    _link(TENANT, other, contact="buyer@example.test", channel="email", key="k1")
    _link(TENANT, current, contact="buyer@example.test", channel="email", key="k2")
    _seed_run(TENANT, other)

    prior = _run(
        _with_session(
            TENANT,
            lambda s: prior_conversations(
                s,
                tenant_id=uuid.UUID(TENANT),
                external_contact_id="buyer@example.test",
                exclude_conversation_ref_id=current,
            ),
        )
    )
    fields = set(vars(prior[0]))
    assert fields == {"conversation_ref_id", "channel", "opened_at", "lease_version"}


def test_an_unknown_contact_has_no_timeline() -> None:
    """The honest answer, and what the endpoint turns into an empty list rather
    than a 404."""
    from platform_core.support_bridge.continuity import contact_for_conversation

    found = _run(
        _with_session(
            TENANT,
            lambda s: contact_for_conversation(
                s, tenant_id=uuid.UUID(TENANT), conversation_ref_id=uuid.uuid4()
            ),
        )
    )
    assert found is None
