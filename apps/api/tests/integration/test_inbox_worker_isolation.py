"""The agent run reaches tenant data as `platform_app`, bound to its own tenant.

Two defects this file pins, both measured on the running deployment rather than
reasoned about:

1. **The whole run ran with RLS off.** The interactive worker opened its unit of
   work with `session_scope()`, which resolves to the bootstrap owner - a
   superuser with `rolbypassrls`. `flag_service` looks a flag up by key and
   relies on RLS to scope the row, so with two tenants defining
   `agent.business_read_enabled` the lookup returned an arbitrary one. Measured
   inside a run: `current_setting('app.tenant_id')` was tenant A while the flag
   row returned belonged to **tenant B**, whose value is `false`. The run then
   skipped the read-tool branch and abstained, so the customer saw "I couldn't
   verify an answer" and the cause was another tenant's configuration.

2. **Even on the right role, the binding did not survive a commit.** The run
   acquires the control lease, and `set_config(..., true)` is
   transaction-scoped, so every statement after that ran unbound - which on a
   non-bypassing role means **zero rows, no error**. `tenant_session` re-binds
   at `after_begin`; the third test here pins that.

The failure mode of both is the same and is why they need a test rather than a
comment: the run completes, answers something, and looks healthy.
"""

from __future__ import annotations

import json
import os
import uuid

import pytest
from sqlalchemy import create_engine, text

from platform_core.identity.tenant_context import TenantContext, tenant_session
from platform_core.support_bridge.conversation_ref import conversation_ref_for

pytestmark = pytest.mark.integration

ADMIN_URL = os.environ.get(
    "APP_ADMIN_DATABASE_URL",
    "postgresql+psycopg://platform:platform@localhost:5435/platform",
)

TENANT_A = uuid.UUID("01900000-0000-7000-8000-0000000000f1")
TENANT_B = uuid.UUID("01900000-0000-7000-8000-0000000000f2")
SLUG_A = "inbox-iso-a"
SLUG_B = "inbox-iso-b"

EXTERNAL = "conv-inbox-iso"
FLAG = "agent.business_read_enabled"

# Selects `order.get_status` (`tool_gateway/selector.py`) and routes to
# `business_read`. English on purpose: every Chinese phrasing classifies as a
# knowledge question today, which would make this test unable to observe the
# thing it is about (see `docs/research/chinese-intent-measurement.md`).
QUESTION = "What is the status of order SO-9001?"


def _admin():
    return create_engine(ADMIN_URL)


def _cleanup() -> None:
    admin = _admin()
    with admin.begin() as conn:
        # `contact_facts` is in the list because a run writes durable facts
        # from the customer's turn ("order_ref" is extracted from this
        # question); leaving it out makes the teardown fail on the FK.
        for table in (
            "contact_facts",
            "feature_flags",
            "conversation_turns",
            "conversation_control_leases",
            "agent_runs",
            "inbox_events",
            "audit_events",
        ):
            conn.execute(
                text(
                    f"DELETE FROM {table} WHERE tenant_id IN "  # noqa: S608 - fixed literal names
                    "(SELECT id FROM tenants WHERE slug = ANY(:s))"
                ),
                {"s": [SLUG_A, SLUG_B]},
            )
        conn.execute(text("DELETE FROM tenants WHERE slug = ANY(:s)"), {"s": [SLUG_A, SLUG_B]})
    admin.dispose()


