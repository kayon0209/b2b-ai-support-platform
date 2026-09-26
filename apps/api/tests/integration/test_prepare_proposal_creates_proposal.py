"""B1-05: "准备提案" must create a real ToolProposal.

The acceptance review called `prepare_proposal`, got HTTP 200 with the task in
`awaiting_confirmation` and `proposal_id=NULL`, and counted zero new
`tool_proposals` rows. The UI read "生成待确认提案，等待坐席确认后执行" over a
task with nothing to confirm.

These tests assert the three outcomes that must be distinguishable:

1. A task with a write tool and complete arguments produces a real proposal
   row, bound to the task, and the task's `proposal_id` points at it.
2. A task with no write capability for this tenant goes to `needs_human` with
   the planner's reason - never to a confirmation state that will not arrive.
3. Changing the action's arguments bumps `action_revision` and creates a
   *different* proposal, so a confirmation bound to the old one no longer
   matches.
"""

from __future__ import annotations

import json
import os
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from platform_core.identity.middleware import TenantContextMiddleware
from platform_core.identity.tenant_context import TenantContext

pytestmark = pytest.mark.integration

ADMIN_URL = os.environ.get(
    "APP_ADMIN_DATABASE_URL",
    "postgresql+psycopg://platform:platform@localhost:5435/platform",
)

TENANT = "01900000-0000-7000-8000-00000000e001"
SLUG = "r1-prepare-proposal"
CONV = "01900000-0000-7000-8000-00000000e010"
TASK = "01900000-0000-7000-8000-00000000e020"
AGENT_REF = "r1-proposal-agent"

WRITE_TOOL = "jira.create_issue"


class _Resolver:
    def __init__(self) -> None:
        self._actor = uuid.uuid5(uuid.NAMESPACE_URL, AGENT_REF)

    async def __call__(self, request: object) -> TenantContext:
        return TenantContext(
            tenant_id=uuid.UUID(TENANT),
            actor_id=self._actor,
            actor_kind="user",
            role="support_admin",
        )


def _client():
    import importlib

    main_mod = importlib.import_module("platform_core.main")
    fresh = FastAPI()
    for route in main_mod.app.router.routes:
        fresh.router.routes.append(route)
    fresh.add_middleware(TenantContextMiddleware, resolver=_Resolver())
    return TestClient(fresh, raise_server_exceptions=False)


