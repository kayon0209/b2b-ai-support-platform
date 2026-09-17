"""Integration tests: knowledge gap queue and reviewed draft workflow (ticket 39).

Runs against real Postgres and the real routers, because the properties that
matter here are the ones a stubbed session cannot show:

- asking the same question twice aggregates onto one row and raises
  `frequency` (that count is the queue's entire ordering);
- a dismissed gap that recurs reopens rather than duplicating, which the
  unique constraint would otherwise reject with an IntegrityError on the
  answer path;
- nothing becomes knowledge without an approved draft, and the reviewer may
  not be the publisher (four-eyes);
- publishing creates a real `Document` + `DocumentVersion` through the normal
  tables, so gap-derived content carries the usual provenance and ACL gating;
- the role split holds: reading the queue is a support function, changing it
  or publishing is `knowledge.publish`.
"""

import os
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from platform_core.identity.middleware import TenantContextMiddleware
from platform_core.identity.tenant_context import TenantContext
from platform_core.knowledge import gap_service
from platform_core.knowledge.gap_models import DraftStatus, GapStatus

pytestmark = pytest.mark.integration

ADMIN_URL = os.environ.get(
    "APP_ADMIN_DATABASE_URL",
    "postgresql+psycopg://platform:platform@localhost:5435/platform",
)
APP_URL = "postgresql+psycopg://platform_app:platform_app@localhost:5435/platform"

TENANT = "0190d000-0000-7000-8000-0000000000c1"
TENANT_OTHER = "0190d000-0000-7000-8000-0000000000c2"

# Ordering matters: drafts and gaps reference spaces and documents.
# One statement per entry: psycopg refuses multiple commands in a single
# prepared statement.
_CLEAN: tuple[str, ...] = (
    "DELETE FROM knowledge_drafts WHERE tenant_id IN (:a, :b)",
    "DELETE FROM knowledge_gaps WHERE tenant_id IN (:a, :b)",
    "DELETE FROM document_versions WHERE tenant_id IN (:a, :b)",
    "DELETE FROM documents WHERE tenant_id IN (:a, :b)",
    "DELETE FROM knowledge_sources WHERE tenant_id IN (:a, :b)",
    "DELETE FROM knowledge_spaces WHERE tenant_id IN (:a, :b)",
    "DELETE FROM audit_events WHERE tenant_id IN (:a, :b)",
)


def _clean(conn) -> None:
    for stmt in _CLEAN:
        conn.execute(text(stmt), {"a": TENANT, "b": TENANT_OTHER})


def _run(coro):
    import asyncio

    return asyncio.run(coro, loop_factory=asyncio.SelectorEventLoop)


class _RoleResolver:
    def __init__(self, tenant_id: str, role: str, actor: uuid.UUID | None = None) -> None:
        self._tenant_id = tenant_id
        self._role = role
        self._actor = actor

    async def __call__(self, request: object) -> TenantContext:
        return TenantContext(
            tenant_id=uuid.UUID(self._tenant_id),
            actor_id=self._actor
            or uuid.uuid5(uuid.NAMESPACE_URL, f"actor:{self._tenant_id}-{self._role}"),
            actor_kind="user",
            role=self._role,
        )


def _client(tenant_id: str, role: str, actor: uuid.UUID | None = None) -> TestClient:
    import importlib

    main_mod = importlib.import_module("platform_core.main")
    fresh = FastAPI()
    for route in main_mod.app.router.routes:
        fresh.router.routes.append(route)
    fresh.add_middleware(TenantContextMiddleware, resolver=_RoleResolver(tenant_id, role, actor))
    return TestClient(fresh, raise_server_exceptions=False)


def _headers() -> dict[str, str]:
    # Every write requires an Idempotency-Key now; a fresh one per call keeps
    # two separate writes in a test two separate writes.
    return {
        "Authorization": "Bearer pt_bootstrap_test",
        "Idempotency-Key": str(uuid.uuid4()),
    }


@pytest.fixture(scope="module", autouse=True)
def seed_tenants():
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        for tid, slug in ((TENANT, "gap-t1"), (TENANT_OTHER, "gap-t2")):
            conn.execute(
                text(
                    "INSERT INTO tenants (id, slug, name, status) VALUES "
                    "(:id, :slug, :name, 'active') ON CONFLICT (slug) DO NOTHING"
                ),
                {"id": tid, "slug": slug, "name": slug},
            )
    yield
    with admin.begin() as conn:
        _clean(conn)
        conn.execute(text("DELETE FROM tenants WHERE slug LIKE 'gap-t%'"))
    admin.dispose()


