"""End-to-end against the real MinIO container on 19000.

Exercises the true path: object uploaded to MinIO -> worker downloads it ->
chunks written -> hybrid_search recalls the chunk. No storage stub.
"""

import asyncio
import os
import sys
import uuid

os.environ["APP_OBJECT_STORAGE_ENDPOINT"] = "localhost:19000"
os.environ["APP_OBJECT_STORAGE_ACCESS_KEY"] = "minioadmin"
os.environ["APP_OBJECT_STORAGE_SECRET_KEY"] = "minioadmin"
os.environ["APP_OBJECT_STORAGE_BUCKET"] = "documents"
os.environ["APP_OBJECT_STORAGE_SECURE"] = "false"

sys.path.insert(0, "apps/api/src")
sys.path.insert(0, "apps/worker/src")
sys.path.insert(0, "packages/contracts/src")
sys.path.insert(0, "packages/policy/src")
sys.path.insert(0, "packages/observability/src")

from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from platform_core.identity.tenant_context import TenantContext, apply_rls_tenant  # noqa: E402
from platform_core.knowledge import service  # noqa: E402
from platform_core.llm.provider import EmbeddingResult  # noqa: E402
from platform_core.retrieval.hybrid import hybrid_search  # noqa: E402

PLATFORM_URL = "postgresql+psycopg://platform:platform@localhost:5435/platform"
APP_URL = "postgresql+psycopg://platform_app:platform_app@localhost:5435/platform"

DOC = b"""# Refund policy

## Eligibility window

A customer may request a refund within 30 calendar days of purchase.
Requests after that window require a supervisor override.

## Processing time

Approved refunds settle in 5 to 7 business days on the original card.
"""

EMBED_DIM = 1536


class DeterministicProvider:
    """Satisfies the embedding-provider shape the worker calls.

    The worker needs `embed(texts) -> EmbeddingResult` (the batch interface);
    `hybrid_search` needs `embed_query(query) -> list[float]` (the `Embedder`
    protocol). They are different interfaces on purpose - the retrieval path
    must not be able to request a batch - so this class provides the batch
    side and `ProviderEmbedder` adapts it to the query side below.
    """

    async def embed(self, texts: list[str], *, model: str | None = None) -> EmbeddingResult:
        return EmbeddingResult(
            vectors=[[1.0] + [0.0] * (EMBED_DIM - 1) for _ in texts],
            model="deterministic-e2e",
            dimensions=EMBED_DIM,
        )


class DeterministicEmbedder:
    """Same vector for query and document, so recall is driven by lexical match."""

    @property
    def dimensions(self) -> int:
        return EMBED_DIM

    async def embed_query(self, _text: str) -> list[float]:
        return [1.0] + [0.0] * (EMBED_DIM - 1)


async def main() -> None:
    engine = create_async_engine(PLATFORM_URL)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    # --- 1. set up a tenant/space we own ---------------------------------
    # Create the tenant as the superuser first, then the space as the *app*
    # role with the tenant bound: `knowledge_spaces` is FORCE RLS, so an
    # insert from an unbound connection is refused by WITH CHECK. Doing both
    # from one superuser session would also work, but it would not exercise
    # the same path the API takes.
    slug = f"e2e-minio-{uuid.uuid4().hex[:8]}"
    async with factory() as s:
        tenant_id = (
            await s.execute(
                text(
                    "INSERT INTO tenants (id, slug, name) "
                    "VALUES (gen_random_uuid(), :slug, :slug) RETURNING id"
                ),
                {"slug": slug},
            )
        ).scalar()

    app_engine = create_async_engine(APP_URL)
    app_factory = async_sessionmaker(app_engine, expire_on_commit=False)
    async with app_factory() as s:
        await apply_rls_tenant(
            s, TenantContext(tenant_id=tenant_id, actor_id=None, actor_kind="system")
        )
        space_id = (
            await s.execute(
                text(
                    "INSERT INTO knowledge_spaces (id, tenant_id, name) "
                    "VALUES (gen_random_uuid(), :t, 'e2e') RETURNING id"
                ),
                {"t": tenant_id},
            )
        ).scalar()
        await s.commit()
    print(f"tenant={tenant_id} space={space_id}")

    # --- 2. upload through the real service (row + real MinIO object) -----
    async with app_factory() as s:
        await apply_rls_tenant(
            s, TenantContext(tenant_id=tenant_id, actor_id=None, actor_kind="system")
        )
        created = await service.create_document(
            s,
            tenant_id=tenant_id,
            space_id=space_id,
            title="E2E refund policy",
            canonical_uri=f"doc://e2e-{uuid.uuid4()}",
            data=DOC,
            content_type="text/markdown",
            filename="refund.md",
            classification="internal",
            version_label="v1",
        )
        # The row must be committed before the worker can see it: the worker
        # claims from a different connection, and an uncommitted version is
        # invisible to it. The API path commits before uploading for the same
        # reason - a row without an object is recoverable, the reverse is not.
        await s.commit()
        uri = service.upload_object(created.object_key, DOC, "text/markdown")
        print(f"uploaded version={created.version_id} uri={uri}")

    # Prove the bytes really are in MinIO by reading them back over HTTP.
    fetched = service.get_object(created.object_key)
    assert fetched == DOC, "MinIO did not return what we uploaded"
    print(f"minio round-trip OK ({len(fetched)} bytes)")

    # --- 3. run the real worker against it -------------------------------
    from worker.ingestion_consumer import drain_ingestion_once

    try:
        async with app_factory() as s:
            stats = await drain_ingestion_once(s, embedder=DeterministicProvider(), batch=5)
            await s.commit()
            print(f"worker stats: {stats}")
    finally:
        await app_engine.dispose()

    # --- 4. verify chunks landed and search recalls ----------------------
    async with factory() as s:
        await apply_rls_tenant(
            s, TenantContext(tenant_id=tenant_id, actor_id=None, actor_kind="system")
        )
        row = (
            await s.execute(
                text(
                    "SELECT ingestion_status, status FROM document_versions WHERE id = :v"
                ),
                {"v": created.version_id},
            )
        ).mappings().one()
        n_chunks = (
            await s.execute(
                text("SELECT count(*) FROM chunks WHERE document_version_id = :v"),
                {"v": created.version_id},
            )
        ).scalar()
        n_vec = (
            await s.execute(
                text(
                    "SELECT count(*) FROM chunks "
                    "WHERE document_version_id = :v AND embedding IS NOT NULL"
                ),
                {"v": created.version_id},
            )
        ).scalar()
        print(f"ingestion_status={row['ingestion_status']} status={row['status']}")
        print(f"chunks={n_chunks} with_embedding={n_vec}")

        from platform_core.retrieval.hybrid import PrincipalScope

        hits = await hybrid_search(
            s,
            tenant_id=tenant_id,
            query="refund eligibility window",
            top_k=5,
            principal=PrincipalScope(
                principal_types=("tenant",), principal_ids=(str(tenant_id),)
            ),
            embedder=DeterministicEmbedder(),
        )
        print(f"hybrid_search hits={len(hits)}")
        for h in hits[:3]:
            print(f"    sections={h.section_path} score={h.score:.4f} {h.excerpt[:60]!r}")

    await engine.dispose()
    assert stats.ready == 1, stats
    assert n_chunks > 0 and n_vec == n_chunks, (n_chunks, n_vec)
    assert hits, "an indexed version must be recallable"
    print("\nE2E OK: upload -> MinIO -> worker -> chunks -> hybrid_search recall")


asyncio.run(main(), loop_factory=asyncio.SelectorEventLoop)
