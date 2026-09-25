"""Retention must erase bytes, not only repaint the index row.

`sweep_expired_data` transitions a SUPERSEDED document version to EXPIRED and
stops there. Retrieval then stops serving it, which looks like compliance and
reads like compliance in every dashboard that counts rows by status - while the
uploaded document is still sitting in the bucket, readable by anyone holding
the key, forever. A promise to erase something, kept only in the index.

These tests run against live Postgres and a live S3 endpoint for the same
reason the storage lifecycle suite does: a delete that silently no-ops returns
success, so "the function raised nothing" proves nothing. The bytes have to be
absent from the endpoint at the end.

Ids follow the `0190c000-...` block used by `test_retention_sweep.py`; `e5`
onward were free when this file was written, and the suite carries a hygiene
test that fails if one id ever names two slugs.
"""

import os
import time
import uuid

import pytest
from sqlalchemy import create_engine, text

pytestmark = pytest.mark.integration

ADMIN_URL = os.environ.get(
    "APP_ADMIN_DATABASE_URL",
    "postgresql+psycopg://platform:platform@localhost:5435/platform",
)

S3_ENDPOINT = os.environ.get("APP_S3_ENDPOINT")
S3_BUCKET = os.environ.get("APP_S3_BUCKET", "documents")

if not S3_ENDPOINT:
    pytest.skip(
        "APP_S3_ENDPOINT is not set - byte erasure must be verified against a live endpoint",
        allow_module_level=True,
    )

TENANT = "0190c000-0000-7000-8000-0000000000e5"
SPACE = "0190c000-0000-7000-8000-0000000000e6"
DOC = "0190c000-0000-7000-8000-0000000000ea"

NOW = int(time.time())
DAY = 86400


def _run(coro):
    import asyncio

    return asyncio.run(coro, loop_factory=asyncio.SelectorEventLoop)


APP_URL = "postgresql+psycopg://platform_app:platform_app@localhost:5435/platform"