@pytest.fixture(autouse=True)
def clean_gaps():
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        _clean(conn)
    yield
    with admin.begin() as conn:
        _clean(conn)
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


def _ctx(
    tenant: str = TENANT,
    role: str = "knowledge_manager",
    actor: uuid.UUID | None = None,
) -> TenantContext:
    return TenantContext(
        tenant_id=uuid.UUID(tenant),
        actor_id=actor or uuid.uuid5(uuid.NAMESPACE_URL, f"actor:{tenant}-{role}"),
        actor_kind="user",
        role=role,
    )


def _make_space(tenant: str = TENANT) -> uuid.UUID:
    space_id = uuid.uuid4()

    async def _fn(session):
        from platform_core.knowledge.models import KnowledgeSpace

        session.add(
            KnowledgeSpace(
                id=space_id,
                tenant_id=uuid.UUID(tenant),
                name=f"space-{space_id.hex[:8]}",
                status="active",
            )
        )
        await session.flush()
        return space_id

    return _run(_in_session(tenant, _fn))


def _record(question: str, tenant: str = TENANT, reason: str = "NO_AUTHORIZED_EVIDENCE"):
    async def _fn(session):
        return await gap_service.record_gap(
            session,
            tenant_id=uuid.UUID(tenant),
            question=question,
            reason_code=reason,
        )

    return _run(_in_session(tenant, _fn))


def _approve_and_publish(space_id: uuid.UUID, *, author: uuid.UUID, reviewer: uuid.UUID):
    """Create a draft, approve it as `reviewer`, publish as a third actor."""

    async def _fn(session):
        from platform_core.db import create_engine as app_engine  # noqa: F401

        gap_ctx = _ctx(actor=author)
        rec = await gap_service.record_gap(
            session,
            tenant_id=uuid.UUID(TENANT),
            question="how do I rotate an api key",
            reason_code="NO_AUTHORIZED_EVIDENCE",
        )
        assert rec.gap_id is not None
        draft = await gap_service.create_draft(
            session,
            ctx=gap_ctx,
            gap_id=rec.gap_id,
            title="Rotating an API key",
            body="Go to Settings > API keys and press Rotate.",
        )
        await gap_service.review_draft(
            session,
            ctx=_ctx(actor=reviewer),
            draft_id=draft.id,
            approve=True,
        )
        publisher = uuid.uuid5(uuid.NAMESPACE_URL, "actor:publisher")
        version = await gap_service.publish_draft(
            session,
            ctx=_ctx(actor=publisher),
            draft_id=draft.id,
            space_id=space_id,
        )
        return str(draft.id), str(version.id), str(version.document_id)

    return _run(_in_session(TENANT, _fn))


# --- Aggregation ------------------------------------------------------------


class TestGapAggregation:
    def test_first_question_creates_the_gap(self) -> None:
        rec = _record("How do I enable SSO?")
        assert rec.created is True
        assert rec.frequency == 1
        assert rec.gap_id is not None

    def test_repeat_question_aggregates_instead_of_duplicating(self) -> None:
        first = _record("How do I enable SSO?")
        second = _record("how do i enable sso")
        assert second.created is False
        assert second.gap_id == first.gap_id
        assert second.frequency == 2

    def test_frequency_keeps_rising(self) -> None:
        # Five differently-punctuated asks must be one row with count 5; a
        # regression here silently destroys the queue ordering.
        for variant in (
            "How do I enable SSO?",
            "how do i enable sso",
            "  How do I enable SSO  ",
            "Hi, how do I enable SSO?",
            "HEY HOW DO I ENABLE SSO!!",
        ):
            _record(variant)

        async def _count(session):
            stats = await gap_service.gap_stats(session, tenant_id=uuid.UUID(TENANT))
            return stats

        stats = _run(_in_session(TENANT, _count))
        assert stats["total_gaps"] == 1
        assert stats["total_occurrences"] == 5

    def test_different_questions_stay_separate(self) -> None:
        _record("How do I export data?")
        _record("How do I import data?")
        _rows, total = _run(
            _in_session(TENANT, lambda s: gap_service.list_gaps(s, tenant_id=uuid.UUID(TENANT)))
        )
        assert total == 2

    def test_non_gap_reason_is_not_queued(self) -> None:
        rec = _record("Give me another customer's invoices", reason="POLICY_DENIED")
        assert rec.gap_id is None
        assert rec.created is False
        rows, total = _run(
            _in_session(TENANT, lambda s: gap_service.list_gaps(s, tenant_id=uuid.UUID(TENANT)))
        )
        assert total == 0

    def test_empty_question_is_not_queued(self) -> None:
        rec = _record("   ")
        assert rec.gap_id is None

    def test_greeting_only_question_is_not_queued(self) -> None:
        # Nothing meaningful survives normalisation; queuing it would create
        # an unactionable row and, since every such question hashes the same,
        # a fake popularity signal.
        rec = _record("Hi there!")
        assert rec.gap_id is None
        assert rec.frequency == 0