@pytest.fixture
def seeded() -> None:
    """Two tenants, the same flag key, opposite values.

    The identical key is the whole fixture. With one tenant the lookup cannot
    return the wrong row, so the test would pass on the broken code.
    """
    _cleanup()
    admin = _admin()
    with admin.begin() as conn:
        for tid, slug in ((TENANT_A, SLUG_A), (TENANT_B, SLUG_B)):
            conn.execute(
                text(
                    "INSERT INTO tenants (id, slug, name, status) VALUES "
                    "(:id, :slug, 'Inbox Isolation', 'active')"
                ),
                {"id": tid, "slug": slug},
            )
        conn.execute(
            text(
                "INSERT INTO feature_flags (id, tenant_id, key, description, enabled, "
                "rollout_percent, created_at) VALUES "
                "(gen_random_uuid(), :a, :k, '', true, 100, 0), "
                "(gen_random_uuid(), :b, :k, '', false, 0, 0)"
            ),
            {"a": TENANT_A, "b": TENANT_B, "k": FLAG},
        )
        conn.execute(
            text(
                "INSERT INTO inbox_events (id, tenant_id, delivery_id, event_type, "
                "payload_hash, minimized_payload, status, received_at) VALUES "
                "(gen_random_uuid(), :t, :d, 'message_created', 'h', CAST(:p AS jsonb), "
                "'received', :ts)"
            ),
            {
                "t": TENANT_A,
                "d": f"inbox-iso-{uuid.uuid4()}",
                "p": json.dumps(
                    {
                        "message_type": "incoming",
                        "message_id": "m-1",
                        "conversation_id": EXTERNAL,
                        "contact_id": "c-1",
                        # Carried directly so no Chatwoot reader is needed:
                        # `resolve_question` uses a payload that already has
                        # content.
                        "content": QUESTION,
                    }
                ),
                "ts": 1_700_000_000,
            },
        )
    admin.dispose()
    yield
    _cleanup()


def _run_row() -> tuple[str, str, str | None] | None:
    """The run this event produced, read across RLS as the owner."""
    admin = _admin()
    with admin.begin() as conn:
        row = conn.execute(
            text(
                "SELECT route, status, abstain_reason FROM agent_runs "
                "WHERE tenant_id = :t AND conversation_ref_id = :c "
                "ORDER BY started_at DESC LIMIT 1"
            ),
            {"t": TENANT_A, "c": conversation_ref_for(TENANT_A, EXTERNAL)},
        ).fetchone()
    admin.dispose()
    return (row[0], row[1], row[2]) if row else None


# --- 1. The flag that decides the run is the run's OWN tenant's --------------


