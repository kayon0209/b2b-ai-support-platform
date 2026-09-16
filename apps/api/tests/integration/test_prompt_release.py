"""Integration tests: prompt version release workflow (ticket 38).

Covers the state transitions end to end against real Postgres and the real
routers, because the properties that matter here are the ones a unit test
cannot see:

- exactly one version serves traffic at a time (the invariant that makes
  "what is live right now?" answerable);
- a rollback restores a previously active version without re-running the
  gate, because it is the action taken when a live prompt is already
  causing harm;
- the role split holds: reading prompts is an auditor/security_admin
  function, releasing is tenant_owner only;
- a candidate cannot be promoted by a caller that omits evidence, and a
  client cannot downgrade its own P0 regression to non-blocking.
"""

import os
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from platform_core.agent_runtime.prompt_release import (
    CategoryScore,
    EvaluationEvidence,
    Regression,
    ReleaseError,
    create_draft,
    get_active,
    promote,
    rollback,
)
from platform_core.identity.middleware import TenantContextMiddleware
from platform_core.identity.tenant_context import TenantContext

pytestmark = pytest.mark.integration

ADMIN_URL = os.environ.get(
    "APP_ADMIN_DATABASE_URL",
    "postgresql+psycopg://platform:platform@localhost:5435/platform",
)
APP_URL = "postgresql+psycopg://platform_app:platform_app@localhost:5435/platform"

TENANT = "0190d000-0000-7000-8000-0000000000a1"
TENANT_OTHER = "0190d000-0000-7000-8000-0000000000b1"
TEMPLATE = "release_test_template"

_CLEAN = "DELETE FROM prompt_versions WHERE tenant_id IN (:a, :b)"


def _run(coro):
    import asyncio

    return asyncio.run(coro, loop_factory=asyncio.SelectorEventLoop)


class _RoleResolver:
    def __init__(self, tenant_id: str, role: str) -> None:
        self._tenant_id = tenant_id
        self._role = role

    async def __call__(self, request: object) -> TenantContext:
        return TenantContext(
            tenant_id=uuid.UUID(self._tenant_id),
            actor_id=uuid.uuid5(uuid.NAMESPACE_URL, f"actor:{self._tenant_id}-{self._role}"),
            actor_kind="user",
            role=self._role,
        )


def _client(tenant_id: str, role: str) -> TestClient:
    import importlib

    main_mod = importlib.import_module("platform_core.main")
    fresh = FastAPI()
    for route in main_mod.app.router.routes:
        fresh.router.routes.append(route)
    fresh.add_middleware(TenantContextMiddleware, resolver=_RoleResolver(tenant_id, role))
    return TestClient(fresh, raise_server_exceptions=False)


def _headers() -> dict[str, str]:
    return {"Authorization": "Bearer pt_bootstrap_test"}


@pytest.fixture(scope="module", autouse=True)
def seed_tenants():
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        for tid, slug in ((TENANT, "prompt-t1"), (TENANT_OTHER, "prompt-t2")):
            conn.execute(
                text(
                    "INSERT INTO tenants (id, slug, name, status) VALUES "
                    "(:id, :slug, :name, 'active') ON CONFLICT (slug) DO NOTHING"
                ),
                {"id": tid, "slug": slug, "name": slug},
            )
    yield
    with admin.begin() as conn:
        conn.execute(text("DELETE FROM tenants WHERE slug LIKE 'prompt-t%'"))
    admin.dispose()


@pytest.fixture(autouse=True)
def clean_prompts():
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        conn.execute(text(_CLEAN), {"a": TENANT, "b": TENANT_OTHER})
    yield
    with admin.begin() as conn:
        conn.execute(text(_CLEAN), {"a": TENANT, "b": TENANT_OTHER})
    admin.dispose()


async def _in_session(tenant: str, fn):
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from platform_core.db import create_engine as app_engine

    engine = app_engine(APP_URL)
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            await session.execute(
                text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant}
            )
            result = await fn(session)
            await session.commit()
            return result
    finally:
        await engine.dispose()


def _ctx(tenant: str = TENANT, role: str = "tenant_owner") -> TenantContext:
    return TenantContext(
        tenant_id=uuid.UUID(tenant),
        actor_id=uuid.uuid5(uuid.NAMESPACE_URL, f"actor:{tenant}-{role}"),
        actor_kind="user",
        role=role,
    )