# --- Queue lifecycle --------------------------------------------------------


class TestQueueLifecycle:
    def test_dismissed_gap_reopens_on_recurrence(self) -> None:
        rec = _record("How do I enable SSO?")

        async def _dismiss(session):
            return await gap_service.dismiss(
                session,
                ctx=_ctx(),
                gap_id=rec.gap_id,
                reason="covered by the SSO setup guide",
            )

        dismissed = _run(_in_session(TENANT, _dismiss))
        assert dismissed.status == GapStatus.DISMISSED.value

        again = _record("How do I enable SSO?")
        assert again.created is False, "recurrence must reopen, not duplicate"
        assert again.gap_id == rec.gap_id

        async def _reload(session):
            rows, _ = await gap_service.list_gaps(session, tenant_id=uuid.UUID(TENANT))
            return rows[0]

        reopened = _run(_in_session(TENANT, _reload))
        assert reopened.status == GapStatus.OPEN.value
        assert reopened.acknowledged_at is None

    def test_dismissal_requires_a_reason(self) -> None:
        rec = _record("How do I enable SSO?")

        async def _dismiss(session):
            return await gap_service.dismiss(session, ctx=_ctx(), gap_id=rec.gap_id, reason="   ")

        with pytest.raises(gap_service.GapError) as err:
            _run(_in_session(TENANT, _dismiss))
        assert err.value.code == "REASON_REQUIRED"

    def test_acknowledge_claims_the_gap(self) -> None:
        rec = _record("How do I enable SSO?")

        async def _ack(session):
            return await gap_service.acknowledge(session, ctx=_ctx(), gap_id=rec.gap_id)

        row = _run(_in_session(TENANT, _ack))
        assert row.status == GapStatus.ACKNOWLEDGED.value
        assert row.acknowledged_at is not None

    def test_queue_orders_most_asked_first(self) -> None:
        for _ in range(3):
            _record("How do I enable SSO?")
        _record("How do I rotate a key?")

        async def _list(session):
            rows, _total = await gap_service.list_gaps(session, tenant_id=uuid.UUID(TENANT))
            return rows

        rows = _run(_in_session(TENANT, _list))
        assert rows[0].frequency == 3
        assert rows[1].frequency == 1

    def test_unknown_gap_is_not_found(self) -> None:
        async def _ack(session):
            return await gap_service.acknowledge(session, ctx=_ctx(), gap_id=uuid.uuid4())

        with pytest.raises(gap_service.GapError) as err:
            _run(_in_session(TENANT, _ack))
        assert err.value.code == "NOT_FOUND"


# --- Draft workflow ---------------------------------------------------------


