"""B1-03: a queued copilot job must exist, and be readable by its job id.

The acceptance review posted a job, got 200 with `queued`, counted zero rows in
the database, and the GET that followed returned 404. It also noted that the
GET looked the row up by primary key while the URL carries the `job_id` column -
so adding the insert alone would not have fixed it.

These tests assert:

1. POST writes a `copilot_drafts` row before answering, and GET by `job_id`
   finds it.
2. A source turn that does not belong to this conversation is refused.
3. A replayed request returns the same job rather than a second one.
4. A queued job that has aged out reads as `expired`, not `queued`.
5. The consumer writes a body and never sends: a test parses its AST for an
   outbound identifier.
"""

from __future__ import annotations

import ast
import json
import os
import pathlib
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

TENANT = "01900000-0000-7000-8000-00000000f001"
SLUG = "r1-copilot-jobs"
CONV = "01900000-0000-7000-8000-00000000f010"
TURN = "01900000-0000-7000-8000-00000000f030"
AGENT_REF = "r1-copilot-agent"


class _Resolver:
    def __init__(self) -> None:
        self._actor = uuid.uuid5(uuid.NAMESPACE_URL, AGENT_REF)

    async def __call__(self, request: object) -> TenantContext:
        return TenantContext(
            tenant_id=uuid.UUID(TENANT),
            actor_id=self._actor,
            actor_kind="user",
            role="support_agent",
        )


def _client():
    import importlib

    main_mod = importlib.import_module("platform_core.main")
    fresh = FastAPI()
    for route in main_mod.app.router.routes:
        fresh.router.routes.append(route)
    fresh.add_middleware(TenantContextMiddleware, resolver=_Resolver())
    return TestClient(fresh, raise_server_exceptions=False)


def _seed() -> None:
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO tenants (id, slug, name, status) VALUES "
                "(:id, :slug, 'R1 copilot', 'active') ON CONFLICT (slug) DO NOTHING"
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
        # One customer turn, so `timeline_revision` is 1 and a source resolves.
        conn.execute(
            text(
                "INSERT INTO conversation_turns (id, tenant_id, conversation_ref_id, role, "
                "text_redacted, text_hash, ts, ref, created_at) "
                "VALUES (:id, :t, :c, 'customer', 'SO-240918 redacted', "
                ":hash, 1, 'r1', 1) "
                "ON CONFLICT (id) DO NOTHING"
            ),
            {
                "id": TURN,
                "t": TENANT,
                "c": CONV,
                "hash": "a" * 64,
            },
        )
    admin.dispose()


def _clear() -> None:
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        conn.execute(text("DELETE FROM outbox_events WHERE tenant_id = :t"), {"t": TENANT})
        conn.execute(text("DELETE FROM copilot_drafts WHERE tenant_id = :t"), {"t": TENANT})
        conn.execute(text("DELETE FROM conversation_turns WHERE tenant_id = :t"), {"t": TENANT})
        conn.execute(
            text("DELETE FROM conversation_control_leases WHERE tenant_id = :t"), {"t": TENANT}
        )
    admin.dispose()


@pytest.fixture(autouse=True)
def _clean():
    _clear()
    _seed()
    yield
    _clear()


def _post(**overrides):
    payload: dict[str, object] = {
        "kind": "summary",
        "timeline_revision": 1,
        "lease_version": 3,
        "source_turn_ids": [TURN],
    }
    payload.update(overrides)
    return _client().post(
        f"/v1/workbench/conversations/{CONV}/copilot/jobs",
        headers={"Authorization": "Bearer pt_bootstrap_test", "Idempotency-Key": "k1"},
        json=payload,
    )


def _row_count() -> int:
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        n = conn.execute(
            text("SELECT count(*) FROM copilot_drafts WHERE tenant_id = :t"), {"t": TENANT}
        ).scalar()
    admin.dispose()
    return int(n or 0)


# --- the row exists ---------------------------------------------------------


def test_posting_a_job_writes_a_row() -> None:
    """B1-03: the POST answered `queued` with nothing behind it."""
    resp = _post()
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "queued"
    assert _row_count() == 1, "no copilot_drafts row was written"


