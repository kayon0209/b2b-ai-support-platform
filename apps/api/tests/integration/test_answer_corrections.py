"""Corrections over HTTP (feature list 7.8).

The loop this closes: an agent sees a wrong answer, records what it should have
said, and that record is reviewable instead of living in a chat message. The
interesting assertions are the boundaries - who may record, who may approve,
and whose corrections are visible.
"""

from __future__ import annotations

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

TENANT = "01900000-0000-7000-8000-0000000000a5"
OTHER = "01900000-0000-7000-8000-0000000000a6"
SLUG = "corrections"
OTHER_SLUG = "corrections-other"

RUN_ID = "01900000-0000-7000-8000-0000000000a7"


class _RoleResolver:
    def __init__(self, tenant_id: str, role: str) -> None:
        self._tenant_id = tenant_id
        self._role = role

    async def __call__(self, request: object) -> TenantContext:
        return TenantContext(
            tenant_id=uuid.UUID(self._tenant_id),
            actor_id=uuid.uuid4(),
            actor_kind="user",
            role=self._role,
        )


def _client(tenant: str = TENANT, role: str = "support_agent") -> TestClient:
    import importlib

    main_mod = importlib.import_module("platform_core.main")
    fresh = FastAPI()
    for route in main_mod.app.router.routes:
        fresh.router.routes.append(route)
    fresh.add_middleware(TenantContextMiddleware, resolver=_RoleResolver(tenant, role))
    return TestClient(fresh, raise_server_exceptions=False)


def _headers() -> dict[str, str]:
    return {"Authorization": "Bearer pt_bootstrap_test"}


def _seed() -> None:
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        for tid, slug in ((TENANT, SLUG), (OTHER, OTHER_SLUG)):
            conn.execute(
                text(
                    "INSERT INTO tenants (id, slug, name, status) VALUES "
                    "(:id, :slug, :slug, 'active') ON CONFLICT (slug) DO NOTHING"
                ),
                {"id": tid, "slug": slug},
            )
    admin.dispose()


def _clear() -> None:
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        for tenant in (TENANT, OTHER):
            # Drafts and gaps before the corrections that create them: a draft
            # left behind would satisfy "a draft exists" on a later run even if
            # the wiring had stopped working - the same leftover-row trap that
            # has already produced two false results tonight.
            conn.execute(text("DELETE FROM knowledge_drafts WHERE tenant_id = :t"), {"t": tenant})
            conn.execute(text("DELETE FROM knowledge_gaps WHERE tenant_id = :t"), {"t": tenant})
            conn.execute(text("DELETE FROM answer_corrections WHERE tenant_id = :t"), {"t": tenant})
        for slug in (SLUG, OTHER_SLUG):
            conn.execute(text("DELETE FROM tenants WHERE slug = :slug"), {"slug": slug})
    admin.dispose()


@pytest.fixture(autouse=True)
def clean() -> None:
    _clear()
    _seed()
    yield
    _clear()


def _record(client: TestClient, *, question: str = "标准交期是几天？") -> object:
    return client.post(
        "/v1/corrections",
        json={
            "agent_run_id": RUN_ID,
            "question": question,
            "correct_answer": "标准交期 7 天，加急 3 天，以报价单为准。",
            "note": "AI 说了 5 天，实际是 7 天。",
        },
        headers=_headers(),
    )


def test_an_agent_can_record_a_correction() -> None:
    """Recording is gated on CASE_UPDATE, not on admin: the people who see
    bad answers are the agents, and gating this behind an admin action would
    leave the correction in a chat message where it is lost."""
    resp = _record(_client())

    assert resp.status_code == 200, resp.text[:300]
    body = resp.json()
    assert body["status"] == "pending"
    assert body["correct_answer"].startswith("标准交期 7 天")
    assert body["agent_run_id"] == RUN_ID