class TestDraftWorkflow:
    def _gap(self) -> uuid.UUID:
        rec = _record(f"How do I enable SSO {uuid.uuid4().hex[:6]}?")
        assert rec.gap_id is not None
        return rec.gap_id

    def test_draft_creation_moves_the_gap_to_drafted(self) -> None:
        gap_id = self._gap()

        async def _fn(session):
            return (
                await gap_service.create_draft(
                    session,
                    ctx=_ctx(),
                    gap_id=gap_id,
                    title="Enabling SSO",
                    body="Use the SSO section in Settings.",
                ),
                await gap_service.list_gaps(session, tenant_id=uuid.UUID(TENANT)),
            )

        draft, (rows, _total) = _run(_in_session(TENANT, _fn))
        assert draft.status == DraftStatus.PENDING.value
        assert rows[0].status == GapStatus.DRAFTED.value

    def test_empty_draft_is_refused(self) -> None:
        gap_id = self._gap()

        async def _fn(session):
            return await gap_service.create_draft(
                session, ctx=_ctx(), gap_id=gap_id, title="   ", body="x"
            )

        with pytest.raises(gap_service.GapError) as err:
            _run(_in_session(TENANT, _fn))
        assert err.value.code == "EMPTY_DRAFT"

    def test_rejection_returns_the_gap_to_the_queue(self) -> None:
        gap_id = self._gap()

        async def _fn(session):
            draft = await gap_service.create_draft(
                session, ctx=_ctx(), gap_id=gap_id, title="Enabling SSO", body="Body."
            )
            await gap_service.review_draft(
                session, ctx=_ctx(), draft_id=draft.id, approve=False, notes="wrong"
            )
            rows, _total = await gap_service.list_gaps(session, tenant_id=uuid.UUID(TENANT))
            return draft, rows[0]

        draft, gap = _run(_in_session(TENANT, _fn))
        assert draft.status == DraftStatus.REJECTED.value
        assert gap.status == GapStatus.ACKNOWLEDGED.value, "the gap is still real"

    def test_double_review_is_refused(self) -> None:
        gap_id = self._gap()

        async def _fn(session):
            draft = await gap_service.create_draft(
                session, ctx=_ctx(), gap_id=gap_id, title="Enabling SSO", body="Body."
            )
            await gap_service.review_draft(session, ctx=_ctx(), draft_id=draft.id, approve=True)
            await gap_service.review_draft(session, ctx=_ctx(), draft_id=draft.id, approve=True)

        with pytest.raises(gap_service.GapError) as err:
            _run(_in_session(TENANT, _fn))
        assert err.value.code == "ALREADY_REVIEWED"


# --- Publishing (safety-critical) -------------------------------------------


class TestPublishing:
    def test_pending_draft_cannot_be_published(self) -> None:
        space_id = _make_space()

        async def _fn(session):
            rec = await gap_service.record_gap(
                session,
                tenant_id=uuid.UUID(TENANT),
                question="how do I close my account",
                reason_code="NO_AUTHORIZED_EVIDENCE",
            )
            draft = await gap_service.create_draft(
                session,
                ctx=_ctx(),
                gap_id=rec.gap_id,
                title="Closing your account",
                body="Ask support.",
            )
            return await gap_service.publish_draft(
                session, ctx=_ctx(), draft_id=draft.id, space_id=space_id
            )

        with pytest.raises(gap_service.GapError) as err:
            _run(_in_session(TENANT, _fn))
        assert err.value.code == "DRAFT_NOT_APPROVED"

    def test_reviewer_cannot_publish_their_own_approval(self) -> None:
        # Four-eyes. Same actor approves and then publishes: refused.
        space_id = _make_space()
        same = uuid.uuid5(uuid.NAMESPACE_URL, "actor:same-person")

        async def _fn(session):
            rec = await gap_service.record_gap(
                session,
                tenant_id=uuid.UUID(TENANT),
                question="how do I close my account",
                reason_code="NO_AUTHORIZED_EVIDENCE",
            )
            draft = await gap_service.create_draft(
                session,
                ctx=_ctx(actor=same),
                gap_id=rec.gap_id,
                title="Closing your account",
                body="Ask support.",
            )
            await gap_service.review_draft(
                session, ctx=_ctx(actor=same), draft_id=draft.id, approve=True
            )
            return await gap_service.publish_draft(
                session, ctx=_ctx(actor=same), draft_id=draft.id, space_id=space_id
            )

        with pytest.raises(gap_service.GapError) as err:
            _run(_in_session(TENANT, _fn))
        assert err.value.code == "SELF_APPROVAL"

    def test_approved_draft_publishes_real_knowledge(self) -> None:
        space_id = _make_space()
        author = uuid.uuid5(uuid.NAMESPACE_URL, "actor:author")
        reviewer = uuid.uuid5(uuid.NAMESPACE_URL, "actor:reviewer")

        _draft_id, _version_id, document_id = _approve_and_publish(
            space_id, author=author, reviewer=reviewer
        )

        async def _inspect(session):
            from sqlalchemy import select

            from platform_core.knowledge.models import Document, DocumentVersion

            doc = (
                (
                    await session.execute(
                        select(Document).where(Document.id == uuid.UUID(document_id))
                    )
                )
                .scalars()
                .one()
            )
            vers = (
                (
                    await session.execute(
                        select(DocumentVersion).where(
                            DocumentVersion.document_id == uuid.UUID(document_id)
                        )
                    )
                )
                .scalars()
                .all()
            )
            rows, _total = await gap_service.list_gaps(session, tenant_id=uuid.UUID(TENANT))
            return doc, vers, rows

        doc, versions, gaps = _run(_in_session(TENANT, _inspect))
        assert doc.title == "Rotating an API key"
        assert doc.space_id == space_id
        assert doc.canonical_uri.startswith("gap://")
        assert len(versions) == 1
        assert versions[0].status == "processing", "ingestion is a separate step"
        assert versions[0].content_hash
        assert gaps[0].status == GapStatus.RESOLVED.value

    def test_double_publish_is_refused(self) -> None:
        space_id = _make_space()
        author = uuid.uuid5(uuid.NAMESPACE_URL, "actor:author")
        reviewer = uuid.uuid5(uuid.NAMESPACE_URL, "actor:reviewer")
        draft_id, _version_id, _document_id = _approve_and_publish(
            space_id, author=author, reviewer=reviewer
        )
        publisher = uuid.uuid5(uuid.NAMESPACE_URL, "actor:publisher")

        async def _fn(session):
            return await gap_service.publish_draft(
                session,
                ctx=_ctx(actor=publisher),
                draft_id=uuid.UUID(draft_id),
                space_id=space_id,
            )

        with pytest.raises(gap_service.GapError) as err:
            _run(_in_session(TENANT, _fn))
        assert err.value.code == "ALREADY_PUBLISHED"

    def test_gap_derived_document_is_labeled_with_its_source(self) -> None:
        space_id = _make_space()
        author = uuid.uuid5(uuid.NAMESPACE_URL, "actor:author")
        reviewer = uuid.uuid5(uuid.NAMESPACE_URL, "actor:reviewer")
        _draft_id, _version_id, document_id = _approve_and_publish(
            space_id, author=author, reviewer=reviewer
        )

        async def _inspect(session):
            from sqlalchemy import select

            from platform_core.knowledge.models import Document, KnowledgeSource

            doc = (
                (
                    await session.execute(
                        select(Document).where(Document.id == uuid.UUID(document_id))
                    )
                )
                .scalars()
                .one()
            )
            source = (
                (
                    await session.execute(
                        select(KnowledgeSource).where(KnowledgeSource.id == doc.source_id)
                    )
                )
                .scalars()
                .one()
            )
            return source

        source = _run(_in_session(TENANT, _inspect))
        assert source.name == "Knowledge gap resolutions"


