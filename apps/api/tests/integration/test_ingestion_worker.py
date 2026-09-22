"""Ingestion worker: DocumentVersion -> retrievable chunks.

The property under test
-----------------------
A document that has been uploaded but not ingested must not be retrievable,
and a document that reports READY must actually be retrievable. Both
directions matter:

- If a READY version has no chunks, `hybrid_search` returns nothing and the
  orchestrator abstains. The failure is invisible - it looks exactly like a
  question the knowledge base genuinely cannot answer.
- If a version becomes `status='active'` before its chunks are written, a
  partially-indexed document becomes citable. A citation would point at a
  document whose text is only half present, and nothing downstream could
  tell.

So the suite drives the real pipeline (parse -> chunk -> embed -> index ->
READY) against the real database, with a stub embedder standing in for the
provider. The stub is deterministic and produces vectors aligned to input
order, which is what lets the alignment assertion below be meaningful: a
real provider returning vectors out of order would silently attach every
chunk's embedding to the wrong text.

The end-to-end case at the bottom is the one that matters most: it goes
upload -> worker -> `hybrid_search` and asserts a real query returns the
document. Everything above it is a component check.
"""

import asyncio
import json
import os
import sys
import uuid
from dataclasses import dataclass, field

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from uuid6 import uuid7

pytestmark = pytest.mark.integration

ADMIN_URL = os.environ.get(
    "APP_ADMIN_DATABASE_URL",
    "postgresql+psycopg://platform:platform@localhost:5435/platform",
)
APP_URL = os.environ.get(
    "APP_APP_DATABASE_URL",
    "postgresql+psycopg://platform_app:platform_app@localhost:5435/platform",
)

TENANT = "01900000-0000-7000-8000-000000000b01"
OTHER_TENANT = "01900000-0000-7000-8000-000000000b02"
SPACE = "01900000-0000-7000-8000-000000000b11"
SLUG = "ingest-worker"
OTHER_SLUG = "ingest-worker-other"

DOC = """# Refund policy

Refunds are processed within five business days of approval.

## Eligibility

A refund is eligible when the order was placed in the last 30 days.

## Exceptions

Digital goods are non-refundable once downloaded.
"""


@dataclass
class StubEmbedder:
    """Deterministic embedder that honours input order.

    Not a semantic model: vectors are derived from the text's own hash, so
    "similar text" does not produce "similar vector". That is deliberate -
    this suite is testing the pipeline's write path and ordering discipline,
    and a semantic stub would make a broken pipeline look like it worked
    because the query happened to match.

    Two surfaces, because the two callers need different shapes and using one
    for both would hide a real mismatch:

    - `embed(texts)` is the provider batch API the ingestion worker calls.
    - `embed_query(text)` is the narrower `retrieval.Embedder` protocol that
      `hybrid_search` calls for the query vector. Without it the vector half
      of the fusion would be silently skipped, so a test that only stubbed
      `embed` could not tell whether the search path was exercised at all
      (it failed with an AttributeError the first time this ran, which is the
      honest way to find out).

    Records the batches it received so a test can assert chunking actually
    reached the model (an embedder that is never called is the shape of the
    bug this module fixed).
    """

    dimensions: int = 1536
    batches: list[list[str]] = field(default_factory=list)
    queries: list[str] = field(default_factory=list)
    fail_with: Exception | None = None

    async def embed(self, texts: list[str], *, model: str | None = None) -> object:
        if self.fail_with is not None:
            raise self.fail_with
        self.batches.append(list(texts))

        from platform_core.llm.provider import EmbeddingResult
        from platform_core.retrieval.hybrid import embed_deterministic

        return EmbeddingResult(
            vectors=[embed_deterministic(t, self.dimensions) for t in texts],
            model=model or "stub",
            dimensions=self.dimensions,
        )

    async def embed_query(self, query: str) -> list[float]:
        if self.fail_with is not None:
            raise self.fail_with
        self.queries.append(query)

        from platform_core.retrieval.hybrid import embed_deterministic

        return embed_deterministic(query, self.dimensions)


def _run(coro: object) -> object:
    """psycopg async needs a selector loop on Windows."""
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    return asyncio.run(coro, loop_factory=asyncio.SelectorEventLoop)  # type: ignore[arg-type]


@pytest.fixture(scope="module", autouse=True)
def seed() -> None:
    admin = create_engine(ADMIN_URL)
    _cleanup(admin)
    with admin.begin() as conn:
        for tid, slug, name in (
            (TENANT, SLUG, "Ingest Worker"),
            (OTHER_TENANT, OTHER_SLUG, "Ingest Worker Other"),
        ):
            conn.execute(
                text(
                    "INSERT INTO tenants (id, slug, name, status) VALUES "
                    "(:id, :slug, :name, 'active') ON CONFLICT (slug) DO NOTHING"
                ),
                {"id": tid, "slug": slug, "name": name},
            )
        conn.execute(
            text(
                "INSERT INTO knowledge_spaces (id, tenant_id, name, status) "
                "VALUES (:id, :t, 'Ingest', 'active') ON CONFLICT DO NOTHING"
            ),
            {"id": SPACE, "t": TENANT},
        )
    yield
    _cleanup(admin)
    admin.dispose()


def _cleanup(admin: object) -> None:
    with admin.begin() as conn:  # type: ignore[attr-defined]
        for table in ("chunks", "knowledge_acls", "document_versions", "documents"):
            conn.execute(
                text(
                    f"DELETE FROM {table} WHERE tenant_id IN "  # noqa: S608 - fixed table names
                    "(SELECT id FROM tenants WHERE slug = ANY(:s))"
                ),
                {"s": [SLUG, OTHER_SLUG]},
            )
        conn.execute(
            text(
                "DELETE FROM knowledge_spaces WHERE tenant_id IN "
                "(SELECT id FROM tenants WHERE slug = ANY(:s))"
            ),
            {"s": [SLUG, OTHER_SLUG]},
        )
        conn.execute(text("DELETE FROM tenants WHERE slug = ANY(:s)"), {"s": [SLUG, OTHER_SLUG]})


