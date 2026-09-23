"""Feature list 8.3: conversation replay.

What these tests are here to protect, in order of importance:

1. **A decision is tied to an utterance by hash, never by time.** The replay
   attaches a run to the turn whose `text_hash` equals the run's `input_hash`.
   If that link ever degrades to "the turn that happened closest in time", the
   replay starts explaining one customer's question with another's answer, and
   it would do so exactly when a conversation is busy - which is when a replay
   is opened.
2. **Nothing that happened is hidden.** Runs whose hash matches no turn are
   still listed under `runs`; conversations that exist only as runs are still
   listed; queued runs with no content are excluded from the list but counted
   in `nothing_to_replay`. A view that drops rows silently is worse than one that
   omits them knowingly.
3. **The text is what was stored, and what was stored is redacted.** A replay
   must not become the surface that leaks a phone number back into the
   operator's screen by re-hydrating what retention removed.
4. **Tenant isolation.** Another tenant's conversation is a 404, not an empty
   replay.

The pattern mirrors `test_conversation_continuity.py` (real PostgreSQL through
the application role, so RLS applies) plus `test_m2_http_api.py`'s fabricated
role resolver for the endpoint-level assertions.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from platform_core.identity.middleware import TenantContextMiddleware
from platform_core.identity.tenant_context import TenantContext
from platform_core.orm_base import default_uuid

pytestmark = pytest.mark.integration

ADMIN_URL = os.environ.get(
    "APP_ADMIN_DATABASE_URL",
    "postgresql+psycopg://platform:platform@localhost:5435/platform",
)
APP_URL = "postgresql+psycopg://platform_app:platform_app@localhost:5435/platform"

TENANT = "01900000-0000-7000-8000-0000000000c3"
OTHER_TENANT = "01900000-0000-7000-8000-0000000000de"
SLUG = "conversation-replay"
OTHER_SLUG = "conversation-replay-b"


def _run(coro):
    return asyncio.run(coro, loop_factory=asyncio.SelectorEventLoop)


def _digest(raw: str) -> str:
    """The platform's convention: sha256 of the *raw* text, unredacted.

    Both write paths hash before discarding (`conversation_store.append_turn`
    and `chat_service.append_customer_turn`), which is what makes the turn to
    run correlation possible at all.
    """
    return hashlib.sha256(raw.encode()).hexdigest()


def _ref(tenant: str, seed: uuid.UUID) -> uuid.UUID:
    """A conversation's platform ref, which is both the stored id and the path value.

    Seeding and requesting name the **same** id. The HTTP tests used to store
    under a derived id and pass the pre-derivation value in the path, because
    the endpoints derived; they no longer do, so the two must agree or the
    tests prove nothing about the endpoint.

    The value is still minted with `conversation_ref_for` rather than picked at
    random, so the rows look like the rows real writers produce.
    """
    from platform_core.support_bridge.conversation_ref import conversation_ref_for

    return conversation_ref_for(uuid.UUID(tenant), str(seed))


def _seed_tenants() -> None:
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        for tid, slug in ((TENANT, SLUG), (OTHER_TENANT, OTHER_SLUG)):
            conn.execute(
                text(
                    "INSERT INTO tenants (id, slug, name, status) VALUES "
                    "(:id, :slug, 'Replay', 'active') ON CONFLICT (slug) DO NOTHING"
                ),
                {"id": tid, "slug": slug},
            )
    admin.dispose()


def _clear() -> None:
    """Delete this file's rows only, children first."""
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        for tid in (TENANT, OTHER_TENANT):
            conn.execute(
                text(
                    "DELETE FROM citations WHERE agent_run_id IN "
                    "(SELECT id FROM agent_runs WHERE tenant_id = :t)"
                ),
                {"t": tid},
            )
            conn.execute(text("DELETE FROM agent_runs WHERE tenant_id = :t"), {"t": tid})
            conn.execute(text("DELETE FROM conversation_turns WHERE tenant_id = :t"), {"t": tid})
        conn.execute(
            text("DELETE FROM tenants WHERE slug IN (:a, :b)"), {"a": SLUG, "b": OTHER_SLUG}
        )
    admin.dispose()