async def _with_rls(fn):
    """Run `fn(session)` in one tenant-bound transaction.

    The same shape as `test_retention_sweep.py`: RLS is set as a local
    configuration parameter on the session, so these tests exercise what the
    worker actually does rather than an admin connection that can see
    everything. Passing the session in is also what lets the erasure and the
    audit stamp commit atomically.
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from platform_core.db import create_engine as app_engine

    engine = app_engine(APP_URL)
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            await session.execute(
                text("SELECT set_config('app.tenant_id', :t, true)"), {"t": TENANT}
            )
            result = await fn(session)
            await session.commit()
            return result
    finally:
        await engine.dispose()


async def _erase_with(storage, *, now: int = NOW):
    from platform_core.evaluation.pii import erase_expired_objects

    async def bound(session):
        return await erase_expired_objects(session, storage, tenant_id=uuid.UUID(TENANT), now=now)

    return await _with_rls(bound)


async def _reconcile_with(storage):
    from platform_core.evaluation.pii import reconcile_objects

    async def bound(session):
        return await reconcile_objects(session, storage, tenant_id=uuid.UUID(TENANT))

    return await _with_rls(bound)


def _storage():
    from platform_core.knowledge.storage import MinioStorage

    engine = MinioStorage(endpoint=S3_ENDPOINT, bucket=S3_BUCKET)
    engine.ensure_bucket()
    return engine


def _version_row(version_id: str, *, status: str, expires_at: int | None, key: str) -> dict:
    return {
        "id": version_id,
        "t": TENANT,
        "doc": DOC,
        "label": f"v-{version_id[-4:]}",
        "status": status,
        "expires": expires_at,
        "uri": key,
    }


_VERSION_INSERT = (
    "INSERT INTO document_versions "
    "(id, tenant_id, document_id, version_label, content_hash, status, "
    " object_uri, expires_at) "
    "VALUES (:id, :t, :doc, :label, 'sha256:test', :status, :uri, :expires)"
)


@pytest.fixture(scope="module", autouse=True)
def seed_tenant():
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO tenants (id, slug, name, status) VALUES "
                "(:id, 'erase-t', 'erase', 'active') ON CONFLICT (slug) DO NOTHING"
            ),
            {"id": TENANT},
        )
        conn.execute(
            text(
                "INSERT INTO knowledge_spaces (id, tenant_id, name) VALUES "
                "(:id, :t, 'Erase Space') ON CONFLICT DO NOTHING"
            ),
            {"id": SPACE, "t": TENANT},
        )
        conn.execute(
            text(
                "INSERT INTO documents (id, tenant_id, space_id, title, canonical_uri) VALUES "
                "(:id, :t, :s, 'Erase Doc', 'minio://erase/doc.pdf') "
                "ON CONFLICT DO NOTHING"
            ),
            {"id": DOC, "t": TENANT, "s": SPACE},
        )
    yield
    with admin.begin() as conn:
        conn.execute(text("DELETE FROM document_versions WHERE tenant_id = :t"), {"t": TENANT})
        conn.execute(text("DELETE FROM documents WHERE tenant_id = :t"), {"t": TENANT})
        conn.execute(text("DELETE FROM knowledge_spaces WHERE tenant_id = :t"), {"t": TENANT})
        conn.execute(text("DELETE FROM tenants WHERE id = :t"), {"t": TENANT})
    admin.dispose()


@pytest.fixture(autouse=True)
def clean_rows():
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        conn.execute(text("DELETE FROM document_versions WHERE tenant_id = :t"), {"t": TENANT})
    yield
    admin.dispose()


def _insert(*rows: dict) -> None:
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        for row in rows:
            conn.execute(text(_VERSION_INSERT), row)
    admin.dispose()


# What `service.py` writes to `DocumentVersion.status` once ingestion
# completes. `IngestionStatus` has no such member: that enum drives
# `ingestion_status`, and the two vocabularies only overlap on 'expired'.
VERSION_ACTIVE = "active"


def _storage_cleanup(bucket: str) -> None:
    """Empty a scratch bucket so the probe does not accumulate."""
    from platform_core.knowledge.storage import MinioStorage

    scratch = MinioStorage(endpoint=S3_ENDPOINT, bucket=bucket)
    for key in scratch.list_objects(prefix=""):
        scratch.delete_object(key)


def _status_of(version_id: str) -> str | None:
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        row = conn.execute(
            text("SELECT status FROM document_versions WHERE id = :v"), {"v": version_id}
        ).first()
    admin.dispose()
    return str(row[0]) if row else None


def _bytes_deleted_at(version_id: str) -> int | None:
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        row = conn.execute(
            text("SELECT bytes_deleted_at FROM document_versions WHERE id = :v"),
            {"v": version_id},
        ).first()
    admin.dispose()
    return int(row[0]) if row and row[0] is not None else None


def test_expiring_a_version_erases_the_uploaded_bytes() -> None:
    """The finding itself. EXPIRED used to mean the row changed colour.

    Asserted by asking the endpoint whether the object is still there, because
    a `delete_object` that silently no-ops would leave this test green if it
    only checked that the call returned.
    """
    from platform_core.knowledge.models import IngestionStatus

    storage = _storage()
    version_id, key = str(uuid.uuid4()), f"{TENANT}/{uuid.uuid4()}/contract.pdf"
    storage.put_object(key, b"a signed contract the tenant asked us to forget", "application/pdf")
    _insert(
        _version_row(
            version_id, status=IngestionStatus.EXPIRED.value, expires_at=NOW - DAY, key=key
        )
    )

    assert storage.object_exists(key) is True, "fixture did not upload"

    _run(_erase_with(storage))

    assert storage.object_exists(key) is False, "EXPIRED row but the bytes are still in the bucket"


def test_erasing_is_idempotent_and_never_raises_on_an_absent_object() -> None:
    """The worker retries and the endpoint may already have lost the object.

    S3 answers 204 whether or not anything was there. Raising here would wedge
    the retry loop on a job that has already done its work - the failure would
    be permanent and unfixable by retrying.
    """
    from platform_core.knowledge.models import IngestionStatus

    storage = _storage()
    version_id, key = str(uuid.uuid4()), f"{TENANT}/{uuid.uuid4()}/ghost.pdf"
    storage.put_object(key, b"x", "application/pdf")
    _insert(
        _version_row(
            version_id, status=IngestionStatus.EXPIRED.value, expires_at=NOW - DAY, key=key
        )
    )

    _run(_erase_with(storage))
    after_first = _bytes_deleted_at(version_id)
    assert after_first is not None, "first pass did not stamp the deletion"

    _run(_erase_with(storage))
    assert _bytes_deleted_at(version_id) == after_first, "second pass rewrote the audit stamp"


def test_an_active_version_is_never_erased() -> None:
    """Retention must not destroy live knowledge.

    This is the dangerous direction: an erasure that is too eager deletes the
    corpus the tenant is actively searching, and unlike leaking expired bytes
    there is no second pass that can put it back.
    """

    storage = _storage()
    version_id, key = str(uuid.uuid4()), f"{TENANT}/{uuid.uuid4()}/live.pdf"
    storage.put_object(key, b"in force", "application/pdf")
    _insert(_version_row(version_id, status=VERSION_ACTIVE, expires_at=None, key=key))

    counts = _run(_erase_with(storage))

    assert counts["objects_erased"] == 0, counts
    assert storage.object_exists(key) is True, "erased a version that is still in force"


def test_reconciliation_removes_an_object_no_row_refers_to() -> None:
    """The other half of the ledger: bytes with no index entry at all.

    These come from an upload that wrote to storage and then failed before the
    row committed. Nothing else can ever find them - no row, no sweep, no
    status - so the only way they get discovered is by walking the prefix.
    """
    storage = _storage()
    orphan_key = f"{TENANT}/{uuid.uuid4()}/never-registered.pdf"
    storage.put_object(orphan_key, b"upload that died before commit", "application/pdf")

    report = _run(_reconcile_with(storage))

    assert report["orphan_objects_removed"] >= 1, report
    assert storage.object_exists(orphan_key) is False, "orphan survived reconciliation"


def test_reconciliation_reports_a_missing_object_rather_than_deleting_the_row() -> None:
    """A row whose bytes vanished is evidence, not garbage.

    Deleting that row would be convenient - it stops the report firing - and it
    would also destroy the only record that we owe the tenant an erasure we
    cannot prove happened. So the row stays and the report names it, which is
    what makes the difference between "deleted" and "missing" visible.
    """

    storage = _storage()
    version_id, key = str(uuid.uuid4()), f"{TENANT}/{uuid.uuid4()}/vanished.pdf"
    _insert(_version_row(version_id, status=VERSION_ACTIVE, expires_at=None, key=key))
    # Deliberately never uploaded: row points at bytes that were never there.

    report = _run(_reconcile_with(storage))

    assert report["rows_missing_object"] >= 1, report
    assert _status_of(version_id) is not None, "reconciliation deleted the evidence row"


def test_tenant_prefixes_are_not_crossed() -> None:
    """Reconciliation walks one tenant's prefix.

    An unprefixed walk would enumerate every tenant's objects from a job that
    has no business reading any of them, and would then be one bug away from
    deleting them.
    """
    storage = _storage()
    other = "0190c000-0000-7000-8000-0000000000eb"
    foreign_key = f"{other}/{uuid.uuid4()}/belongs-to-someone-else.pdf"
    storage.put_object(foreign_key, b"another tenant's document", "application/pdf")

    try:
        _run(_reconcile_with(storage))
        assert storage.object_exists(foreign_key) is True, (
            "reconciliation reached into another tenant's prefix"
        )
    finally:
        storage.delete_object(foreign_key)


def test_a_versioned_bucket_is_never_certified_as_erased() -> None:
    """The failure this guard exists for, measured rather than imagined.

    On a versioned bucket a DELETE writes a delete marker: the current version
    404s, which is exactly what `object_exists` reports, while the earlier
    versions stay stored and readable by `version_id`. An erasure pass that
    trusted that check would stamp `bytes_deleted_at` and certify - in a column
    an auditor reads - that a document the tenant asked us to forget had been
    erased, while its bytes were one `version_id` away from being read back.

    So the pass refuses to stamp at all. The row stays unproven, the count
    shows as failed, and the next cycle tries again: a wrong alarm on a
    misconfigured bucket is recoverable, a false compliance record is not.
    """
    from platform_core.knowledge.models import IngestionStatus
    from platform_core.knowledge.storage import MinioStorage

    bucket = f"versioned-{uuid.uuid4().hex[:8]}"
    storage = MinioStorage(endpoint=S3_ENDPOINT, bucket=bucket)
    storage.ensure_bucket()
    storage.set_bucket_versioning(True)
    try:
        assert storage.bucket_versioning_enabled() is True, "precondition not established"

        version_id, key = str(uuid.uuid4()), f"{TENANT}/{uuid.uuid4()}/kept.pdf"
        storage.put_object(key, b"a document we were told to forget", "application/pdf")
        _insert(
            _version_row(
                version_id,
                status=IngestionStatus.EXPIRED.value,
                expires_at=NOW - DAY,
                key=key,
            )
        )

        counts = _run(_erase_with(storage))

        assert counts["objects_erased"] == 0, counts
        assert counts["objects_failed"] == 1, counts
        assert _bytes_deleted_at(version_id) is None, (
            "recorded a completed erasure on a bucket where the bytes survive"
        )
    finally:
        storage.set_bucket_versioning(False)
        _storage_cleanup(bucket)


def test_an_unversioned_bucket_does_not_pay_for_the_check() -> None:
    """The guard must not disable erasure on a correctly configured bucket.

    A safety check that fires on every healthy deployment trains people to turn
    it off. This is the negative case: same code path, versioning off, erasure
    proceeds.
    """
    from platform_core.knowledge.models import IngestionStatus

    storage = _storage()
    assert storage.bucket_versioning_enabled() is False, "the shared bucket is versioned"

    version_id, key = str(uuid.uuid4()), f"{TENANT}/{uuid.uuid4()}/ordinary.pdf"
    storage.put_object(key, b"expired as usual", "application/pdf")
    _insert(
        _version_row(
            version_id, status=IngestionStatus.EXPIRED.value, expires_at=NOW - DAY, key=key
        )
    )

    counts = _run(_erase_with(storage))
    assert counts["objects_erased"] == 1, counts
    assert _bytes_deleted_at(version_id) is not None