def _clean_evidence() -> EvaluationEvidence:
    return EvaluationEvidence(
        eval_run_id="eval-clean",
        scores=[
            CategoryScore("citation", 19, 20),
            CategoryScore("forbidden_claim", 20, 20),
        ],
        regressions=[],
    )


def _make_draft(body: str = "You answer using only the evidence.") -> str:
    async def _fn(session):
        row = await create_draft(
            session, ctx=_ctx(), template_name=TEMPLATE, body=body, notes="test"
        )
        return str(row.id)

    return _run(_in_session(TENANT, _fn))


def _promote(version_id: str, evidence: EvaluationEvidence | None = None) -> None:
    async def _fn(session):
        return await promote(
            session,
            ctx=_ctx(),
            version_id=uuid.UUID(version_id),
            evidence=evidence or _clean_evidence(),
        )

    _run(_in_session(TENANT, _fn))


def _active_version() -> int | None:
    async def _fn(session):
        row = await get_active(session, tenant_id=uuid.UUID(TENANT), template_name=TEMPLATE)
        return row.version if row else None

    return _run(_in_session(TENANT, _fn))


def _count_active() -> int:
    admin = create_engine(ADMIN_URL)
    try:
        with admin.begin() as conn:
            return int(
                conn.execute(
                    text(
                        "SELECT count(*) FROM prompt_versions "
                        "WHERE tenant_id = :t AND template_name = :n AND published = true"
                    ),
                    {"t": TENANT, "n": TEMPLATE},
                ).scalar()
                or 0
            )
    finally:
        admin.dispose()


# --- Lifecycle --------------------------------------------------------------


def test_draft_is_never_active() -> None:
    """Authoring must not affect production."""
    _make_draft()

    assert _count_active() == 0
    assert _active_version() is None


def test_version_numbers_increment_per_template() -> None:
    """Editing is "create the next version"; numbering must be monotonic."""
    first = _make_draft("v1 body")
    second = _make_draft("v2 body")

    async def _fn(session):
        from platform_core.agent_runtime.prompt_release import list_versions

        return [r.version for r in await list_versions(
            session, tenant_id=uuid.UUID(TENANT), template_name=TEMPLATE
        )]

    versions = _run(_in_session(TENANT, _fn))
    assert versions == [2, 1]
    assert first != second


def test_promote_requires_evidence() -> None:
    """The gate is enforced in the service, not only at the HTTP layer."""
    draft = _make_draft()

    async def _fn(session):
        return await promote(
            session, ctx=_ctx(), version_id=uuid.UUID(draft), evidence=None
        )

    with pytest.raises(ReleaseError) as err:
        _run(_in_session(TENANT, _fn))
    assert err.value.code == "EVALUATION_REQUIRED"
    assert _count_active() == 0


def test_promote_activates_and_is_queryable() -> None:
    draft = _make_draft()
    _promote(draft)

    assert _active_version() == 1
    assert _count_active() == 1


def test_promoting_a_second_version_leaves_exactly_one_active() -> None:
    """The single-active invariant, checked at the row level."""
    first = _make_draft("body one")
    second = _make_draft("body two")
    _promote(first)
    _promote(second)

    assert _count_active() == 1
    assert _active_version() == 2


def test_promoting_the_active_version_again_is_refused() -> None:
    """Idempotence is a refusal, not a silent no-op, so callers notice."""
    draft = _make_draft()
    _promote(draft)

    async def _fn(session):
        return await promote(
            session,
            ctx=_ctx(),
            version_id=uuid.UUID(draft),
            evidence=_clean_evidence(),
        )

    with pytest.raises(ReleaseError) as err:
        _run(_in_session(TENANT, _fn))
    assert err.value.code == "ALREADY_ACTIVE"


def test_p0_regression_blocks_promotion() -> None:
    draft = _make_draft()
    bad = EvaluationEvidence(
        eval_run_id="eval-bad",
        scores=[CategoryScore("forbidden_claim", 15, 20)],
        regressions=[
            Regression(
                category="forbidden_claim",
                baseline_rate=1.0,
                candidate_rate=0.75,
                p0=True,
            )
        ],
    )

    async def _fn(session):
        return await promote(
            session, ctx=_ctx(), version_id=uuid.UUID(draft), evidence=bad
        )

    with pytest.raises(ReleaseError) as err:
        _run(_in_session(TENANT, _fn))
    assert err.value.code == "P0_REGRESSION"
    assert _count_active() == 0


# --- Rollback ---------------------------------------------------------------