def test_the_job_is_readable_by_its_job_id() -> None:
    """The second half of B1-03: the GET looked up by primary key."""
    created = _post()
    job_id = created.json()["job_id"]
    resp = _client().get(f"/v1/workbench/conversations/{CONV}/copilot/jobs/{job_id}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["job_id"] == job_id
    assert body["status"] == "queued"
    # Nothing generated yet, and nothing claims otherwise.
    assert body["body"] == ""
    assert body["can_insert"] is False


def test_posting_enqueues_the_generation() -> None:
    """A job that exists with nothing able to consume it is the same silent
    no-op B1-01 was."""
    _post()
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        n = conn.execute(
            text(
                "SELECT count(*) FROM outbox_events WHERE tenant_id = :t "
                "AND event_type = 'copilot.generate_requested'"
            ),
            {"t": TENANT},
        ).scalar()
    admin.dispose()
    assert int(n or 0) == 1, "no generation event was enqueued"


# --- sources ----------------------------------------------------------------


def test_a_source_that_is_not_in_the_conversation_is_refused() -> None:
    """A summary's sources are what make it checkable, so a source that does
    not resolve is refused rather than recorded."""
    resp = _post(source_turn_ids=["01900000-0000-7000-8000-00000000f099"])
    assert resp.status_code == 404, resp.text
    assert resp.json()["error"]["code"] == "COPILOT_SOURCE_NOT_FOUND"
    assert _row_count() == 0


def test_a_summary_with_no_sources_is_refused() -> None:
    resp = _post(source_turn_ids=[])
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == "COPILOT_SUMMARY_REQUIRES_SOURCES"
    assert _row_count() == 0


def test_the_recorded_sources_name_real_turns() -> None:
    _post()
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        raw = conn.execute(
            text("SELECT source_refs FROM copilot_drafts WHERE tenant_id = :t"), {"t": TENANT}
        ).scalar()
    admin.dispose()
    refs = json.loads(raw) if isinstance(raw, str) else raw
    assert [r["turn_id"] for r in refs] == [TURN]


# --- idempotency ------------------------------------------------------------


def test_a_replayed_request_returns_the_same_job() -> None:
    first = _post()
    second = _client().post(
        f"/v1/workbench/conversations/{CONV}/copilot/jobs",
        headers={"Authorization": "Bearer pt_bootstrap_test", "Idempotency-Key": "k2"},
        json={
            "kind": "summary",
            "timeline_revision": 1,
            "lease_version": 3,
            "source_turn_ids": [TURN],
        },
    )
    assert second.status_code == 200, second.text
    assert first.json()["job_id"] == second.json()["job_id"]
    assert _row_count() == 1, "a replayed request created a second job"


# --- expiry -----------------------------------------------------------------


def test_an_aged_out_job_reads_as_expired() -> None:
    """Reported on read, because the operator is the one watching the spinner."""
    _post()
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        conn.execute(
            text("UPDATE copilot_drafts SET created_at = 1 WHERE tenant_id = :t"), {"t": TENANT}
        )
        job_id = conn.execute(
            text("SELECT job_id FROM copilot_drafts WHERE tenant_id = :t"), {"t": TENANT}
        ).scalar()
    admin.dispose()

    resp = _client().get(f"/v1/workbench/conversations/{CONV}/copilot/jobs/{job_id}")
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "expired"
    assert resp.json()["error_code"] == "COPILOT_JOB_EXPIRED"


# --- the consumer never sends ----------------------------------------------


def test_the_copilot_consumer_cannot_send() -> None:
    """Structural, and the reason it is worth stating.

    There is no outbound channel, no sender and no dispatch in the consumer. A
    generation that could reach a customer by itself would make "the AI drafted
    this" indistinguishable from "the AI said this", which is the boundary
    COP-01 draws.
    """
    source = pathlib.Path("apps/worker/src/worker/copilot_consumer.py").read_text()
    tree = ast.parse(source)
    referenced: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            referenced.add(node.id)
        elif isinstance(node, ast.Attribute):
            referenced.add(node.attr)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                referenced.add(alias.name.split(".")[0])
    forbidden = {"outbound", "send_message", "deliver", "channel_sender", "dispatch", "sender"}
    assert not (referenced & forbidden), f"copilot_consumer references {referenced & forbidden}"


def test_the_consumer_takes_the_chat_provider_not_the_generator() -> None:
    """Same rule as the shadow consumer, and the same bug it came from."""
    from worker.copilot_consumer import chat_provider as copilot_provider
    from worker.shadow_consumer import chat_provider as shadow_provider

    class _Chat:
        async def complete(self, *a: object, **k: object) -> object: ...

    class _Generator:
        async def generate(self, *a: object, **k: object) -> str:
            return "x"

    class _Deps:
        def __init__(self, **extra: object) -> None:
            self.extra = extra

    chat = _Chat()
    for pick in (copilot_provider, shadow_provider):
        assert pick(_Deps(chat=chat)) is chat
        assert pick(_Deps(chat=_Generator())) is None
        assert pick(_Deps()) is None