def _seed(*, with_tool: bool) -> None:
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO tenants (id, slug, name, status) VALUES "
                "(:id, :slug, 'R1 proposal', 'active') ON CONFLICT (slug) DO NOTHING"
            ),
            {"id": TENANT, "slug": SLUG},
        )
        conn.execute(
            text(
                "INSERT INTO conversation_control_leases (id, tenant_id, conversation_ref_id, "
                "owner_type, owner_ref, mode, lease_version, changed_reason, updated_at) "
                "VALUES (:id, :t, :c, 'human', :ref, 'HUMAN_ACTIVE', 3, 'test', 0) "
                "ON CONFLICT (tenant_id, conversation_ref_id) DO UPDATE "
                "SET owner_type='human', owner_ref=:ref, lease_version=3"
            ),
            {
                "id": uuid.uuid4(),
                "t": TENANT,
                "c": CONV,
                "ref": str(uuid.uuid5(uuid.NAMESPACE_URL, AGENT_REF)),
            },
        )
        # Seed the catalog row the gateway resolves, so the test exercises the
        # real lookup rather than a stubbed one.
        from platform_core.tool_gateway.registry import TOOL_CATALOG

        risk, schema, permissions, requires_confirmation = TOOL_CATALOG[WRITE_TOOL]
        conn.execute(
            text(
                "INSERT INTO tool_definitions (id, tenant_id, name, version, risk, "
                "input_schema, output_schema, required_permissions, timeout_ms, "
                "idempotent, requires_confirmation) "
                "VALUES (:id, NULL, :name, 1, :risk, CAST(:schema AS jsonb), '{}'::jsonb, "
                "CAST(:perms AS jsonb), 5000, true, :conf) "
                "ON CONFLICT DO NOTHING"
            ),
            {
                "id": uuid.uuid4(),
                "name": WRITE_TOOL,
                "risk": risk,
                "schema": json.dumps(schema),
                "perms": json.dumps(permissions),
                "conf": requires_confirmation,
            },
        )
        slots = (
            '[{"name": "tool", "origin": "verified_receipt", "confirmed": true, '
            f'"value": "{WRITE_TOOL}"}}, '
            '{"name": "project", "origin": "customer_stated", "confirmed": true, '
            '"value": "PCB"}, '
            '{"name": "summary", "origin": "customer_stated", "confirmed": false, '
            '"value": "板子短路"}]'
        )
        if not with_tool:
            slots = (
                '[{"name": "address", "origin": "customer_stated", "confirmed": false, '
                '"value_withheld": true}]'
            )
        conn.execute(
            text(
                "INSERT INTO conversation_tasks (id, tenant_id, conversation_ref_id, "
                "source_turn_id, task_local_key, sequence, kind, status, version, "
                "action_revision, content_hash, depends_on, slots, missing_slots, "
                "created_at, updated_at) "
                "VALUES (:id, :t, :c, 'turn-1', 'write-1', 1, 'write', 'ready', 1, 1, "
                ":hash, '[]', CAST(:slots AS jsonb), '[]', 0, 0) "
                "ON CONFLICT (tenant_id, conversation_ref_id, source_turn_id, task_local_key) "
                "DO UPDATE SET status='ready', version=1, slots=CAST(:slots AS jsonb), "
                "action_revision=1, proposal_id=NULL"
            ),
            {"id": TASK, "t": TENANT, "c": CONV, "hash": "0" * 64, "slots": slots},
        )
    admin.dispose()


def _clear() -> None:
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        conn.execute(text("DELETE FROM action_confirmations WHERE tenant_id = :t"), {"t": TENANT})
        conn.execute(text("DELETE FROM tool_proposals WHERE tenant_id = :t"), {"t": TENANT})
        conn.execute(
            text("DELETE FROM conversation_task_events WHERE tenant_id = :t"), {"t": TENANT}
        )
        conn.execute(text("DELETE FROM conversation_tasks WHERE tenant_id = :t"), {"t": TENANT})
        conn.execute(
            text("DELETE FROM conversation_control_leases WHERE tenant_id = :t"), {"t": TENANT}
        )
    admin.dispose()


@pytest.fixture(autouse=True)
def _clean():
    _clear()
    yield
    _clear()


def _prepare(key: str = "k1", version: int = 1):
    return _client().post(
        f"/v1/workbench/conversations/{CONV}/tasks/{TASK}/commands",
        headers={"Authorization": "Bearer pt_bootstrap_test", "Idempotency-Key": key},
        json={
            "command": "prepare_proposal",
            "expected_version": version,
            "expected_lease_version": 3,
        },
    )


def _row() -> dict:
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        row = (
            conn.execute(
                text(
                    "SELECT status, action_revision, proposal_id, blocked_reason "
                    "FROM conversation_tasks WHERE id = :i"
                ),
                {"i": TASK},
            )
            .mappings()
            .one()
        )
        proposals = int(
            conn.execute(
                text("SELECT count(*) FROM tool_proposals WHERE tenant_id = :t"), {"t": TENANT}
            ).scalar()
            or 0
        )
    admin.dispose()
    return {
        "status": row["status"],
        "action_revision": row["action_revision"],
        "proposal_id": str(row["proposal_id"]) if row["proposal_id"] else None,
        "blocked_reason": row["blocked_reason"],
        "proposals": proposals,
    }


# --- a real proposal --------------------------------------------------------