@pytest.fixture(scope="module", autouse=True)
def seeded() -> None:
    _seed_tenants()
    yield
    _clear()


async def _session(tenant: str = TENANT):
    from sqlalchemy import text as sa_text

    from platform_core.db import create_engine as async_engine

    engine = async_engine(APP_URL)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    session = factory()
    await session.execute(sa_text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant})
    return session


def _add_turn(
    conversation_ref_id: uuid.UUID,
    *,
    role: str,
    raw: str,
    redacted: str | None = None,
    ts: int = 1_700_000_000,
    source: str = "platform",
    tenant: str = TENANT,
) -> uuid.UUID:
    row_id = default_uuid()

    async def _inner():
        session = await _session(tenant)
        try:
            await session.execute(
                text(
                    "INSERT INTO conversation_turns (id, tenant_id, conversation_ref_id, role, "
                    "text_redacted, text_hash, ts, ref, source, created_at) VALUES "
                    "(:id, :t, :ref, :role, :text, :hash, :ts, '', :source, :ts)"
                ),
                {
                    "id": row_id,
                    "t": tenant,
                    "ref": conversation_ref_id,
                    "role": role,
                    "text": redacted if redacted is not None else raw,
                    "hash": _digest(raw),
                    "ts": ts,
                    "source": source,
                },
            )
            await session.commit()
        finally:
            await session.close()

    _run(_inner())
    return row_id


def _add_run(
    conversation_ref_id: uuid.UUID,
    *,
    raw: str | None,
    route: str = "knowledge_qa",
    status: str = "completed",
    abstain_reason: str | None = None,
    started_at: int | None = 1_700_000_000,
    model_config: str = '{"model": "qwen3.8-flash", "intent": {"scene": "order", '
    '"business_line": "pcb", "primary_kind": "knowledge_question", "confidence": 0.7}}',
    tenant: str = TENANT,
) -> uuid.UUID:
    """Insert a run. `raw=None` means the placeholder shape: queued, no input."""
    row_id = default_uuid()

    async def _inner():
        session = await _session(tenant)
        try:
            await session.execute(
                text(
                    "INSERT INTO agent_runs (id, tenant_id, conversation_ref_id, route, status, "
                    "model_config, retrieval_config, policy_version, code_version, trace_id, "
                    "input_hash, token_usage, latency_ms, abstain_reason, started_at) VALUES "
                    "(:id, :t, :ref, :route, :status, CAST(:mc AS jsonb), '{}'::jsonb, 'v1', "
                    "'0.1.0', '', :hash, '{}'::jsonb, 42, :abstain, :started)"
                ),
                {
                    "id": row_id,
                    "t": tenant,
                    "ref": conversation_ref_id,
                    "route": route,
                    "status": status,
                    "mc": model_config,
                    "hash": "" if raw is None else _digest(raw),
                    "abstain": abstain_reason,
                    "started": started_at,
                },
            )
            await session.commit()
        finally:
            await session.close()

    _run(_inner())
    return row_id


def _add_citation(agent_run_id: uuid.UUID, *, source_uri: str, claim_index: int = 0) -> None:
    async def _inner():
        session = await _session()
        try:
            await session.execute(
                text(
                    "INSERT INTO citations (id, tenant_id, agent_run_id, document_version_id, "
                    "chunk_id, excerpt_hash, source_uri, claim_index, retrieval_score) VALUES "
                    "(:id, :t, :run, NULL, :chunk, :excerpt, :uri, :idx, 0.81)"
                ),
                {
                    "id": default_uuid(),
                    "t": TENANT,
                    "run": agent_run_id,
                    "chunk": default_uuid(),
                    "excerpt": _digest("excerpt"),
                    "uri": source_uri,
                    "idx": claim_index,
                },
            )
            await session.commit()
        finally:
            await session.close()

    _run(_inner())