def _make_version(
    *,
    tenant: str = TENANT,
    uri: str | None = None,
    body: str = DOC,
    content_type: str | None = None,
) -> str:
    """Insert a document + version at `uploaded`, exactly as the API would.

    Written with raw SQL rather than the service so the fixture does not
    depend on the code under test: if `create_document` regressed, a
    fixture that called it would fail for the wrong reason.

    `content_type` is opt-in rather than defaulted, because the two cases are
    genuinely different code paths and only one of them was covered before:
    a version row with no `content_type` in metadata yields SQL NULL from the
    claim function and the worker falls back to markdown, while a version
    uploaded through the **API** always carries one. Migration 0024 got the
    JSON extraction wrong and only the second path failed - see
    `test_the_claim_returns_an_unquoted_content_type`.
    """
    uri = uri or f"doc://{uuid.uuid4()}"
    # `metadata` is NOT NULL with a `'{}'` default, and an explicit NULL in
    # the INSERT overrides that default - so the empty case passes `"{}"`, not
    # None. (Same trap as the ORM `server_default` note in the project memory.)
    metadata = json.dumps({"content_type": content_type}) if content_type else "{}"
    # UUIDv7, matching what production inserts (`PkMixin.default_uuid` ->
    # `uuid7`), and NOT the `gen_random_uuid()` this fixture used to call.
    # This is load-bearing rather than cosmetic: the claim orders by
    # `created_at, id` (migration 0039), and `created_at` is whole seconds, so
    # every row seeded in one test shares an ordering key and `id` decides the
    # tie. Under a random v4 the tie order was arbitrary, the FIFO window could
    # land anywhere, and a narrowed claim could still miss - which showed up as
    # a different test failing on each run. Inserting v7 makes the fixture's
    # ordering agree with production's, so "oldest first" means here what it
    # means there.
    doc_id_value = uuid7()
    version_id_value = uuid7()
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        doc_id = conn.execute(
            text(
                "INSERT INTO documents (id, tenant_id, space_id, canonical_uri, title, "
                "classification) VALUES (:id, :t, :s, :uri, 'Refund policy', "
                "'internal') RETURNING id"
            ),
            {
                "id": doc_id_value,
                "t": tenant,
                "s": SPACE if tenant == TENANT else SPACE,
                "uri": uri,
            },
        ).scalar_one()
        version_id = conn.execute(
            text(
                "INSERT INTO document_versions (id, tenant_id, document_id, version_label, "
                "content_hash, status, object_uri, ingestion_status, metadata) VALUES "
                "(:id, :t, :d, :label, 'sha256:stub', 'processing', :key, "
                "'uploaded', CAST(:meta AS jsonb)) RETURNING id"
            ),
            {
                "id": version_id_value,
                "t": tenant,
                "d": doc_id,
                "label": f"v-{uuid.uuid4().hex[:8]}",
                "key": f"{tenant}/stub/file.md",
                "meta": metadata,
            },
        ).scalar_one()
    admin.dispose()
    return str(version_id)


def _read_version(version_id: str) -> dict:
    admin = create_engine(ADMIN_URL)
    with admin.connect() as conn:
        row = (
            conn.execute(
                text(
                    "SELECT ingestion_status, status, metadata FROM document_versions WHERE id = :v"
                ),
                {"v": version_id},
            )
            .mappings()
            .one()
        )
        chunk_count = conn.execute(
            text("SELECT count(*) FROM chunks WHERE document_version_id = :v"),
            {"v": version_id},
        ).scalar_one()
    admin.dispose()
    return {
        "status": row["ingestion_status"],
        "doc_status": row["status"],
        "metadata": row["metadata"],
        "chunks": int(chunk_count),
    }


class _ClosingSession(AsyncSession):
    """An `AsyncSession` that always ends its transaction when it goes away.

    Why this exists, and why it is a class rather than a rule in a docstring.

    `async with factory() as session:` does **not** end the transaction on exit
    - it returns the connection to the pool with whatever transaction was open
    still open. For a read-only test that is invisible. For a test that calls
    `claim_versions` it is not: the claim runs `... FOR UPDATE SKIP LOCKED` and
    then UPDATEs the claimed rows, so the session holds **row locks** on
    `document_versions`. Closing without commit/rollback leaves those locks
    held by a pooled connection, which Postgres reports as `idle in
    transaction`.

    The consequence is not a leak, it is a *cross-test* failure with a
    misleading symptom:

    - The next claim on that same pooled connection hits `FOR UPDATE SKIP
      LOCKED`, which **skips** a locked row rather than waiting on it, so the
      claim silently returns fewer rows than requested. Measured: `claim(3)`
      over 3 requested ids returned 2, missing exactly the locked one.
    - A narrowed claim can therefore still miss its own row, and
      `drain_versions` reports `IngestionError: ... never ingested` for a
      document that is perfectly ingestable. Before this class, three
      consecutive runs of this file failed three *different* tests, and a
      fourth run was green.
    - Worse, the stale locks deadlock against the module fixture's teardown
      `DELETE FROM document_versions`, which aborts the cleanup and leaves
      `documents` rows referencing a deleted `knowledge_spaces` row - so the
      *next* run dies on a foreign-key violation that has nothing to do with
      what it was testing.

    Fixing the one call site that happened to leak first was not enough: the
    idiom is used 27 times in this file, and every claim-issuing one has the
    same latent bug. Ending the transaction centrally means no call site can
    reintroduce it, and a future test cannot forget.
    """

    async def __aexit__(self, *args: object) -> None:  # type: ignore[override]
        # Roll back rather than commit: this runs *after* the test body, so a
        # commit would be a second, unreviewed write. The tests that need their
        # work persisted commit explicitly; everything else is rolled back, and
        # the rollback is what releases the row locks.
        if self.in_transaction():
            await self.rollback()
        await super().__aexit__(*args)  # type: ignore[arg-type]


def _session_factory(url: str) -> tuple[object, object]:
    engine = create_async_engine(url, pool_pre_ping=True)
    return engine, async_sessionmaker(engine, expire_on_commit=False, class_=_ClosingSession)


# --- 1. The happy path ----------------------------------------------------


def test_ingestion_produces_chunks_and_marks_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    """The pipeline writes chunks and only then promotes the version.

    Asserting both halves in one test is intentional: they are one invariant.
    Chunks without READY means the document stays invisible; READY without
    chunks means a citation to nothing.
    """
    from worker.ingestion_consumer import drain_versions

    version_id = _make_version()
    monkeypatch.setattr(
        "platform_core.knowledge.service.get_object", lambda key: DOC.encode("utf-8")
    )

    embedder = StubEmbedder()

    async def _drive() -> object:
        engine, factory = _session_factory(APP_URL)
        try:
            async with factory() as session:  # type: ignore[operator]
                stats = await drain_versions(session, [uuid.UUID(version_id)], embedder=embedder)
                # `drain_versions` does not commit: the caller owns the
                # unit of work, same contract as `drain_ingestion_once` and
                # `inbox_consumer.drain_once`. Omitting this rolls the whole
                # pipeline back and leaves the rows at `uploaded` while every
                # log line reports success.
                await session.commit()
                return stats
        finally:
            await engine.dispose()  # type: ignore[attr-defined]

    stats = _run(_drive())
    assert stats.ready == 1, f"expected one ready version, got {stats}"
    assert embedder.batches, "the embedder was never called - chunks were not embedded"

    row = _read_version(version_id)
    assert row["status"] == "ready"
    assert row["doc_status"] == "active", "an indexed version must be retrievable"
    assert row["chunks"] > 0, "READY with no chunks is a citation to nothing"


def test_chunks_carry_section_paths_and_ordinals(monkeypatch: pytest.MonkeyPatch) -> None:
    """Structure survives the pipeline.

    `parse_markdown_sections` builds a heading hierarchy; if the worker
    discarded it, excerpts would lose the context that makes a citation
    intelligible ("within five days" of what?).
    """
    from worker.ingestion_consumer import drain_versions

    version_id = _make_version()
    monkeypatch.setattr(
        "platform_core.knowledge.service.get_object", lambda key: DOC.encode("utf-8")
    )

    async def _drive() -> None:
        engine, factory = _session_factory(APP_URL)
        try:
            async with factory() as session:  # type: ignore[operator]
                await drain_versions(session, [uuid.UUID(version_id)], embedder=StubEmbedder())
                # `drain_versions` does not commit: the caller owns the
                # unit of work, same contract as `drain_ingestion_once` and
                # `inbox_consumer.drain_once`. Omitting this rolls the whole
                # pipeline back and leaves the rows at `uploaded` while every
                # log line reports success.
                await session.commit()
        finally:
            await engine.dispose()  # type: ignore[attr-defined]

    _run(_drive())

    admin = create_engine(ADMIN_URL)
    with admin.connect() as conn:
        rows = (
            conn.execute(
                text(
                    "SELECT ordinal, section_path, text, text_hash FROM chunks "
                    "WHERE document_version_id = :v ORDER BY ordinal"
                ),
                {"v": version_id},
            )
            .mappings()
            .all()
        )
    admin.dispose()

    assert rows, "no chunks written"
    ordinals = [r["ordinal"] for r in rows]
    assert ordinals == list(range(len(rows))), f"ordinals must be dense from 0, got {ordinals}"
    assert all(r["text_hash"].startswith("sha256:") for r in rows)
    assert any(r["section_path"] for r in rows), "heading paths were dropped"