def test_rollback_restores_the_previous_version() -> None:
    """The core recovery path: bad prompt live -> restore the known good one."""
    first = _make_draft("known good")
    second = _make_draft("suspected bad")
    _promote(first)
    _promote(second)
    assert _active_version() == 2

    async def _fn(session):
        return await rollback(
            session,
            ctx=_ctx(),
            template_name=TEMPLATE,
            to_version_id=uuid.UUID(first),
            reason="answer quality dropped after release",
        )

    _run(_in_session(TENANT, _fn))

    assert _active_version() == 1
    assert _count_active() == 1


def test_rollback_needs_no_fresh_evidence() -> None:
    """Rollback must not be gated.

    Requiring an evaluation pass here would add latency to the one operation
    whose whole purpose is speed when production is already degraded. The
    target is safe precisely because it was active before.
    """
    first = _make_draft("known good")
    second = _make_draft("bad")
    _promote(first)
    _promote(second)

    async def _fn(session):
        # No evidence argument exists on this call path at all.
        return await rollback(
            session,
            ctx=_ctx(),
            template_name=TEMPLATE,
            to_version_id=uuid.UUID(first),
            reason="incident 1234",
        )

    _run(_in_session(TENANT, _fn))
    assert _active_version() == 1


def test_rollback_requires_a_reason() -> None:
    """An unexplained rollback is an unexplained production change."""
    first = _make_draft("a")
    second = _make_draft("b")
    _promote(first)
    _promote(second)

    async def _fn(session):
        return await rollback(
            session,
            ctx=_ctx(),
            template_name=TEMPLATE,
            to_version_id=uuid.UUID(first),
            reason="   ",
        )

    with pytest.raises(ReleaseError) as err:
        _run(_in_session(TENANT, _fn))
    assert err.value.code == "REASON_REQUIRED"


def test_rollback_to_a_different_template_is_refused() -> None:
    """Guards against restoring the wrong prompt under a plausible reason."""
    first = _make_draft("a")
    second = _make_draft("b")
    _promote(first)
    _promote(second)

    async def _fn(session):
        return await rollback(
            session,
            ctx=_ctx(),
            template_name="some_other_template",
            to_version_id=uuid.UUID(first),
            reason="oops",
        )

    with pytest.raises(ReleaseError) as err:
        _run(_in_session(TENANT, _fn))
    assert err.value.code == "TEMPLATE_MISMATCH"


def test_rollback_without_an_active_version_is_refused() -> None:
    draft = _make_draft()

    async def _fn(session):
        return await rollback(
            session,
            ctx=_ctx(),
            template_name=TEMPLATE,
            to_version_id=uuid.UUID(draft),
            reason="nothing live",
        )

    with pytest.raises(ReleaseError) as err:
        _run(_in_session(TENANT, _fn))
    assert err.value.code == "NO_ACTIVE_VERSION"


# --- Tenant isolation -------------------------------------------------------


def test_another_tenants_version_is_not_found() -> None:
    """Absent and not-yours must be indistinguishable, or the error confirms
    that a guessed id exists somewhere."""

    async def _create_other(session):
        row = await create_draft(
            session,
            ctx=_ctx(tenant=TENANT_OTHER),
            template_name=TEMPLATE,
            body="other tenant body",
        )
        return str(row.id)

    other_id = _run(_in_session(TENANT_OTHER, _create_other))

    async def _fn(session):
        return await promote(
            session,
            ctx=_ctx(),
            version_id=uuid.UUID(other_id),
            evidence=_clean_evidence(),
        )

    with pytest.raises(ReleaseError) as err:
        _run(_in_session(TENANT, _fn))
    assert err.value.code == "NOT_FOUND"


def test_active_lookup_is_tenant_scoped() -> None:
    """One tenant's active prompt must not be visible to another."""
    draft = _make_draft()
    _promote(draft)

    async def _other_active(session):
        row = await get_active(
            session, tenant_id=uuid.UUID(TENANT_OTHER), template_name=TEMPLATE
        )
        return row

    assert _run(_in_session(TENANT_OTHER, _other_active)) is None


# --- HTTP surface -----------------------------------------------------------


def _create_via_http(client: TestClient, body: str = "http body") -> dict:
    resp = client.post(
        "/v1/prompts",
        json={"template_name": TEMPLATE, "body": body, "notes": "via http"},
        headers=_headers(),
    )
    return resp.json()