def _replay(conversation_ref_id: uuid.UUID, tenant: str = TENANT, **kwargs):
    from platform_core.agent_runtime import replay

    async def _inner():
        session = await _session(tenant)
        try:
            return await replay.build_replay(
                session,
                tenant_id=uuid.UUID(tenant),
                conversation_ref_id=conversation_ref_id,
                **kwargs,
            )
        finally:
            await session.close()

    return _run(_inner())


def _list(tenant: str = TENANT, **kwargs):
    from platform_core.agent_runtime import replay

    async def _inner():
        session = await _session(tenant)
        try:
            return await replay.list_conversations(session, tenant_id=uuid.UUID(tenant), **kwargs)
        finally:
            await session.close()

    return _run(_inner())


# --- 1. The correlation -----------------------------------------------------


def test_a_decision_is_attached_to_the_turn_that_triggered_it() -> None:
    """The whole point of the screen: this question, that decision."""
    conversation = default_uuid()
    asked = "can you expedite SO-9001?"
    _add_turn(conversation, role="customer", raw=asked)
    run_id = _add_run(
        conversation,
        raw=asked,
        route="business_read",
        status="abstained",
        abstain_reason="IDENTITY_MISMATCH",
    )

    bundle = _replay(conversation)
    assert bundle is not None
    decisions = [t["decision"] for t in bundle["turns"] if t["decision"] is not None]
    assert len(decisions) == 1
    assert decisions[0]["run_id"] == str(run_id)
    assert decisions[0]["route"] == "business_read"
    assert decisions[0]["abstain_reason"] == "IDENTITY_MISMATCH"
    # The link states how it was made, so a reader can judge it.
    assert decisions[0]["matched_by"] == "input_hash"


def test_the_decision_carries_the_routing_snapshot() -> None:
    """`why did it answer that` is answered by the intent, not the route alone."""
    conversation = default_uuid()
    asked = "impedance for FR4?"
    _add_turn(conversation, role="customer", raw=asked)
    _add_run(conversation, raw=asked)

    bundle = _replay(conversation)
    intent = bundle["turns"][0]["decision"]["intent"]
    assert intent["business_line"] == "pcb"
    assert intent["scene"] == "order"
    assert bundle["turns"][0]["decision"]["model"] == "qwen3.8-flash"


def test_the_intent_snapshot_is_narrowed_not_forwarded_wholesale() -> None:
    """A replay is a view of a decision, not a JSON viewer.

    `model_config` also holds conversation budgeting and sampling settings.
    Forwarding all of it would make the operator hunt for the three fields that
    matter, and would freeze internal config into a public payload.
    """
    conversation = default_uuid()
    asked = "why is my order late"
    _add_turn(conversation, role="customer", raw=asked)
    _add_run(
        conversation,
        raw=asked,
        model_config='{"model": "m", "temperature": 0.0, "conversation": {"budget_chars": 1500},'
        ' "intent": {"route": "knowledge_qa", "scene": "order", "business_line": "pcb"}}',
    )

    intent = _replay(conversation)["turns"][0]["decision"]["intent"]
    assert "temperature" not in intent
    assert "conversation" not in intent
    assert "budget_chars" not in intent


def test_a_run_whose_input_matches_no_turn_is_kept_but_attached_to_nothing() -> None:
    """No guessing by proximity.

    An unattributable run still belongs in the replay - it happened, and its
    latency and abstention are part of the story - but it must not be hung on
    whichever turn came closest in time.
    """
    conversation = default_uuid()
    _add_turn(conversation, role="customer", raw="first question", ts=1_700_000_000)
    _add_run(conversation, raw="a question no turn records", route="sensitive")

    bundle = _replay(conversation)
    assert [t["decision"] for t in bundle["turns"]] == [None]
    assert len(bundle["runs"]) == 1
    assert bundle["runs"][0]["route"] == "sensitive"


def test_an_agent_turn_never_carries_a_decision() -> None:
    """Only a customer turn can trigger a run.

    An agent turn's `text_hash` is the reply text. If a run's `input_hash` ever
    collided with it, attaching the decision there would place the explanation
    after the answer it explains.
    """
    conversation = default_uuid()
    reply = "Here is what I found."
    _add_turn(conversation, role="agent", raw=reply, ts=1_700_000_100)
    # Same bytes, stored as a run input - the collision the role filter catches.
    _add_run(conversation, raw=reply)

    bundle = _replay(conversation)
    assert [t["decision"] for t in bundle["turns"]] == [None]