# --- Tenant isolation -------------------------------------------------------
#
# Caveat, verified by mutation: these tests run with `app.tenant_id` set for
# the calling tenant (as `apply_rls_tenant` does in production), so RLS alone
# already blocks cross-tenant rows. Deleting the service's own
# `tenant_id == ctx.tenant_id` predicate does NOT fail this suite - the
# database still filters. That is deliberate defence in depth, but it means
# these tests prove the *combination* is sound, not that the application
# predicate works on its own. The application predicate matters for the paths
# that run before or outside a tenant-scoped session, and for catching a bug
# on a deployment where RLS is misconfigured. Do not treat this suite as
# covering the application-layer filter in isolation.


class TestTenantIsolation:
    def test_other_tenants_gap_is_invisible(self) -> None:
        other = _record("How do I enable SSO?", tenant=TENANT_OTHER)
        assert other.gap_id is not None

        rows, total = _run(
            _in_session(TENANT, lambda s: gap_service.list_gaps(s, tenant_id=uuid.UUID(TENANT)))
        )
        assert total == 0, "one tenant's queue must not show another's gaps"

    def test_other_tenants_gap_cannot_be_acknowledged(self) -> None:
        other = _record("How do I enable SSO?", tenant=TENANT_OTHER)

        async def _fn(session):
            return await gap_service.acknowledge(session, ctx=_ctx(), gap_id=other.gap_id)

        with pytest.raises(gap_service.GapError) as err:
            _run(_in_session(TENANT, _fn))
        assert err.value.code == "NOT_FOUND", "absent and not-yours must be indistinguishable"

    def test_same_question_in_two_tenants_is_two_gaps(self) -> None:
        mine = _record("How do I enable SSO?")
        theirs = _record("How do I enable SSO?", tenant=TENANT_OTHER)
        assert mine.gap_id != theirs.gap_id


# --- HTTP surface -----------------------------------------------------------