def test_http_list_requires_prompt_read() -> None:
    """A support agent can read cases but not prompt internals."""
    resp = _client(TENANT, "support_agent").get(
        "/v1/prompts", params={"template_name": TEMPLATE}, headers=_headers()
    )
    assert resp.json()["error"]["code"] == "PROMPT_ACCESS_DENIED"


def test_http_release_denied_for_security_admin() -> None:
    """security_admin may read prompts but must not put one live."""
    resp = _client(TENANT, "security_admin").post(
        "/v1/prompts",
        json={"template_name": TEMPLATE, "body": "x", "notes": ""},
        headers=_headers(),
    )
    assert resp.json()["error"]["code"] == "PROMPT_ACCESS_DENIED"


def test_http_draft_then_promote_flow() -> None:
    owner = _client(TENANT, "tenant_owner")
    draft = _create_via_http(owner)
    assert "id" in draft, draft
    assert draft["published"] is False
    assert draft["version"] == 1

    promoted = owner.post(
        f"/v1/prompts/{draft['id']}/promote",
        json={
            "eval_run_id": "eval-http",
            "scores": [{"category": "citation", "passed": 20, "total": 20}],
            "regressions": [],
        },
        headers=_headers(),
    ).json()

    assert promoted["published"] is True
    active = owner.get(
        "/v1/prompts/active", params={"template_name": TEMPLATE}, headers=_headers()
    ).json()
    assert active["active"]["version"] == 1


def test_http_promote_without_body_is_refused() -> None:
    """Omitting evidence must not be read as "no regressions"."""
    owner = _client(TENANT, "tenant_owner")
    draft = _create_via_http(owner)

    resp = owner.post(f"/v1/prompts/{draft['id']}/promote", headers=_headers())

    assert resp.json()["error"]["code"] == "EVALUATION_REQUIRED"


def test_http_client_cannot_downgrade_its_own_p0_regression() -> None:
    """P0 membership is server-owned.

    The wire format carries no `p0` field on purpose: if it did, a client
    could mark a safety regression non-blocking and walk a bad prompt live.
    """
    owner = _client(TENANT, "tenant_owner")
    draft = _create_via_http(owner)

    resp = owner.post(
        f"/v1/prompts/{draft['id']}/promote",
        json={
            "eval_run_id": "eval-bad",
            "scores": [{"category": "forbidden_claim", "passed": 15, "total": 20}],
            "regressions": [
                {
                    "category": "forbidden_claim",
                    "baseline_rate": 1.0,
                    "candidate_rate": 0.75,
                }
            ],
        },
        headers=_headers(),
    )

    assert resp.json()["error"]["code"] == "P0_REGRESSION"


def test_http_malformed_id_is_not_found_not_a_crash() -> None:
    """A bad path id must not surface as a 500."""
    owner = _client(TENANT, "tenant_owner")

    resp = owner.post(
        "/v1/prompts/not-a-uuid/promote",
        json={"eval_run_id": "e", "scores": [], "regressions": []},
        headers=_headers(),
    )

    assert resp.status_code == 200
    assert resp.json()["error"]["code"] == "NOT_FOUND"


def test_http_rollback_creates_an_audit_event() -> None:
    """Every release decision must be reconstructable after the fact."""
    owner = _client(TENANT, "tenant_owner")
    first = _create_via_http(owner, "one")
    owner.post(
        f"/v1/prompts/{first['id']}/promote",
        json={"eval_run_id": "e1", "scores": [], "regressions": []},
        headers=_headers(),
    )
    second = _create_via_http(owner, "two")
    owner.post(
        f"/v1/prompts/{second['id']}/promote",
        json={"eval_run_id": "e2", "scores": [], "regressions": []},
        headers=_headers(),
    )
    owner.post(
        "/v1/prompts/rollback",
        json={
            "template_name": TEMPLATE,
            "to_version_id": first["id"],
            "reason": "quality regression in prod",
        },
        headers=_headers(),
    )

    admin = create_engine(ADMIN_URL)
    try:
        with admin.begin() as conn:
            actions = [
                r[0]
                for r in conn.execute(
                    text(
                        "SELECT action FROM audit_events WHERE tenant_id = :t "
                        "AND action LIKE 'prompt.%' ORDER BY action"
                    ),
                    {"t": TENANT},
                ).fetchall()
            ]
    finally:
        admin.dispose()

    assert "prompt.rolled_back" in actions
    assert "prompt.promoted" in actions
    assert "prompt.archived" in actions