def test_embeddings_are_written_so_the_vector_half_of_fusion_works(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Chunks without vectors halve retrieval and fail silently.

    `hybrid_search` unions a lexical list with a vector list; chunks with
    `embedding IS NULL` are excluded from the second, so RRF would fuse one
    list instead of two and nothing would report an error.
    """
    from worker.ingestion_consumer import drain_versions

    version_id = _make_version()
    monkeypatch.setattr(
        "platform_core.knowledge.service.get_object", lambda key: DOC.encode("utf-8")
    )

    async def _drive() -> None:
        engine, factory = _session_factory(APP_URL)
        try:
            async with factory() as session:  # type: ignore[operator]
                await drain_versions(session, [uuid.UUID(version_id)], embedder=StubEmbedder())
                # `drain_versions` does not commit: the caller owns the
                # unit of work, same contract as `drain_ingestion_once` and
                # `inbox_consumer.drain_once`. Omitting this rolls the whole
                # pipeline back and leaves the rows at `uploaded` while every
                # log line reports success.
                await session.commit()
        finally:
            await engine.dispose()  # type: ignore[attr-defined]

    _run(_drive())

    admin = create_engine(ADMIN_URL)
    with admin.connect() as conn:
        missing = conn.execute(
            text(
                "SELECT count(*) FROM chunks WHERE document_version_id = :v AND embedding IS NULL"
            ),
            {"v": version_id},
        ).scalar_one()
        total = conn.execute(
            text("SELECT count(*) FROM chunks WHERE document_version_id = :v"),
            {"v": version_id},
        ).scalar_one()
    admin.dispose()

    assert int(total) > 0
    assert int(missing) == 0, f"{missing} of {total} chunks have no embedding"


def test_search_vector_is_generated_by_the_database(monkeypatch: pytest.MonkeyPatch) -> None:
    """The tsvector column is populated without the writer naming it.

    It is `GENERATED ALWAYS AS ... STORED`, so including it in the INSERT
    would be rejected. This asserts the resulting column is non-null, which
    is what makes the lexical half of retrieval work.
    """
    from worker.ingestion_consumer import drain_versions

    version_id = _make_version()
    monkeypatch.setattr(
        "platform_core.knowledge.service.get_object", lambda key: DOC.encode("utf-8")
    )

    async def _drive() -> None:
        engine, factory = _session_factory(APP_URL)
        try:
            async with factory() as session:  # type: ignore[operator]
                await drain_versions(session, [uuid.UUID(version_id)], embedder=StubEmbedder())
                # `drain_versions` does not commit: the caller owns the
                # unit of work, same contract as `drain_ingestion_once` and
                # `inbox_consumer.drain_once`. Omitting this rolls the whole
                # pipeline back and leaves the rows at `uploaded` while every
                # log line reports success.
                await session.commit()
        finally:
            await engine.dispose()  # type: ignore[attr-defined]

    _run(_drive())

    admin = create_engine(ADMIN_URL)
    with admin.connect() as conn:
        null_vectors = conn.execute(
            text(
                "SELECT count(*) FROM chunks WHERE document_version_id = :v "
                "AND search_vector IS NULL"
            ),
            {"v": version_id},
        ).scalar_one()
    admin.dispose()
    assert int(null_vectors) == 0


# --- 2. Claim discipline --------------------------------------------------


def test_the_claim_returns_an_unquoted_content_type() -> None:
    """Pins the JSON extraction in `claim_ingestion_versions`.

    Migration 0024 returned `(metadata -> 'content_type')::text`. `->` yields
    a JSON value, and casting a JSON *string* to text keeps its double quotes,
    so the worker received `"text/markdown"` (quotes included) and
    `parse_document` rejected it as an unsupported content type:

        IngestionError: unsupported content type for parsing: "text/markdown"

    Asserting the exact string rather than a substring is the point: the
    failure was two extra characters, and `in` would have accepted the buggy
    value.
    """
    from worker.ingestion_consumer import claim_versions

    version_id = _make_version(content_type="text/markdown")

    async def _claim() -> str:
        engine, factory = _session_factory(APP_URL)
        try:
            async with factory() as session:  # type: ignore[operator]
                claimed = await claim_versions(
                    session, batch=50, version_ids=[uuid.UUID(version_id)]
                )
                await session.commit()
                match = [c for c in claimed if str(c.version_id) == version_id]
                assert match, "the seeded version was not claimed"
                return str(match[0].content_type)
        finally:
            await engine.dispose()  # type: ignore[attr-defined]

    assert _run(_claim()) == "text/markdown"


def test_a_missing_content_type_yields_an_empty_string_not_a_literal() -> None:
    """The other half of the same rule.

    A version row predating the field yields SQL NULL, which `ClaimedVersion`
    normalises to `""` (the field is typed `str`). That is the contract
    `parse_document` relies on: an empty or whitespace-only content type
    defaults to markdown, so old rows keep ingesting.

    What this must never be is the *string* `"null"` or `"None"`, which would
    fail the content-type match and turn every legacy row into a terminal
    ingestion error.
    """
    from worker.ingestion_consumer import claim_versions

    version_id = _make_version()

    async def _claim() -> object:
        engine, factory = _session_factory(APP_URL)
        try:
            async with factory() as session:  # type: ignore[operator]
                claimed = await claim_versions(
                    session, batch=50, version_ids=[uuid.UUID(version_id)]
                )
                await session.commit()
                match = [c for c in claimed if str(c.version_id) == version_id]
                assert match, "the seeded version was not claimed"
                return match[0].content_type
        finally:
            await engine.dispose()  # type: ignore[attr-defined]

    assert _run(_claim()) == ""


def test_an_api_style_upload_with_a_content_type_ingests_to_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The end-to-end path the e2e script exercises, pinned in the suite.

    Every other ingestion test seeds a version with no `content_type`, which
    takes the worker's markdown fallback and never touches the value the
    claim function returns. This one seeds it the way the API does, so a
    regression in the claim function fails here instead of only in
    `tests/e2e/e2e_ingestion_minio.py`.
    """
    from worker.ingestion_consumer import drain_versions

    version_id = _make_version(content_type="text/markdown")
    monkeypatch.setattr(
        "platform_core.knowledge.service.get_object", lambda key: DOC.encode("utf-8")
    )

    async def _drive() -> object:
        engine, factory = _session_factory(APP_URL)
        try:
            async with factory() as session:  # type: ignore[operator]
                stats = await drain_versions(
                    session, [uuid.UUID(version_id)], embedder=StubEmbedder()
                )
                await session.commit()
                return stats
        finally:
            await engine.dispose()  # type: ignore[attr-defined]

    stats = _run(_drive())
    assert stats.ready == 1, stats

    state = _read_version(version_id)
    assert state["status"] == "ready"
    assert state["chunks"] > 0


def test_a_claimed_version_is_not_claimed_twice(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two workers must get disjoint batches.

    The claim moves the row out of the claimable set inside the same
    transaction, so a second claim sees nothing. Without this, two workers
    ingest the same version and the `UNIQUE (version, ordinal)` constraint
    turns a duplicate into a failed document.
    """
    from worker.ingestion_consumer import claim_versions

    version_id = _make_version()

    async def _claim_twice() -> tuple[int, int]:
        engine, factory = _session_factory(APP_URL)
        try:
            async with factory() as session:  # type: ignore[operator]
                first = await claim_versions(session, batch=50, version_ids=[uuid.UUID(version_id)])
                await session.commit()
            async with factory() as session:  # type: ignore[operator]
                second = await claim_versions(
                    session, batch=50, version_ids=[uuid.UUID(version_id)]
                )
                await session.commit()
            return len(first), len(second)
        finally:
            await engine.dispose()  # type: ignore[attr-defined]

    first_n, second_n = _run(_claim_twice())
    assert first_n >= 1
    assert second_n == 0, "a claimed version was handed to a second worker"
    assert _read_version(version_id)["status"] == "parsing"


@pytest.fixture(autouse=True)
def isolate_work_rows() -> None:
    """Make the ingestion queues empty before each test.

    Without this, a test that inserts one version sees the claimable rows the
    *previous* test left behind (a deferred version returns to `uploaded` by
    design, so it stays claimable). Assertions like `stats.claimed == 1` then
    fail on another test's data, and the failure looks like a pipeline bug
    rather than cross-test leakage.

    The tenant and knowledge space are NOT touched - they come from the
    module-scoped `seed` and deleting them breaks every later test with a
    foreign-key violation on `documents`.
    """
    _drain_claimable_outside_this_suite()
    _purge_documents()
    yield


def _drain_claimable_outside_this_suite() -> None:
    """Settle every claimable version that this suite does not own.

    The claim function is deliberately tenant-agnostic: one bulk worker serves
    all tenants, so `claim_ingestion_versions(n)` returns whatever is queued
    in the whole database. That is correct behaviour, and it means the suite
    is only deterministic if nothing *else* is queued.

    Measured failure that motivated this: a manual probe run outside the test
    suite left one `uploaded` version in a seeded tenant, and
    `test_ingestion_produces_chunks_and_marks_ready` then failed with
    `IngestStats(claimed=2, ready=2, ...)` - a message that reads like a
    pipeline bug but was leaked state. Purging only our own two tenants was
    not enough, because the leaked row belonged to neither.

    Rows are moved to `expired` rather than deleted: this is other people's
    data (a developer running a probe, or a leftover from a prior manual
    test), and silently deleting a document version to make a test pass would
    be a destructive fix to a test-isolation problem. `expired` is a terminal
    state the state machine allows, and it is not claimable, so the queue is
    empty either way.
    """
    admin = create_engine(ADMIN_URL)
    try:
        with admin.begin() as conn:
            conn.execute(
                text(
                    """
                    UPDATE document_versions
                    SET ingestion_status = 'expired'
                    WHERE ingestion_status IN ('uploaded', 'queued_for_retry')
                      AND tenant_id NOT IN (
                          SELECT id FROM tenants WHERE slug = ANY(:s)
                      )
                    """
                ),
                {"s": [SLUG, OTHER_SLUG]},
            )
    finally:
        admin.dispose()


def _purge_documents() -> None:
    """Delete the documents/versions/chunks this suite creates.

    Deliberately NOT `_cleanup`: the tenant and knowledge space are created
    once by the module-scoped `seed` fixture, and deleting them mid-run would
    break every later test with a foreign-key violation on `documents`
    (observed). This only clears work rows.
    """
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        for table in ("chunks", "knowledge_acls", "document_versions", "documents"):
            conn.execute(
                text(
                    f"DELETE FROM {table} WHERE tenant_id IN "  # noqa: S608 - fixed table names
                    "(SELECT id FROM tenants WHERE slug = ANY(:s))"
                ),
                {"s": [SLUG, OTHER_SLUG]},
            )
    admin.dispose()


def test_empty_queue_returns_no_claims() -> None:
    from worker.ingestion_consumer import claim_versions

    async def _claim() -> int:
        engine, factory = _session_factory(APP_URL)
        try:
            async with factory() as session:  # type: ignore[operator]
                return len(await claim_versions(session, batch=5))
        finally:
            await engine.dispose()  # type: ignore[attr-defined]

    assert _run(_claim()) == 0


def test_the_claim_function_is_executable_only_by_the_app_role() -> None:
    """The SECURITY DEFINER escalation must be as narrow as it can be.

    `claim_ingestion_versions` reads `document_versions` outside RLS, which is
    the whole point - but it is a privilege escalation, so the grant matters
    as much as the body. `CREATE FUNCTION` gives EXECUTE to PUBLIC by default,
    so without the explicit REVOKE every role in the cluster could call it.

    Asserting on `proacl` rather than trying to call it as another role: the
    only other role available here is a superuser, which bypasses ACLs and
    would pass the check while proving nothing.
    """
    admin = create_engine(ADMIN_URL)
    with admin.connect() as conn:
        acl, secdef, volatility = conn.execute(
            text(
                "SELECT proacl, prosecdef, provolatile FROM pg_proc "
                "WHERE proname = 'claim_ingestion_versions'"
            )
        ).one()
    admin.dispose()

    assert secdef is True, "the claim function must be SECURITY DEFINER"
    assert volatility == "v", (
        "must be VOLATILE: PostgreSQL rejects FOR UPDATE in a non-volatile function"
    )
    assert acl is not None, "no ACL means the default PUBLIC grant was never revoked"
    entries = list(acl)
    assert entries == ["platform_app=X/platform"], (
        f"the claim function must be granted to platform_app and nobody else, got {entries}"
    )


def test_rls_hides_versions_from_the_app_role_when_unbound() -> None:
    """The measurement that forced the claim function into existence.

    `document_versions` is FORCE-RLS'd, so a `platform_app` connection with no
    `app.tenant_id` set sees zero rows. That is why the worker cannot simply
    SELECT the queue: an empty result is indistinguishable from an empty
    queue, and the worker would poll forever while every document stayed
    unindexed.
    """
    _make_version()

    admin = create_engine(ADMIN_URL)
    with admin.connect() as conn:
        as_super = conn.execute(text("SELECT count(*) FROM document_versions")).scalar_one()
    admin.dispose()

    app = create_engine(APP_URL)
    with app.connect() as conn:
        app_conn = conn.connection.driver_connection  # type: ignore[attr-defined]
        as_app = app_conn.execute("SELECT count(*) FROM document_versions").fetchone()[0]
        # Same connection, scoped: the row becomes visible, which proves the
        # zero above is RLS rather than an empty table.
        app_conn.execute(f"SELECT set_config('app.tenant_id', '{TENANT}', false)")
        scoped = app_conn.execute("SELECT count(*) FROM document_versions").fetchone()[0]
    app.dispose()

    assert int(as_super) >= 1, "fixture did not insert a version"
    assert int(as_app) == 0, "RLS should hide every row from an unbound app session"
    assert int(scoped) >= 1, "a tenant-scoped app session must see its own rows"


def test_stale_claim_is_reclaimed_and_keeps_its_queue_position(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A worker that dies mid-ingest must not strand the document.

    The row sits in PARSING forever - a legitimate state to be in - so
    nothing else transitions it and no alert fires. Recovery moves it back to
    `uploaded`; it does not go to FAILED, because nothing declared it failed.
    """
    from worker.ingestion_consumer import claim_versions, reclaim_stale_ingestion

    version_id = _make_version()

    async def _claim() -> int:
        engine, factory = _session_factory(APP_URL)
        try:
            async with factory() as session:  # type: ignore[operator]
                claimed = await claim_versions(
                    session, batch=5, version_ids=[uuid.UUID(version_id)]
                )
                await session.commit()
            # The claim is committed; the worker now "dies".
            return len(claimed)
        finally:
            await engine.dispose()  # type: ignore[attr-defined]

    assert _run(_claim()) >= 1
    assert _read_version(version_id)["status"] == "parsing"

    async def _reclaim() -> int:
        engine, factory = _session_factory(APP_URL)
        try:
            async with factory() as session:  # type: ignore[operator]
                # timeout=0 makes any in-progress row stale immediately, which
                # is the only way to exercise this without sleeping.
                count = await reclaim_stale_ingestion(session, timeout_seconds=0)
                await session.commit()
                return count
        finally:
            await engine.dispose()  # type: ignore[attr-defined]

    assert _run(_reclaim()) >= 1
    assert _read_version(version_id)["status"] == "uploaded"


def test_a_fresh_claim_is_not_reclaimed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reclaim must not steal work that is actively being processed.

    The counterpart to the test above: with the normal timeout a row claimed
    a moment ago is untouched. Getting this wrong means two workers ingest
    the same document concurrently.
    """
    from worker.ingestion_consumer import claim_versions
    from worker.ingestion_consumer import reclaim_stale_ingestion as reclaim

    version_id = _make_version()

    async def _claim_then_reclaim() -> tuple[int, int]:
        engine, factory = _session_factory(APP_URL)
        try:
            async with factory() as session:  # type: ignore[operator]
                await claim_versions(session, batch=50, version_ids=[uuid.UUID(version_id)])
                await session.commit()
            async with factory() as session:  # type: ignore[operator]
                stolen = await reclaim(session, timeout_seconds=900)
                await session.commit()
            return 1, stolen
        finally:
            await engine.dispose()  # type: ignore[attr-defined]

    _, stolen = _run(_claim_then_reclaim())
    assert stolen == 0, "reclaim stole a claim that was still fresh"


# --- 3. Failure handling --------------------------------------------------


def test_a_binary_document_fails_explicitly(monkeypatch: pytest.MonkeyPatch) -> None:
    """A format the pipeline cannot read is recorded as FAILED.

    Decoding with replacement characters would produce plausible-looking
    garbage that embeds cleanly and is undetectable downstream. Failing is
    the honest outcome, and the operator can see why.
    """
    from worker.ingestion_consumer import drain_versions

    version_id = _make_version()
    monkeypatch.setattr(
        "platform_core.knowledge.service.get_object", lambda key: b"%PDF-1.7\xb5\xb5\xb5"
    )

    async def _drive() -> object:
        engine, factory = _session_factory(APP_URL)
        try:
            async with factory() as session:  # type: ignore[operator]
                stats = await drain_versions(
                    session, [uuid.UUID(version_id)], embedder=StubEmbedder()
                )
                await session.commit()
                return stats
        finally:
            await engine.dispose()  # type: ignore[attr-defined]

    stats = _run(_drive())
    assert stats.failed == 1, f"expected a terminal failure, got {stats}"

    row = _read_version(version_id)
    assert row["status"] == "failed"
    assert row["chunks"] == 0
    assert "ingestion_error" in (row["metadata"] or {}), "the reason must be recorded"


def test_a_missing_object_fails_rather_than_retrying_forever(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A version whose object was never written cannot be ingested.

    Storage raising is classified as retryable, so the version returns to
    `uploaded` and stays pending - visible as unprocessed work rather than
    disappearing into a log line.

    `drain_ingestion_once`, not `drain_versions`, and the distinction is the
    subject of the test rather than a convenience. Deferral means the row goes
    *back to* `uploaded`, which is a claimable state - so `drain_versions`,
    whose whole contract is "loop until these ids leave the claimable set",
    would re-claim this row forever and fail with `IngestionError` after
    `max_rounds` instead of reporting the deferral it was asked about. There is
    no settled state to wait for here: a deferred row is *supposed* to still be
    claimable. The claim is narrowed to this row so the single-shot call cannot
    be starved by a busy queue (see `test_a_targeted_claim_reaches_a_row_the_
    fifo_would_starve` for why that narrowing is load-bearing).
    """
    from worker.ingestion_consumer import drain_ingestion_once

    version_id = _make_version()

    def _boom(key: str) -> bytes:
        raise RuntimeError("get_object failed: 404")

    monkeypatch.setattr("platform_core.knowledge.service.get_object", _boom)

    async def _drive() -> object:
        engine, factory = _session_factory(APP_URL)
        try:
            async with factory() as session:  # type: ignore[operator]
                stats = await drain_ingestion_once(
                    session, embedder=StubEmbedder(), version_ids=[uuid.UUID(version_id)]
                )
                await session.commit()
                return stats
        finally:
            await engine.dispose()  # type: ignore[attr-defined]

    stats = _run(_drive())
    assert stats.deferred == 1, f"a storage outage must defer, not fail: {stats}"
    assert _read_version(version_id)["status"] == "uploaded"


def test_an_embedder_outage_defers_rather_than_failing(monkeypatch: pytest.MonkeyPatch) -> None:
    """A provider outage is the dependency's fault, not the document's.

    Parking the document in FAILED would mean a human has to notice and
    requeue, for a failure that clears on its own.

    Single-shot for the same structural reason as the storage-outage test
    above: a deferral puts the row *back* into a claimable state, so
    `drain_versions` - which loops until its ids stop being claimable - would
    spin on it and raise. The narrow claim keeps the single shot honest.
    """
    from worker.ingestion_consumer import drain_ingestion_once

    version_id = _make_version()
    monkeypatch.setattr(
        "platform_core.knowledge.service.get_object", lambda key: DOC.encode("utf-8")
    )
    embedder = StubEmbedder(fail_with=RuntimeError("provider 503"))

    async def _drive() -> object:
        engine, factory = _session_factory(APP_URL)
        try:
            async with factory() as session:  # type: ignore[operator]
                stats = await drain_ingestion_once(
                    session, embedder=embedder, version_ids=[uuid.UUID(version_id)]
                )
                # `drain_ingestion_once` does not commit: the caller owns the
                # unit of work, same contract as `drain_versions` and
                # `inbox_consumer.drain_once`. Omitting this rolls the whole
                # pipeline back and leaves the rows at `uploaded` while every
                # log line reports success.
                await session.commit()
                return stats
        finally:
            await engine.dispose()  # type: ignore[attr-defined]

    stats = _run(_drive())
    assert stats.deferred == 1, f"expected a deferral, got {stats}"
    assert _read_version(version_id)["status"] == "uploaded"


def test_one_poison_document_does_not_block_the_batch(monkeypatch: pytest.MonkeyPatch) -> None:
    """Per-version isolation: a bad document must not stall the queue.

    Two versions, both hitting a failing embedder. Both are deferred in one
    cycle - the claim released, the loop continued - rather than the first
    failure aborting the batch and leaving the second unclaimed.
    """
    from worker.ingestion_consumer import drain_ingestion_once

    good = _make_version()
    bad = _make_version()

    monkeypatch.setattr(
        "platform_core.knowledge.service.get_object", lambda key: DOC.encode("utf-8")
    )

    embedder = StubEmbedder(fail_with=RuntimeError("provider 503"))

    async def _drive() -> object:
        engine, factory = _session_factory(APP_URL)
        try:
            async with factory() as session:  # type: ignore[operator]
                # `drain_ingestion_once`, not `drain_versions`: this test is
                # about what happens *within one batch*, so a caller-level
                # retry loop would hide the behaviour under test (it would
                # re-claim the deferred rows and the stats would no longer be
                # a single batch's). Both seeded versions are named so the
                # batch is exactly this test's rows.
                stats = await drain_ingestion_once(
                    session,
                    embedder=embedder,
                    batch=5,
                    version_ids=[uuid.UUID(good), uuid.UUID(bad)],
                )
                # `drain_versions` does not commit: the caller owns the
                # unit of work, same contract as `drain_ingestion_once` and
                # `inbox_consumer.drain_once`. Omitting this rolls the whole
                # pipeline back and leaves the rows at `uploaded` while every
                # log line reports success.
                await session.commit()
                return stats
        finally:
            await engine.dispose()  # type: ignore[attr-defined]

    stats = _run(_drive())
    # Both defer (the claim is released), which proves the loop continued
    # past the first failure instead of aborting the batch.
    assert stats.deferred == stats.claimed >= 2, f"batch aborted early: {stats}"
    assert stats.failed == 0
    assert _read_version(good)["status"] == "uploaded"
    assert _read_version(bad)["status"] == "uploaded"


def test_reingestion_replaces_chunks_rather_than_duplicating(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A retry is idempotent.

    Chunks carry `UNIQUE (document_version_id, ordinal)`, so an append would
    fail on the second run and make a retry look like a parse error. Deleting
    first keeps the operation idempotent, which is what makes stale-claim
    recovery safe.
    """
    from worker.ingestion_consumer import drain_versions

    version_id = _make_version()
    monkeypatch.setattr(
        "platform_core.knowledge.service.get_object", lambda key: DOC.encode("utf-8")
    )

    async def _drive() -> None:
        engine, factory = _session_factory(APP_URL)
        try:
            async with factory() as session:  # type: ignore[operator]
                await drain_versions(session, [uuid.UUID(version_id)], embedder=StubEmbedder())
                # `drain_versions` does not commit: the caller owns the
                # unit of work, same contract as `drain_ingestion_once` and
                # `inbox_consumer.drain_once`. Omitting this rolls the whole
                # pipeline back and leaves the rows at `uploaded` while every
                # log line reports success.
                await session.commit()
        finally:
            await engine.dispose()  # type: ignore[attr-defined]

    _run(_drive())
    first = _read_version(version_id)["chunks"]

    # Reopen the version for retry, as an operator would after a FAILED row.
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        conn.execute(
            text(
                "UPDATE document_versions SET ingestion_status='queued_for_retry', "
                "status='processing' WHERE id = :v"
            ),
            {"v": version_id},
        )
    admin.dispose()

    _run(_drive())
    assert _read_version(version_id)["chunks"] == first, "a retry duplicated chunks"


# --- 4. Retrieval sees the document ---------------------------------------
#
# The end-to-end property. Everything above is a component check; this is
# the one that answers "can a customer's question actually reach this
# document".


def test_an_ingested_document_is_retrievable_by_hybrid_search(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """upload -> worker -> search returns the document.

    Before the ingestion worker existed, this test could not pass for any
    query: `chunks` was always empty, so retrieval returned nothing and the
    orchestrator abstained on every knowledge question.
    """
    from platform_core.identity.tenant_context import TenantContext, apply_rls_tenant
    from platform_core.retrieval.hybrid import PrincipalScope, hybrid_search
    from worker.ingestion_consumer import drain_versions

    monkeypatch.setattr(
        "platform_core.knowledge.service.get_object", lambda key: DOC.encode("utf-8")
    )
    version_id = _make_version()
    embedder = StubEmbedder()

    async def _drive_and_search() -> dict:
        engine, factory = _session_factory(APP_URL)
        try:
            async with factory() as session:  # type: ignore[operator]
                await drain_versions(session, [uuid.UUID(version_id)], embedder=embedder)
                await session.commit()

            async with factory() as session:  # type: ignore[operator]
                await apply_rls_tenant(
                    session,
                    TenantContext(tenant_id=uuid.UUID(TENANT), actor_id=None, actor_kind="system"),
                )
                # The same deterministic embedder serves the query, so the
                # vector list is non-empty for the query too. Lexical
                # matching is what actually identifies the document here.
                hits = await hybrid_search(
                    session,
                    tenant_id=uuid.UUID(TENANT),
                    query="refund eligibility window",
                    top_k=5,
                    principal=PrincipalScope(
                        principal_types=("role",), principal_ids=("support_agent",)
                    ),
                    embedder=embedder,  # type: ignore[arg-type]
                )
                return {"hits": hits}
        finally:
            await engine.dispose()  # type: ignore[attr-defined]

    result = _run(_drive_and_search())
    hits = result["hits"]  # type: ignore[index]
    assert hits, "an ingested document was not retrievable"

    versions = {str(h.document_version_id) for h in hits}
    assert version_id in versions, f"expected {version_id} in {versions}"
    assert any("refund" in h.excerpt.lower() for h in hits)


def test_an_uploaded_but_uningested_document_is_not_retrievable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The negative case, and the reason the pipeline is fail-closed.

    A version at `uploaded` has no chunks, so it cannot appear in results.
    This is what makes the READY promotion meaningful: if an unprocessed
    document were retrievable, `ingestion_status` would be decorative.
    """
    from platform_core.identity.tenant_context import TenantContext, apply_rls_tenant
    from platform_core.retrieval.hybrid import PrincipalScope, hybrid_search

    version_id = _make_version(uri=f"doc://pending-{uuid.uuid4()}")

    async def _search() -> list:
        engine, factory = _session_factory(APP_URL)
        try:
            async with factory() as session:  # type: ignore[operator]
                await apply_rls_tenant(
                    session,
                    TenantContext(tenant_id=uuid.UUID(TENANT), actor_id=None, actor_kind="system"),
                )
                return await hybrid_search(
                    session,
                    tenant_id=uuid.UUID(TENANT),
                    query="refund eligibility window",
                    top_k=5,
                    principal=PrincipalScope(
                        principal_types=("role",), principal_ids=("support_agent",)
                    ),
                    embedder=StubEmbedder(),  # type: ignore[arg-type]
                )
        finally:
            await engine.dispose()  # type: ignore[attr-defined]

    hits = _run(_search())
    assert version_id not in {str(h.document_version_id) for h in hits}


def test_another_tenant_cannot_retrieve_the_document(monkeypatch: pytest.MonkeyPatch) -> None:
    """RLS and the tenant filter, exercised through the ingestion output.

    A cross-tenant negative test on this path is required by AGENTS.md, and
    it is the one that would catch a worker that wrote chunks under the wrong
    `tenant_id` - which would make them visible to nobody (best case) or to
    the wrong tenant (worst case).
    """
    from platform_core.identity.tenant_context import TenantContext, apply_rls_tenant
    from platform_core.retrieval.hybrid import PrincipalScope, hybrid_search
    from worker.ingestion_consumer import drain_versions

    monkeypatch.setattr(
        "platform_core.knowledge.service.get_object", lambda key: DOC.encode("utf-8")
    )
    version_id = _make_version()
    embedder = StubEmbedder()

    async def _drive_and_search() -> list:
        engine, factory = _session_factory(APP_URL)
        try:
            async with factory() as session:  # type: ignore[operator]
                await drain_versions(session, [uuid.UUID(version_id)], embedder=embedder)
                await session.commit()

            async with factory() as session:  # type: ignore[operator]
                await apply_rls_tenant(
                    session,
                    TenantContext(
                        tenant_id=uuid.UUID(OTHER_TENANT), actor_id=None, actor_kind="system"
                    ),
                )
                return await hybrid_search(
                    session,
                    tenant_id=uuid.UUID(OTHER_TENANT),
                    query="refund eligibility window",
                    top_k=5,
                    principal=PrincipalScope(
                        principal_types=("role",), principal_ids=("support_agent",)
                    ),
                    embedder=embedder,  # type: ignore[arg-type]
                )
        finally:
            await engine.dispose()  # type: ignore[attr-defined]

    hits = _run(_drive_and_search())
    assert version_id not in {str(h.document_version_id) for h in hits}


# --- 5. Targeted claim and the drain-until-settled loop -------------------


def test_a_targeted_claim_reaches_a_row_the_fifo_would_starve(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The regression this suite would have caught, had it existed.

    `claim_ingestion_versions` is a global FIFO: it takes the *oldest* N
    claimable rows anywhere. With a batch smaller than the queue depth, the
    rows past the batch never get a turn, and every round re-claims from the
    same head. Measured on a live queue: 13 claimable rows, `claim(10)`
    returned 9, and the oldest row was absent - the signature of
    `FOR UPDATE SKIP LOCKED` skipping a row another transaction holds.

    For this test to mean anything the marker must be **outside the claim
    window** the un-narrowed call can reach. `created_at` is whole seconds
    (migration 0017's INSERT trigger), so every row seeded here shares one
    ordering key and the FIFO order within it is decided by the tiebreaker,
    not by insertion order - which makes "make the marker land past the
    window" a property of the ids rather than of the insertion sequence.

    The fixture now inserts UUIDv7, matching production (`PkMixin.default_uuid`
    -> `uuid7`), so the `ORDER BY created_at, id` tiebreaker really is FIFO
    here too and "oldest first" is well defined. The marker is still re-seeded
    until it genuinely sits outside the window, because *which* seven rows the
    fixture created is a property of the test's own execution order, not
    something this assertion should assume.

    The assertion is therefore built so it holds without assuming which row the
    tiebreaker picks: the queue is made deeper than the batch, and the test
    asserts the marker is **not** in the un-narrowed window - re-seeding until
    that precondition is true - rather than asserting on a window that happens
    to contain it. What is being tested is the *narrowing*, and that is
    deterministic: a targeted claim returns its row whatever the tie order is.

    Without the fix `scripts/run_eval.py` fails with

        RuntimeError: ingesting <key> left ingestion_status='uploaded'

    for a document that is uploaded, stored and perfectly ingestable.
    """
    from worker.ingestion_consumer import claim_versions

    async def _seed_and_window() -> tuple[str, list[str]]:
        """Seed fillers + marker, return (marker, un-narrowed window).

        Retried because the precondition is a property of the tie order, which
        is arbitrary under `gen_random_uuid()`. A run that cannot produce an
        out-of-window marker is a run that cannot test anything, so it must
        fail loudly rather than assert on a window that happens to contain it.
        """
        for _attempt in range(8):
            marker = _make_version()
            fillers = [_make_version() for _ in range(6)]
            engine, factory = _session_factory(APP_URL)
            try:
                async with factory() as session:  # type: ignore[operator]
                    # batch=2 against a 7-deep queue: most of the queue is out
                    # of reach. NOTE: six filler rows minimum, not three -
                    # with `batch=2` and a naive three the window still reaches
                    # the marker often enough to flake.
                    un_narrowed = await claim_versions(session, batch=2)
                    await session.rollback()
            finally:
                await engine.dispose()  # type: ignore[attr-defined]
            ids = [str(v.version_id) for v in un_narrowed]
            if marker not in ids and set(fillers).isdisjoint(ids):
                return marker, ids
        raise AssertionError(
            "could not seed a queue where the marker sits outside a batch=2 "
            "window; the test cannot distinguish the two selectors"
        )

    marker, un_narrowed = _run(_seed_and_window())
    assert len(un_narrowed) == 2, f"expected a 2-row FIFO window, got {len(un_narrowed)}"

    async def _targeted() -> list[str]:
        engine, factory = _session_factory(APP_URL)
        try:
            async with factory() as session:  # type: ignore[operator]
                targeted = await claim_versions(session, batch=2, version_ids=[uuid.UUID(marker)])
                await session.commit()
                return [str(v.version_id) for v in targeted]
        finally:
            await engine.dispose()  # type: ignore[attr-defined]

    targeted = _run(_targeted())
    assert targeted == [marker], (
        f"a targeted claim must reach its row regardless of FIFO position, got {targeted}"
    )
    assert _read_version(marker)["status"] == "parsing"


def test_a_targeted_claim_cannot_reach_a_row_that_is_not_claimable() -> None:
    """Narrowing the candidate set must not widen the state filter.

    The claim function is SECURITY DEFINER, so the reviewer's question is
    always "what can this now be pointed at that it could not before". The
    answer must remain: only rows in a claimable state. Targeting a `ready`
    or `expired` version asks for it by id and must still get nothing.
    """
    from worker.ingestion_consumer import claim_versions

    version_id = _make_version()
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        conn.execute(
            text("UPDATE document_versions SET ingestion_status = 'expired' WHERE id = :v"),
            {"v": version_id},
        )
    admin.dispose()

    async def _claim() -> bool:
        engine, factory = _session_factory(APP_URL)
        try:
            async with factory() as session:  # type: ignore[operator]
                got = await claim_versions(session, batch=5, version_ids=[uuid.UUID(version_id)])
                await session.commit()
            return bool(got)
        finally:
            await engine.dispose()  # type: ignore[attr-defined]

    assert not _run(_claim()), "targeting must not make a non-claimable row claimable"


def test_drain_versions_settles_every_requested_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The caller's own rows are ingested, not whatever was oldest.

    `drain_ingestion_once` answers "the oldest N rows in the database", which
    is right for the worker and wrong for a caller holding specific documents.
    `drain_versions` claims repeatedly until none of the requested ids remain
    claimable, and raises rather than returning quietly if one never settles.
    """
    from worker.ingestion_consumer import drain_versions

    monkeypatch.setattr(
        "platform_core.knowledge.service.get_object",
        lambda _key: DOC.encode("utf-8"),
    )

    ids = [uuid.UUID(_make_version()) for _ in range(3)]

    async def _drain() -> int:
        engine, factory = _session_factory(APP_URL)
        try:
            async with factory() as session:  # type: ignore[operator]
                stats = await drain_versions(session, ids, embedder=StubEmbedder())
                await session.commit()
            return stats.ready
        finally:
            await engine.dispose()  # type: ignore[attr-defined]

    ready = _run(_drain())
    assert ready == 3, f"expected all three requested versions ingested, got {ready}"
    for version_id in ids:
        state = _read_version(str(version_id))
        assert state["status"] == "ready", f"{version_id} did not settle: {state}"
        assert state["chunks"] > 0


def test_drain_versions_narrows_the_claim_it_issues(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The narrowing must survive the trip through `drain_ingestion_once`.

    Found by an end-to-end run, not by the unit-level tests: `drain_versions`
    called `drain_ingestion_once`, which called `claim_versions` **without**
    the id set, so the claim stayed un-narrowed and the starvation persisted.
    The direct `claim_versions(version_ids=...)` tests all passed while the
    caller was still broken, which is exactly the gap this test closes.

    Asserting on the ids `claim_versions` actually receives is deliberate: the
    observable end state ("eventually READY") can be reached by a lucky FIFO
    ordering, so a test that only checks the outcome would pass on a machine
    with an empty queue and fail in production.
    """
    from worker import ingestion_consumer

    monkeypatch.setattr(
        "platform_core.knowledge.service.get_object",
        lambda _key: DOC.encode("utf-8"),
    )

    # A queue deeper than the batch, with the requested rows at the tail.
    for _ in range(4):
        _make_version()
    wanted = [uuid.UUID(_make_version()) for _ in range(2)]

    seen: list[list[uuid.UUID] | None] = []
    real_claim = ingestion_consumer.claim_versions

    async def _spy(session: object, *, batch: int = 5, version_ids=None):  # type: ignore[no-untyped-def]
        seen.append(list(version_ids) if version_ids else None)
        return await real_claim(session, batch=batch, version_ids=version_ids)  # type: ignore[arg-type]

    monkeypatch.setattr(ingestion_consumer, "claim_versions", _spy)

    async def _drain() -> None:
        engine, factory = _session_factory(APP_URL)
        try:
            async with factory() as session:  # type: ignore[operator]
                await ingestion_consumer.drain_versions(session, wanted, embedder=StubEmbedder())
                await session.commit()
        finally:
            await engine.dispose()  # type: ignore[attr-defined]

    _run(_drain())

    assert seen, "the drain issued no claim at all"
    assert all(ids is not None for ids in seen), (
        f"every claim the drain issues must carry the requested ids, got {seen}"
    )
    assert all(set(ids) <= set(wanted) for ids in seen if ids), (  # type: ignore[arg-type]
        f"the claim must be narrowed to the requested rows, got {seen}"
    )
    for version_id in wanted:
        assert _read_version(str(version_id))["status"] == "ready"


def test_drain_versions_raises_rather_than_returning_partial_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A caller that asked for specific documents must not be told "fine".

    `drain_ingestion_once` reports a batch summary, and a summary is exactly
    what let the original bug through: `ready=8` says nothing about the row
    the caller wanted. A requested version that could not be ingested has to
    be visible, not absorbed into a smaller number.

    Two things this test had to learn, both of which are real contracts:

    1. **An unreachable dependency defers, it does not fail.** A storage
       outage is classified retryable by design (`_Retryable`), so the row goes
       back to `uploaded` for a later cycle. The outcome to assert is
       `deferred`, and the row must still be `uploaded` rather than parked in
       FAILED for a fault that clears itself.

       The fault is injected rather than inherited from the environment. This
       test used to reach the real storage layer and pass because that layer
       was *misconfigured and always unreachable* - so it was asserting
       "deferral" against an outage while the docstring called it a missing
       object. Once the endpoint was fixed, the same call returned a real 404
       and the two conditions separated: an outage is transient, a 404 is not
       (`test_a_missing_object_fails_...` pins the other half).
    2. **`drain_versions` does not raise for a handled row.** Deferral means
       the row is *supposed* to stay claimable, so treating it as "never
       ingested" would make the loop raise on the one path that exists to
       recover. An error is reserved for an id the loop genuinely never
       reached after `max_rounds`.

    So the assertion is on the reporting contract: the outcome is attributed
    to the caller (stats *and* `handled_ids`), and the row agrees. Silence is
    impossible either way - if a future change makes it raise, the raise must
    be a real `IngestionError` naming the cause, which the except branch
    enforces.
    """
    from platform_core.knowledge.ingest import IngestionError
    from platform_core.knowledge.storage import StorageValidationError
    from worker.ingestion_consumer import drain_versions

    version_id = uuid.UUID(_make_version())

    def _dependency_down(key: str) -> bytes:
        raise StorageValidationError("get_object failed: 503")

    monkeypatch.setattr("platform_core.knowledge.service.get_object", _dependency_down)

    async def _drain() -> object:
        engine, factory = _session_factory(APP_URL)
        try:
            async with factory() as session:  # type: ignore[operator]
                out = await drain_versions(session, [version_id], embedder=StubEmbedder())
                await session.commit()
                return out
        finally:
            await engine.dispose()  # type: ignore[attr-defined]

    try:
        stats = _run(_drain())
    except Exception as exc:  # noqa: BLE001 - the other acceptable outcome
        assert isinstance(exc, IngestionError), (
            f"a non-settling drain must raise IngestionError, got {type(exc).__name__}: {exc}"
        )
        assert "never ingested" in str(exc), f"the raise must name the cause, got {exc}"
        return

    assert stats.claimed == 1, f"the requested row must be claimed, got {stats}"
    assert stats.deferred == 1, f"a storage fault must defer, not fail: {stats}"
    assert stats.ready == 0, f"an unreadable document must not be reported ready: {stats}"
    assert version_id in set(stats.handled_ids), (
        f"the outcome must be attributed to the requested id, got {stats.handled_ids}"
    )
    assert _read_version(str(version_id))["status"] == "uploaded", (
        "a retryable fault must leave the row claimable for a later cycle"
    )


def test_a_missing_object_fails_instead_of_retrying_forever(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 404 is permanent; an outage is not, and they used to be one thing.

    An upload whose bytes never landed leaves exactly this row behind: the API
    registers the document, then the storage write fails, and the version row
    stays claimable. Every cycle re-read the key, got a 404, called it
    retryable and released the claim - measured at roughly six attempts per
    second, per stuck document, with no end. Nothing about a 404 clears on its
    own.

    `failed` is the honest terminal state, and it is visible: the document list
    shows it, where the loop only ever showed up in a log nobody reads.
    """
    from platform_core.knowledge.storage import ObjectNotFound
    from worker.ingestion_consumer import drain_versions

    version_id = _make_version()

    def _gone(key: str) -> bytes:
        raise ObjectNotFound(f"get_object failed: 404 for {key}")

    monkeypatch.setattr("platform_core.knowledge.service.get_object", _gone)
    embedder = StubEmbedder()

    async def _drive() -> object:
        engine, factory = _session_factory(APP_URL)
        try:
            async with factory() as session:  # type: ignore[operator]
                stats = await drain_versions(session, [uuid.UUID(version_id)], embedder=embedder)
                await session.commit()
                return stats
        finally:
            await engine.dispose()  # type: ignore[attr-defined]

    stats = _run(_drive())
    assert stats.deferred == 0, f"a 404 must not be deferred, got {stats}"
    assert stats.failed == 1, f"a 404 must be a terminal failure, got {stats}"
    assert embedder.batches == [], "nothing should be embedded without content"

    row = _read_version(version_id)
    assert row["status"] == "failed", (
        "a missing object cannot clear itself; the row must not stay claimable"
    )