# --- 2. Text honesty --------------------------------------------------------


def test_the_replay_serves_the_stored_redacted_text_and_nothing_more() -> None:
    """The replay is not a route around redaction.

    A turn is stored redacted and hashed from the raw bytes; the replay must
    show the stored text. Re-hydrating the number would make this screen the
    one place PII re-enters after retention removed it.
    """
    conversation = default_uuid()
    raw = "my number is 13800001111, call me"
    stored = "my number is [PHONE], call me"
    _add_turn(conversation, role="customer", raw=raw, redacted=stored)

    bundle = _replay(conversation)
    assert bundle["turns"][0]["text"] == stored
    assert "13800001111" not in str(bundle)


def test_turns_are_ordered_oldest_first_with_the_window_reported() -> None:
    """A replay reads forwards; the window says what it is looking at."""
    conversation = default_uuid()
    _add_turn(conversation, role="customer", raw="one", ts=1_700_000_000)
    _add_turn(conversation, role="agent", raw="two", ts=1_700_000_060)
    _add_turn(conversation, role="customer", raw="three", ts=1_700_000_120)

    bundle = _replay(conversation)
    assert [t["text"] for t in bundle["turns"]] == ["one", "two", "three"]
    assert bundle["first_at"] == 1_700_000_000
    assert bundle["last_at"] == 1_700_000_120
    assert bundle["turn_count"] == 3


def test_sources_are_listed_against_the_run_that_cited_them() -> None:
    """The evidence half of the explanation."""
    conversation = default_uuid()
    asked = "what is the lead time"
    _add_turn(conversation, role="customer", raw=asked)
    run_id = _add_run(conversation, raw=asked)
    _add_citation(run_id, source_uri="knowledge://sop/lead-time", claim_index=0)

    bundle = _replay(conversation)
    run = next(r for r in bundle["runs"] if r["run_id"] == str(run_id))
    assert [s["source_uri"] for s in run["sources"]] == ["knowledge://sop/lead-time"]
    assert run["sources"][0]["retrieval_score"] == pytest.approx(0.81)


def test_an_unknown_conversation_returns_none_rather_than_an_empty_exchange() -> None:
    """`not here` and `nothing happened` are different answers."""
    assert _replay(default_uuid()) is None


# --- 3. The listing ---------------------------------------------------------


def test_the_list_covers_a_conversation_with_turns_but_no_run() -> None:
    """Runs are not the only trace of an exchange.

    In the live database 12 conversations have turns and no run at all - a
    refused queue call leaves the customer's words behind. A listing built from
    `agent_runs` would hide them.
    """
    conversation = default_uuid()
    _add_turn(conversation, role="customer", raw="what are your support hours?", ts=1_800_000_000)

    listing = _list(limit=50, offset=0)
    ids = [item["conversation_ref_id"] for item in listing["items"]]
    assert str(conversation) in ids


def test_the_list_covers_a_conversation_whose_turns_live_under_another_id() -> None:
    """The mirror case: a run with content but no turns under that ref.

    Also present in the live database, from the period when the queue path and
    the worker derived the conversation id differently. Those runs answered a
    customer, so a listing built from turns would hide 182 real answers.
    """
    conversation = default_uuid()
    _add_run(conversation, raw="a question whose turn is filed elsewhere", started_at=1_800_000_100)

    listing = _list(limit=50, offset=0)
    ids = [item["conversation_ref_id"] for item in listing["items"]]
    assert str(conversation) in ids


def test_a_queued_run_without_content_is_counted_rather_than_silently_dropped() -> None:
    """Excluded from the list, but the number is on the screen.

    These rows are one per queue call whose worker never advanced it. They have
    no utterance and no outcome, so there is nothing to replay - but a view
    that cannot say they exist is how 287 of them accumulate unremarked.
    """
    before = _list(limit=1, offset=0)["nothing_to_replay"]
    conversation = default_uuid()
    _add_run(conversation, raw=None, status="queued", started_at=1_800_000_200)

    listing = _list(limit=50, offset=0)
    ids = [item["conversation_ref_id"] for item in listing["items"]]
    assert str(conversation) not in ids
    assert listing["nothing_to_replay"] == before + 1