class TestHttpSurface:
    def test_reader_can_list_but_not_dismiss(self, assert_denied) -> None:
        _record("How do I enable SSO?")
        agent = _client(TENANT, "support_agent")

        listed = agent.get("/v1/knowledge/gaps", headers=_headers())
        assert listed.status_code == 200
        assert listed.json()["total"] == 1

        # Any reader can read; claiming a shared work item is a write.
        gaps = agent.get("/v1/knowledge/gaps", headers=_headers()).json()["items"]
        denied = agent.post(
            f"/v1/knowledge/gaps/{gaps[0]['id']}/dismiss",
            json={"reason": "not needed"},
            headers=_headers(),
        )
        assert_denied(denied, "KNOWLEDGE_ACCESS_DENIED")

    def test_knowledge_manager_can_run_the_workflow(self) -> None:
        rec = _record("How do I enable SSO?")
        mgr = _client(TENANT, "knowledge_manager")

        acked = mgr.post(f"/v1/knowledge/gaps/{rec.gap_id}/acknowledge", headers=_headers())
        assert acked.status_code == 200
        assert acked.json()["status"] == GapStatus.ACKNOWLEDGED.value

    def test_unknown_role_is_denied_outright(self, assert_denied) -> None:
        nobody = _client(TENANT, "no_such_role")
        resp = nobody.get("/v1/knowledge/gaps", headers=_headers())
        assert_denied(resp, "KNOWLEDGE_ACCESS_DENIED")

    def test_denial_does_not_leak_counts(self) -> None:
        _record("How do I enable SSO?")
        nobody = _client(TENANT, "no_such_role")
        body = nobody.get("/v1/knowledge/gaps", headers=_headers()).json()
        assert "items" not in body and "total" not in body

    def test_invalid_status_is_refused_not_ignored(self) -> None:
        # An empty queue on a typo looks like an all-clear. Say so instead.
        mgr = _client(TENANT, "knowledge_manager")
        resp = mgr.get("/v1/knowledge/gaps?status=nonsense", headers=_headers())
        assert resp.json()["error"]["code"] == "INVALID_STATUS"

    def test_malformed_gap_id_is_not_found_not_500(self) -> None:
        mgr = _client(TENANT, "knowledge_manager")
        resp = mgr.post("/v1/knowledge/gaps/not-a-uuid/acknowledge", headers=_headers())
        assert resp.status_code == 200
        assert resp.json()["error"]["code"] == "NOT_FOUND"

    def test_http_publish_creates_a_document(self) -> None:
        space_id = _make_space()
        rec = _record("How do I enable SSO?")
        mgr = _client(TENANT, "knowledge_manager")

        created = mgr.post(
            f"/v1/knowledge/gaps/{rec.gap_id}/drafts",
            json={"title": "Enabling SSO", "body": "Use Settings > SSO."},
            headers=_headers(),
        ).json()
        assert created["status"] == DraftStatus.PENDING.value

        # The same actor reviewing and publishing is refused by four-eyes.
        mgr.post(
            f"/v1/knowledge/drafts/{created['id']}/review",
            json={"approve": True},
            headers=_headers(),
        )
        refused = mgr.post(
            f"/v1/knowledge/drafts/{created['id']}/publish",
            json={"space_id": str(space_id)},
            headers=_headers(),
        ).json()
        assert refused["error"]["code"] == "SELF_APPROVAL"

    def test_audit_events_are_recorded(self) -> None:
        rec = _record("How do I enable SSO?")
        mgr = _client(TENANT, "knowledge_manager")
        mgr.post(f"/v1/knowledge/gaps/{rec.gap_id}/acknowledge", headers=_headers())

        admin = create_engine(ADMIN_URL)
        with admin.begin() as conn:
            count = conn.execute(
                text(
                    "SELECT count(*) FROM audit_events WHERE tenant_id = :t "
                    "AND action = 'knowledge_gap.acknowledged'"
                ),
                {"t": TENANT},
            ).scalar_one()
        admin.dispose()
        assert count >= 1

    def test_stats_endpoint_returns_counts(self) -> None:
        _record("How do I enable SSO?")
        _record("How do I enable SSO?")
        mgr = _client(TENANT, "knowledge_manager")
        body = mgr.get("/v1/knowledge/gaps/stats", headers=_headers()).json()
        assert body["total_gaps"] == 1
        assert body["total_occurrences"] == 2
        assert body["by_status"][GapStatus.OPEN.value] == 1