def test_another_tenants_corrections_are_invisible() -> None:
    _record(_client(TENANT))
    assert _record(_client(OTHER), question="别家的问题？").status_code == 200

    listed = _client(OTHER).get("/v1/corrections", headers=_headers()).json()

    assert [item["question"] for item in listed["items"]] == ["别家的问题？"]


def test_approving_needs_the_publish_permission() -> None:
    """One person's correction is not two people's agreement.

    Approving says "this may become what the platform tells customers", which
    is the blast radius of publishing - so an agent who can record a correction
    cannot also approve one.
    """
    created = _record(_client()).json()

    denied = _client(TENANT, "support_agent").post(
        f"/v1/corrections/{created['id']}/review",
        json={"approve": True},
        headers=_headers(),
    )
    assert denied.status_code == 403

    allowed = _client(TENANT, "tenant_owner").post(
        f"/v1/corrections/{created['id']}/review",
        json={"approve": True},
        headers=_headers(),
    )
    assert allowed.status_code == 200, allowed.text[:300]
    assert allowed.json()["status"] == "approved"
    # It is an instruction to write knowledge, not knowledge: publishing stays
    # its own reviewed act.
    assert "publish" in allowed.json()["next"]


def test_a_correction_cannot_be_reviewed_twice() -> None:
    created = _record(_client()).json()
    owner = _client(TENANT, "tenant_owner")

    first = owner.post(
        f"/v1/corrections/{created['id']}/review",
        json={"approve": False},
        headers=_headers(),
    )
    assert first.status_code == 200
    assert first.json()["status"] == "dismissed"

    second = owner.post(
        f"/v1/corrections/{created['id']}/review",
        json={"approve": True},
        headers=_headers(),
    )
    assert second.status_code == 409


def test_pending_corrections_are_visible_to_operations() -> None:
    """7.8, surfaced where the decision gets made.

    A correction nobody can see never becomes an answer. The count rides on
    the same payload as the leak analysis - they are the two halves of "what
    should we fix next".
    """
    metrics = _client(TENANT, "tenant_owner").get(
        "/v1/quality/metrics?window_seconds=3600", headers=_headers()
    )
    assert metrics.status_code == 200, metrics.text[:200]
    assert metrics.json()["pending_corrections"] == 0

    assert _record(_client()).status_code == 200

    after = (
        _client(TENANT, "tenant_owner")
        .get("/v1/quality/metrics?window_seconds=3600", headers=_headers())
        .json()
    )
    assert after["pending_corrections"] == 1

    # Another tenant's correction must not move this tenant's number.
    assert _record(_client(OTHER), question="别家修正？").status_code == 200
    still = (
        _client(TENANT, "tenant_owner")
        .get("/v1/quality/metrics?window_seconds=3600", headers=_headers())
        .json()
    )
    assert still["pending_corrections"] == 1


def test_approving_a_correction_drafts_it_for_the_knowledge_base() -> None:
    """7.8, the last link: an approved correction reaches the publish queue.

    Approving must not depend on someone remembering to retype the answer. It
    lands as a draft - not as knowledge - because approving checked that the
    correction is right, not that it reads well as documentation.
    """
    from platform_core.knowledge.gap_models import KnowledgeDraft

    created = _record(_client()).json()
    approved = _client(TENANT, "tenant_owner").post(
        f"/v1/corrections/{created['id']}/review",
        json={"approve": True},
        headers=_headers(),
    )
    assert approved.status_code == 200, approved.text[:200]

    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        rows = conn.execute(
            text("SELECT body, status FROM knowledge_drafts WHERE tenant_id = :t"),
            {"t": TENANT},
        ).all()
    admin.dispose()

    assert rows, "the approved correction never reached the draft queue"
    bodies = " ".join(str(r[0]) for r in rows)
    assert "标准交期 7 天" in bodies
    # It is a draft awaiting review, not published knowledge.
    assert KnowledgeDraft is not None
    assert any(str(r[1]).lower() in ("draft", "pending", "awaiting_review") for r in rows), rows
