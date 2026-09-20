"""Integration: the leak analysis is populated by real runs, over HTTP.

The unit tests set `handoff_reason_counts` by hand, which proves the
classifier sorts it correctly and nothing more. The defect this repo keeps
finding is a field that is correct in isolation and never written in
production - so this file drives actual `agent_runs` rows through the
aggregator and reads them back off the endpoint.

The behaviour that matters most is the exclusion: a clarification is not a
handoff. Counting it would inflate the number someone is about to act on with
the exact traffic the platform *did* handle.
"""

from __future__ import annotations

import os
import time
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

TENANT = "01900000-0000-7000-8000-0000000000cc"
SLUG = "leak-analysis"


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


def _client(role: str = "tenant_owner") -> TestClient:
    import importlib

    main_mod = importlib.import_module("platform_core.main")
    fresh = FastAPI()
    for route in main_mod.app.router.routes:
        fresh.router.routes.append(route)
    fresh.add_middleware(TenantContextMiddleware, resolver=_RoleResolver(TENANT, role))
    return TestClient(fresh, raise_server_exceptions=False)


def _seed() -> None:
    now = int(time.time())
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO tenants (id, slug, name, status) VALUES "
                "(:id, :slug, :slug, 'active') ON CONFLICT (slug) DO NOTHING"
            ),
            {"id": TENANT, "slug": SLUG},
        )
        runs = [
            # Three evidence gaps - automatable.
            ("NO_AUTHORIZED_EVIDENCE", "abstained"),
            ("NO_AUTHORIZED_EVIDENCE", "abstained"),
            ("CONFLICTING_SOURCES", "abstained"),
            # Two red lines - must never be recommended for automation.
            ("COMPLAINT_REQUIRES_HUMAN", "abstained"),
            ("REDLINE_COMMERCIAL_COMMITMENT", "abstained"),
            # A clarification: the run kept the conversation. Not a leak.
            ("NEEDS_CLARIFICATION", "abstained"),
        ]
        for reason, status in runs:
            conn.execute(
                text(
                    "INSERT INTO agent_runs (id, tenant_id, conversation_ref_id, route, "
                    "status, abstain_reason, started_at, input_hash, code_version) VALUES "
                    "(gen_random_uuid(), :t, :conv, 'knowledge_qa', :s, :r, :ts, '', 'test')"
                ),
                {
                    "t": TENANT,
                    "conv": str(uuid.uuid4()),
                    "s": status,
                    "r": reason,
                    "ts": now,
                },
            )
    admin.dispose()


def _clear() -> None:
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        # knowledge_gaps before the tenant, and before any re-seed: the gap
        # table is unique per (tenant, question_hash), so a row left behind
        # turns the next run into a UniqueViolation instead of a test result.
        conn.execute(text("DELETE FROM knowledge_gaps WHERE tenant_id = :t"), {"t": TENANT})
        conn.execute(text("DELETE FROM agent_runs WHERE tenant_id = :t"), {"t": TENANT})
        conn.execute(text("DELETE FROM tenants WHERE slug = :slug"), {"slug": SLUG})
    admin.dispose()


@pytest.fixture(autouse=True)
def clean() -> None:
    _clear()
    _seed()
    yield
    _clear()


def test_handoffs_are_counted_by_reason_from_real_runs() -> None:
    resp = _client().get(
        "/v1/quality/metrics?window_seconds=3600",
        headers={"Authorization": "Bearer pt_bootstrap_test"},
    )

    assert resp.status_code == 200, resp.text[:300]
    counts = resp.json()["handoff_reason_counts"]

    assert counts.get("NO_AUTHORIZED_EVIDENCE") == 2
    assert counts.get("CONFLICTING_SOURCES") == 1
    assert counts.get("COMPLAINT_REQUIRES_HUMAN") == 1


def test_a_clarification_is_not_counted_as_a_handoff() -> None:
    """Mutation guard: without the exclusion this is 6 instead of 5.

    The clarification run asked the customer a question and kept the
    conversation. If it is counted, the leak queue tells ops to automate
    traffic the platform already handled.
    """
    resp = _client().get(
        "/v1/quality/metrics?window_seconds=3600",
        headers={"Authorization": "Bearer pt_bootstrap_test"},
    )

    counts = resp.json()["handoff_reason_counts"]
    assert "NEEDS_CLARIFICATION" not in counts


def test_the_candidate_queue_separates_evidence_gaps_from_red_lines() -> None:
    resp = _client().get(
        "/v1/quality/metrics?window_seconds=3600",
        headers={"Authorization": "Bearer pt_bootstrap_test"},
    )

    items = {item["reason"]: item for item in resp.json()["automation_candidates"]}

    assert items["NO_AUTHORIZED_EVIDENCE"]["automatable"] is True
    assert items["COMPLAINT_REQUIRES_HUMAN"]["automatable"] is False
    assert items["REDLINE_COMMERCIAL_COMMITMENT"]["automatable"] is False
    # Ranked by volume: the biggest evidence gap first.
    assert [item["reason"] for item in resp.json()["automation_candidates"]][0] == (
        "NO_AUTHORIZED_EVIDENCE"
    )


def test_a_candidate_names_the_questions_behind_it() -> None:
    """A leak analysis you cannot act on is a histogram with opinions.

    "NO_AUTHORIZED_EVIDENCE x 2" tells ops how big the gap is, not what it is,
    so the candidate carries the questions customers actually asked - the gap
    queue already has them.
    """
    import time as _time

    import sqlalchemy as sa
    from sqlalchemy import create_engine as sa_engine

    now = int(_time.time())
    admin = sa_engine(ADMIN_URL)
    with admin.begin() as conn:
        for index, question in enumerate(("最小线宽能做多少？", "阻抗公差是多少？")):
            conn.execute(
                sa.text(
                    "INSERT INTO knowledge_gaps (id, tenant_id, question_hash, "
                    "sample_question, reason_code, status, frequency, first_seen_at, "
                    "last_seen_at) VALUES (gen_random_uuid(), :t, :h, :q, "
                    "'NO_AUTHORIZED_EVIDENCE', 'open', :f, :n, :n) "
                    "ON CONFLICT DO NOTHING"
                ),
                {"t": TENANT, "h": f"h-iso-{index}", "q": question, "f": 10 - index, "n": now},
            )
    admin.dispose()

    resp = _client().get(
        "/v1/quality/metrics?window_seconds=3600",
        headers={"Authorization": "Bearer pt_bootstrap_test"},
    )

    items = {item["reason"]: item for item in resp.json()["automation_candidates"]}
    gap = items["NO_AUTHORIZED_EVIDENCE"]

    # Most-frequent first: answer the question customers ask most.
    assert gap["sample_questions"][0] == "最小线宽能做多少？"
    # A complaint is a policy decision, not a knowledge gap - the queue never
    # holds questions for it, and inventing "documentation" for one would be
    # the wrong fix for a control that is working.
    assert items["COMPLAINT_REQUIRES_HUMAN"]["sample_questions"] == []