def test_preparing_a_proposal_creates_a_tool_proposal() -> None:
    """B1-05's reproduction: the row has to exist."""
    _seed(with_tool=True)
    resp = _prepare()
    assert resp.status_code == 200, resp.text
    row = _row()
    assert row["proposals"] == 1, "no ToolProposal was created"
    assert row["status"] == "awaiting_confirmation"
    # And the task points at it, so an agent has something to open.
    assert row["proposal_id"] is not None


def test_the_proposal_belongs_to_this_tenant() -> None:
    _seed(with_tool=True)
    _prepare()
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        owner = conn.execute(
            text("SELECT tenant_id FROM tool_proposals WHERE tenant_id = :t LIMIT 1"),
            {"t": TENANT},
        ).scalar()
    admin.dispose()
    assert str(owner) == TENANT


def test_the_proposal_carries_the_collected_arguments() -> None:
    """The proposal has to describe the action, or confirming it means
    approving something nobody can see."""
    _seed(with_tool=True)
    _prepare()
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        args = conn.execute(
            text("SELECT sanitized_input FROM tool_proposals WHERE tenant_id = :t LIMIT 1"),
            {"t": TENANT},
        ).scalar()
    admin.dispose()
    assert args, "the proposal has no arguments"
    assert "PCB" in str(args)


# --- no capability means no fake confirmation -------------------------------


def test_a_task_with_no_write_capability_goes_to_needs_human() -> None:
    """R1's address-change case: no connector, so no proposal - and the task
    must not sit in a state advertising one."""
    _seed(with_tool=False)
    resp = _prepare()
    assert resp.status_code == 200, resp.text
    row = _row()
    assert row["status"] == "needs_human"
    assert row["proposals"] == 0
    assert row["blocked_reason"] == "SEMANTIC_NO_WRITE_CAPABILITY"


def test_a_withheld_value_prevents_a_proposal() -> None:
    """A withheld address cannot become a write argument.

    Proposing anyway would send the gateway an empty string where the customer
    gave an address, and the gateway would accept it.
    """
    _seed(with_tool=False)
    _prepare()
    assert _row()["proposals"] == 0


# --- revision binding -------------------------------------------------------


def test_a_second_preparation_bumps_the_revision_and_makes_a_new_proposal() -> None:
    """Changing the action's arguments must invalidate the old confirmation."""
    _seed(with_tool=True)
    first = _prepare(key="k1", version=1)
    assert first.status_code == 200, first.text
    row1 = _row()

    # Return the task to ready, then prepare again: the arguments changed, so
    # the revision must move and the proposal must be a different one.
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        conn.execute(
            text("UPDATE conversation_tasks SET status='ready', version=1 WHERE id = :i"),
            {"i": TASK},
        )
    admin.dispose()

    second = _prepare(key="k2", version=1)
    assert second.status_code == 200, second.text
    row2 = _row()

    assert row2["action_revision"] > row1["action_revision"]
    assert row2["proposals"] == 2, "the second preparation reused the first proposal"
    assert row2["proposal_id"] != row1["proposal_id"]


def test_a_replay_without_a_revision_change_reuses_the_same_proposal() -> None:
    """A double-clicked button at the same revision is one proposal.

    The key is derived from (task, action_revision), and a *completed*
    preparation bumps the revision - which is correct, because after the first
    preparation the task is in `awaiting_confirmation` and the arguments are
    about to be approved. So a genuine double-click has to be modelled as the
    same command arriving twice before the first has moved the task, which is
    what the version check reproduces here: the task is returned to `ready` with
    its revision restored, so the second call carries the same key.
    """
    _seed(with_tool=True)
    _prepare(key="k1", version=1)
    first = _row()

    # Restore the exact pre-preparation state, revision included.
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        conn.execute(
            text(
                "UPDATE conversation_tasks SET status='ready', version=1, action_revision=1 "
                "WHERE id = :i"
            ),
            {"i": TASK},
        )
    admin.dispose()
    _prepare(key="k2", version=1)
    second = _row()

    assert second["proposals"] == 1, "a replay at the same revision created a second row"
    assert second["proposal_id"] == first["proposal_id"]