def test_a_run_is_decided_by_its_own_flag_not_another_tenants(
    seeded: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tenant A's run must take the read-tool branch, because A's flag is on.

    Tenant B's identical key is off. On the broken code the lookup returned B's
    row, the branch was skipped, and the run fell through to the knowledge path
    - indistinguishable in the response from "the corpus had no answer".

    `TOOL_UNAVAILABLE` is the discriminator and it is a precise one: it can only
    be produced by entering `_attempt_business_read` (which then finds no
    `business_api` connector seeded here) and handing off from inside it. The
    knowledge path abstains with a different reason code.

    The wiring's configuration gate refuses to build without an LLM key, and
    this run never reaches the model (the read-tool branch fails before
    generation), so a placeholder satisfies the gate here. It cannot come from
    the job environment: the retrieval endpoint reads the same key and would
    then switch from the deterministic embedder to a provider with nowhere to
    call.

    Two process-wide caches have to be reset with it, not one. `get_settings`
    is `lru_cache`d, and so is `get_model_bundle` - and the bundle is the thing
    the gate actually consults. An earlier test in the batch has already cached
    a `None` bundle, so clearing only the settings lets the stale `None` be
    returned and this test fails in a batch while passing alone. Cleaned on
    both sides, so the placeholder cannot outlive the test either.
    """
    import asyncio

    from platform_core.config import get_settings
    from platform_core.llm.factory import reset_model_bundle
    from worker.inbox_consumer import drain_once
    from worker.wiring import build_interactive_deps, queue_bookkeeping_session

    monkeypatch.setenv("APP_LLM_API_KEY", "test-only-key-not-a-credential")
    get_settings.cache_clear()
    reset_model_bundle()
    try:

        async def drain() -> None:
            deps = build_interactive_deps()
            async with queue_bookkeeping_session() as bookkeeping:
                await drain_once(bookkeeping, deps=deps, batch=5)

        asyncio.run(drain())
    finally:
        reset_model_bundle()
        get_settings.cache_clear()

    row = _run_row()
    assert row is not None, "the event produced no run at all"
    route, _status, reason = row
    assert route == "business_read", row
    assert reason == "TOOL_UNAVAILABLE", (
        "the run did not enter the read-tool branch, so the flag it read was not "
        f"this tenant's own: {row}"
    )


def test_the_two_tenants_really_do_disagree(seeded: None) -> None:
    """The premise of the test above, asserted rather than assumed.

    If a future seed gives both tenants the same value, the test above stops
    being able to fail - and a guard that cannot fail is worse than no guard.
    """
    admin = _admin()
    with admin.begin() as conn:
        rows = conn.execute(
            text(
                "SELECT tenant_id, enabled, rollout_percent FROM feature_flags "
                "WHERE key = :k AND tenant_id = ANY(:ids)"
            ),
            {"k": FLAG, "ids": [TENANT_A, TENANT_B]},
        ).all()
    admin.dispose()
    values = {r[0]: (r[1], r[2]) for r in rows}
    assert values[TENANT_A] == (True, 100), values
    assert values[TENANT_B] == (False, 0), values


# --- 2. Which role each session connects as ---------------------------------


def test_the_processing_session_is_the_non_bypassing_role(seeded: None) -> None:
    """RLS is a boundary only if the role cannot bypass it."""
    import asyncio

    async def check() -> tuple[str, bool, int]:
        ctx = TenantContext(tenant_id=TENANT_A, actor_id=None, actor_kind="system")
        async with tenant_session(ctx) as session:
            user = await session.scalar(text("select current_user"))
            bypass = await session.scalar(
                text("select rolbypassrls from pg_roles where rolname = current_user")
            )
            visible = await session.scalar(text("select count(*) from feature_flags"))
        return str(user), bool(bypass), int(visible)

    user, bypass, visible = asyncio.run(check())
    assert user == "platform_app", user
    assert bypass is False, "a bypassing role makes every tenant filter advisory"
    # Unbound or bound-to-another-tenant would both be 0 here; A's row proves
    # the binding reached the connection.
    assert visible >= 1, "the tenant binding did not reach the session"


def test_the_binding_survives_a_commit_inside_the_scope(seeded: None) -> None:
    """`set_config(..., true)` is transaction-scoped; the lease commit ends it.

    Measured before `tenant_session` moved to the RLS layer: binding, then
    committing, then reading the flags gave `UNKNOWN_FLAG` for every flag, on a
    session that was otherwise correct. On the owner role the same mistake is
    invisible (RLS is bypassed), which is how it survived.
    """
    import asyncio

    async def check() -> tuple[object, object, int]:
        ctx = TenantContext(tenant_id=TENANT_A, actor_id=None, actor_kind="system")
        async with tenant_session(ctx) as session:
            before = await session.scalar(text("select current_setting('app.tenant_id', true)"))
            await session.commit()
            after = await session.scalar(text("select current_setting('app.tenant_id', true)"))
            visible = await session.scalar(text("select count(*) from feature_flags"))
        return before, after, int(visible)

    before, after, visible = asyncio.run(check())
    assert str(before) == str(TENANT_A), before
    assert str(after) == str(TENANT_A), (
        "the RLS binding was lost at COMMIT; every later read in the run would "
        f"return zero rows without an error (got {after!r})"
    )
    assert visible >= 1


# --- 3. The owner role is confined to claiming ------------------------------


def test_the_bookkeeping_session_is_the_only_owner_role_use() -> None:
    """Claiming needs the owner; nothing else does.

    A claim runs before any tenant is known and every queue table is FORCE-RLS,
    so the claim must not be on the app role. This asserts the exception exists
    and is the one documented here - if a second owner-role helper appears, the
    agent path can drift back into it.
    """
    import asyncio

    from worker.wiring import queue_bookkeeping_session

    async def check() -> tuple[str, bool]:
        async with queue_bookkeeping_session() as session:
            user = await session.scalar(text("select current_user"))
            bypass = await session.scalar(
                text("select rolbypassrls from pg_roles where rolname = current_user")
            )
        return str(user), bool(bypass)

    user, bypass = asyncio.run(check())
    assert user == "platform"
    assert bypass is True, "the documented reason for this exception is exactly the bypass"


def test_the_inbox_drain_never_opens_the_owner_role_itself() -> None:
    """The worker's answer path must not reach for `session_scope`.

    `drain_once` takes the bookkeeping session as an argument, so the only place
    that decides the role is `InboxWorker.run_once`. This reads the module to
    assert the path itself contains no owner-role session - the defect was a
    single `session_scope()` call, and it is invisible in every behavioural test
    on a bypassing role.
    """
    import pathlib

    import worker.inbox_consumer as consumer

    source = pathlib.Path(consumer.__file__).read_text(encoding="utf-8")
    assert "session_scope()" not in source, (
        "inbox_consumer opened an owner-role session; the agent run must reach "
        "tenant data through tenant_session"
    )
    assert "tenant_session(" in source, "the processing session is no longer a tenant_session"