def test_a_conversation_that_also_has_content_is_not_counted_as_awaiting() -> None:
    """The count is `waiting and nothing else`, not `waiting`."""
    conversation = default_uuid()
    _add_turn(conversation, role="customer", raw="real question", ts=1_800_000_300)
    _add_run(conversation, raw=None, status="queued", started_at=1_800_000_360)

    listing = _list(limit=50, offset=0)
    entry = next(
        item for item in listing["items"] if item["conversation_ref_id"] == str(conversation)
    )
    # The newest run is the placeholder, and saying so is the point: the last
    # thing that happened is that a run is waiting.
    assert entry["latest_run"]["status"] == "queued"
    assert entry["turn_count"] == 1


def test_the_list_is_ordered_by_last_activity_and_pages_without_gaps() -> None:
    conversation_old = default_uuid()
    conversation_new = default_uuid()
    _add_turn(conversation_old, role="customer", raw="old", ts=1_810_000_000)
    _add_run(conversation_new, raw="new", started_at=1_810_000_500)

    page_one = _list(limit=2, offset=0)
    page_two = _list(limit=2, offset=2)
    ordered = [item["conversation_ref_id"] for item in page_one["items"]]
    assert ordered[0] == str(conversation_new)
    assert ordered[1] == str(conversation_old)
    # Offset paging is only safe if the order is total; the ref breaks ties.
    assert set(ordered).isdisjoint(item["conversation_ref_id"] for item in page_two["items"])


def test_one_tenants_conversation_is_invisible_to_another() -> None:
    """RLS, asserted through the service rather than assumed from the policy."""
    theirs = default_uuid()
    _add_turn(theirs, role="customer", raw="their question", ts=1_820_000_000, tenant=OTHER_TENANT)
    _add_run(theirs, raw="their question", started_at=1_820_000_000, tenant=OTHER_TENANT)

    assert _replay(theirs, tenant=TENANT) is None
    ids = [item["conversation_ref_id"] for item in _list(limit=50, offset=0)["items"]]
    assert str(theirs) not in ids
    # And the owner does see it, so the assertion above is not passing because
    # the seeding silently failed.
    assert _replay(theirs, tenant=OTHER_TENANT) is not None


# --- 4. The endpoints -------------------------------------------------------


class _RoleResolver:
    """Fabricate a TenantContext with a fixed tenant and role."""

    def __init__(self, tenant_id: str, role: str | None) -> None:
        self._tenant_id = tenant_id
        self._role = role

    async def __call__(self, request: object) -> TenantContext:
        return TenantContext(
            tenant_id=uuid.UUID(self._tenant_id),
            actor_id=uuid.uuid5(uuid.NAMESPACE_URL, f"replay:{self._tenant_id}-{self._role}"),
            actor_kind="user",
            role=self._role,
        )


def _client(tenant_id: str, role: str | None) -> TestClient:
    import importlib

    main_mod = importlib.import_module("platform_core.main")
    fresh = FastAPI()
    for route in main_mod.app.router.routes:
        fresh.router.routes.append(route)
    fresh.add_middleware(TenantContextMiddleware, resolver=_RoleResolver(tenant_id, role))
    return TestClient(fresh, raise_server_exceptions=False)


def _auth() -> dict[str, str]:
    return {"Authorization": "Bearer pt_bootstrap_test"}


def test_replay_over_http_returns_the_exchange() -> None:
    ref = _ref(TENANT, default_uuid())
    asked = "what is the MOQ"
    _add_turn(ref, role="customer", raw=asked)
    _add_run(ref, raw=asked)

    resp = _client(TENANT, "support_admin").get(
        f"/v1/conversations/{ref}/replay", headers=_auth()
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["turn_count"] == 1
    assert body["turns"][0]["decision"]["matched_by"] == "input_hash"
    assert body["trace_id"]


def test_the_ref_a_listing_returns_is_the_ref_replay_accepts() -> None:
    """The console's whole flow: list, click, read. It used to dead-end.

    `/v1/conversations` lists `conversation_ref_id`, and the replay screen feeds
    that value straight back into `/{ref}/replay`. While replay derived the path
    segment, the listing's own value resolved to a second conversation - three
    of four sampled conversations returned `NOT_FOUND` and the fourth returned
    an unrelated transcript, with no error anywhere to say so.

    Asserting the round trip rather than the two halves separately, because
    each half was already correct; only the join was wrong.
    """
    ref = _ref(TENANT, default_uuid())
    asked = "does the listing agree with the replay"
    _add_turn(ref, role="customer", raw=asked)
    _add_run(ref, raw=asked)

    listing = _client(TENANT, "support_admin").get("/v1/conversations", headers=_auth())
    assert listing.status_code == 200, listing.text
    listed = [item["conversation_ref_id"] for item in listing.json()["items"]]
    assert str(ref) in listed, f"the conversation is not listed at all: {listed}"

    resp = _client(TENANT, "support_admin").get(
        f"/v1/conversations/{ref}/replay", headers=_auth()
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # The identity, not just "some conversation": a 200 carrying a different
    # transcript is the failure mode that made this defect invisible.
    assert body["conversation_ref_id"] == str(ref)
    assert body["turn_count"] == 1
    assert body["turns"][0]["text"] == asked


def test_the_path_segment_is_not_an_external_id() -> None:
    """A ref is used verbatim - the pre-derivation value must not resolve.

    This is the contract, stated as a negative case. If a derivation comes back,
    this turns green and the test above turns red, which is the pair a future
    reader needs to see.
    """
    external = default_uuid()
    _add_turn(_ref(TENANT, external), role="customer", raw="stored under the ref")

    resp = _client(TENANT, "support_admin").get(
        f"/v1/conversations/{external}/replay", headers=_auth()
    )
    assert resp.status_code == 404, resp.text


def test_an_unknown_conversation_is_a_404() -> None:
    """Not an empty timeline: an operator chasing a wrong id needs to know."""
    resp = _client(TENANT, "support_admin").get(
        f"/v1/conversations/{default_uuid()}/replay", headers=_auth()
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "NOT_FOUND"


def test_a_malformed_ref_is_a_validation_error_not_a_500() -> None:
    resp = _client(TENANT, "support_admin").get(
        "/v1/conversations/not-a-uuid/replay", headers=_auth()
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "VALIDATION_FAILED"


def test_another_tenants_conversation_is_a_404_over_http() -> None:
    theirs = _ref(OTHER_TENANT, default_uuid())
    _add_turn(
        theirs,
        role="customer",
        raw="their question",
        ts=1_830_000_000,
        tenant=OTHER_TENANT,
    )

    resp = _client(TENANT, "support_admin").get(
        f"/v1/conversations/{theirs}/replay", headers=_auth()
    )
    assert resp.status_code == 404


def test_the_replay_requires_the_case_read_permission() -> None:
    """Reading a customer's exchange is a case read, not a free lookup."""
    external = default_uuid()
    # Store under the platform ref (the endpoint takes the path verbatim now,
    # not an external id to derive). The policy gate is checked before the
    # replay lookup, so existence is irrelevant to the 403 - this just seeds
    # a real row so the denial is not an artifact of an empty table.
    _add_turn(_ref(TENANT, external), role="customer", raw="permission check", ts=1_840_000_000)

    denied = _client(TENANT, None).get(f"/v1/conversations/{external}/replay", headers=_auth())
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "POLICY_DENIED"

    listing = _client(TENANT, None).get("/v1/conversations", headers=_auth())
    assert listing.status_code == 403


def test_the_listing_endpoint_returns_items_and_the_awaiting_count() -> None:
    resp = _client(TENANT, "support_admin").get(
        "/v1/conversations", params={"limit": 5}, headers=_auth()
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["limit"] == 5
    assert isinstance(body["items"], list)
    assert isinstance(body["nothing_to_replay"], int)
