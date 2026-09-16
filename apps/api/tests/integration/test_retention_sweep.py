"""Integration tests: retention sweep (ticket 36, docs/security.md).

This suite exists because `sweep_expired_data` filtered
`DocumentVersion.created_at` - a column that exists on neither the model nor
the schema. Every run would have raised `UndefinedColumn`, and nothing
caught it because the function had no test that executed it. This is the
third instance of the same defect class in this codebase (after
`aggregate_quality_metrics`), so the first test here is deliberately blunt:
run the sweep against real Postgres and require it to complete.

The rest pin the semantics that make the sweep safe rather than merely
runnable:

- a SUPERSEDED version past retention becomes EXPIRED (retrieval stops
  serving it) - the documented retention behaviour;
- a SUPERSEDED version *inside* retention is left alone, so rollback still
  works;
- an ACTIVE version is never touched, because retention must not silently
  delete live knowledge;
- the prune counts are real numbers, not zeros from a no-op.
"""

import os
import time
import uuid

import pytest
from sqlalchemy import create_engine, text

from platform_core.evaluation.pii import RetentionPolicy, sweep_expired_data

pytestmark = pytest.mark.integration

ADMIN_URL = os.environ.get(
    "APP_ADMIN_DATABASE_URL",
    "postgresql+psycopg://platform:platform@localhost:5435/platform",
)
APP_URL = "postgresql+psycopg://platform_app:platform_app@localhost:5435/platform"

TENANT = "0190c000-0000-7000-8000-0000000000e1"
SPACE = "0190c000-0000-7000-8000-0000000000e2"
DOC = "0190c000-0000-7000-8000-0000000000e3"

# Dead letter rows have a nullable connector_id, so no connector fixture is
# needed to insert one.
DL_ID = "0190c000-0000-7000-8000-0000000000e4"

NOW = int(time.time())
DAY = 86400


def _run(coro):
    import asyncio

    return asyncio.run(coro, loop_factory=asyncio.SelectorEventLoop)


def _version_row(version_id: str, *, status: str, expires_at: int | None) -> dict:
    return {
        "id": version_id,
        "t": TENANT,
        "doc": DOC,
        "label": f"v-{version_id[-4:]}",
        "status": status,
        "expires": expires_at,
        "uri": f"minio://{TENANT}/documents/{version_id}.pdf",
    }


_VERSION_INSERT = (
    "INSERT INTO document_versions "
    "(id, tenant_id, document_id, version_label, content_hash, status, "
    " object_uri, expires_at) "
    "VALUES (:id, :t, :doc, :label, 'sha256:test', :status, :uri, :expires)"
)

_DL_INSERT = (
    "INSERT INTO dead_letter_items "
    "(id, tenant_id, resource_type, operation, operation_digest, error_code, "
    " status, created_at, resolved_at) "
    "VALUES (:id, :t, 'connector', 'sync', 'digest', 'E_TEST', 'resolved', :created, :resolved)"
)


@pytest.fixture(scope="module", autouse=True)
def seed_tenant():
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO tenants (id, slug, name, status) VALUES "
                "(:id, 'retention-t', 'retention', 'active') ON CONFLICT (slug) DO NOTHING"
            ),
            {"id": TENANT},
        )
        conn.execute(
            text(
                "INSERT INTO knowledge_spaces (id, tenant_id, name) VALUES "
                "(:id, :t, 'Retention Space') ON CONFLICT DO NOTHING"
            ),
            {"id": SPACE, "t": TENANT},
        )
        conn.execute(
            text(
                "INSERT INTO documents (id, tenant_id, space_id, title, canonical_uri) VALUES "
                "(:id, :t, :s, 'Retention Doc', 'minio://retention/doc.pdf') "
                "ON CONFLICT DO NOTHING"
            ),
            {"id": DOC, "t": TENANT, "s": SPACE},
        )
    yield
    with admin.begin() as conn:
        conn.execute(text("DELETE FROM dead_letter_items WHERE tenant_id = :t"), {"t": TENANT})
        conn.execute(text("DELETE FROM document_versions WHERE tenant_id = :t"), {"t": TENANT})
        conn.execute(text("DELETE FROM documents WHERE tenant_id = :t"), {"t": TENANT})
        conn.execute(text("DELETE FROM knowledge_spaces WHERE tenant_id = :t"), {"t": TENANT})
        conn.execute(text("DELETE FROM tenants WHERE id = :t"), {"t": TENANT})
    admin.dispose()


@pytest.fixture(autouse=True)
def clean_rows():
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        conn.execute(text("DELETE FROM dead_letter_items WHERE tenant_id = :t"), {"t": TENANT})
        conn.execute(text("DELETE FROM document_versions WHERE tenant_id = :t"), {"t": TENANT})
    yield
    admin.dispose()


def _insert_versions(*rows: dict) -> None:
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        for row in rows:
            conn.execute(text(_VERSION_INSERT), row)
    admin.dispose()


def _insert_dead_letter(*, resolved_at: int) -> None:
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        conn.execute(
            text(_DL_INSERT),
            {
                "id": DL_ID,
                "t": TENANT,
                "created": resolved_at - 10,
                "resolved": resolved_at,
            },
        )
    admin.dispose()


async def _sweep(*, now: int, policy: RetentionPolicy | None = None) -> dict[str, int]:
    from platform_core.db import create_engine as app_engine

    engine = app_engine(APP_URL)
    try:
        from sqlalchemy.ext.asyncio import async_sessionmaker

        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            await session.execute(
                text("SELECT set_config('app.tenant_id', :t, true)"), {"t": TENANT}
            )
            counts = await sweep_expired_data(
                session,
                tenant_id=uuid.UUID(TENANT),
                now=now,
                policy=policy or RetentionPolicy(),
            )
            await session.commit()
            return counts
    finally:
        await engine.dispose()


def _status_of(version_id: str) -> str | None:
    admin = create_engine(ADMIN_URL)
    try:
        with admin.begin() as conn:
            row = conn.execute(
                text("SELECT status FROM document_versions WHERE id = :id"),
                {"id": version_id},
            ).first()
            return row[0] if row else None
    finally:
        admin.dispose()


# --- The regression this suite was written for ---


def test_sweep_executes_against_real_schema() -> None:
    """The sweep must not reference a column that does not exist.

    `DocumentVersion.created_at` was the original bug: a plpgsql would have
    raised UndefinedColumn on every invocation. Running the real statement is
    the only thing that catches it.
    """
    _insert_versions(
        _version_row(
            "0190c000-0000-7000-8000-0000000000f1",
            status="superseded",
            expires_at=NOW - 60 * DAY,
        )
    )

    counts = _run(_sweep(now=NOW))

    assert set(counts) == {
        "document_versions_expired",
        "dead_letters_pruned",
        "inbox_events_pruned",
    }


def test_timestamp_columns_referenced_by_sweep_exist() -> None:
    """Guard the exact columns, so a rename cannot silently reintroduce it."""
    admin = create_engine(ADMIN_URL)
    expected = {
        "document_versions": "expires_at",
        "dead_letter_items": "resolved_at",
        "inbox_events": "received_at",
    }
    try:
        with admin.begin() as conn:
            for table, column in expected.items():
                found = conn.execute(
                    text(
                        "SELECT 1 FROM information_schema.columns "
                        "WHERE table_name = :t AND column_name = :c"
                    ),
                    {"t": table, "c": column},
                ).first()
                assert found, f"{table}.{column} is missing - the sweep filters on it"
    finally:
        admin.dispose()


# --- Retention semantics ----------------------------------------------------


def test_superseded_version_past_retention_becomes_expired() -> None:
    """The documented lifecycle: superseded -> expired, so retrieval stops."""
    version_id = "0190c000-0000-7000-8000-0000000000f2"
    # expires_at 40 days ago, retention 30 days -> past retention.
    _insert_versions(_version_row(version_id, status="superseded", expires_at=NOW - 40 * DAY))

    counts = _run(_sweep(now=NOW))

    assert counts["document_versions_expired"] == 1
    assert _status_of(version_id) == "expired"


def test_superseded_version_inside_retention_is_kept_for_rollback() -> None:
    """Retention must not expire something a rollback may still need."""
    version_id = "0190c000-0000-7000-8000-0000000000f3"
    # expires_at 5 days ago but retention is 30 days -> still inside.
    _insert_versions(_version_row(version_id, status="superseded", expires_at=NOW - 5 * DAY))

    counts = _run(_sweep(now=NOW))

    assert counts["document_versions_expired"] == 0
    assert _status_of(version_id) == "superseded"


def test_active_version_is_never_expired_by_retention() -> None:
    """Live knowledge is not deleted by a retention sweep."""
    version_id = "0190c000-0000-7000-8000-0000000000f4"
    _insert_versions(_version_row(version_id, status="active", expires_at=NOW - 400 * DAY))

    counts = _run(_sweep(now=NOW))

    assert counts["document_versions_expired"] == 0
    assert _status_of(version_id) == "active"


def test_superseded_version_without_expiry_is_left_alone() -> None:
    """No expiry means no retention clock; the sweep must not guess one.

    A NULL `expires_at` is how an open-ended document is represented, so
    treating NULL as 'expired long ago' would delete live-adjacent content.
    """
    version_id = "0190c000-0000-7000-8000-0000000000f5"
    _insert_versions(_version_row(version_id, status="superseded", expires_at=None))

    counts = _run(_sweep(now=NOW))

    assert counts["document_versions_expired"] == 0
    assert _status_of(version_id) == "superseded"


def test_setting_a_shorter_policy_expires_more() -> None:
    """The policy is honoured, not hardcoded."""
    version_id = "0190c000-0000-7000-8000-0000000000f6"
    _insert_versions(_version_row(version_id, status="superseded", expires_at=NOW - 10 * DAY))

    assert _run(_sweep(now=NOW))["document_versions_expired"] == 0
    assert _run(_sweep(now=NOW, policy=RetentionPolicy(superseded_version_days=5)))[
        "document_versions_expired"
    ] == 1
    assert _status_of(version_id) == "expired"


def test_resolved_dead_letter_past_retention_is_pruned() -> None:
    """The prune count is a real number, and the row is really gone."""
    _insert_dead_letter(resolved_at=NOW - 20 * DAY)

    counts = _run(_sweep(now=NOW))

    assert counts["dead_letters_pruned"] == 1
    admin = create_engine(ADMIN_URL)
    try:
        with admin.begin() as conn:
            remaining = conn.execute(
                text("SELECT count(*) FROM dead_letter_items WHERE id = :id"), {"id": DL_ID}
            ).scalar()
        assert remaining == 0
    finally:
        admin.dispose()


def test_recent_resolved_dead_letter_is_kept() -> None:
    """Pruning is age-gated, not unconditional."""
    _insert_dead_letter(resolved_at=NOW - 1 * DAY)

    counts = _run(_sweep(now=NOW))

    assert counts["dead_letters_pruned"] == 0


def test_sweep_is_tenant_scoped() -> None:
    """A sweep for one tenant must not touch another tenant's rows."""
    other_doc = "0190c000-0000-7000-8000-0000000000e9"
    other_space = "0190c000-0000-7000-8000-0000000000e8"
    other_tenant = "0190c000-0000-7000-8000-0000000000e7"
    other_version = "0190c000-0000-7000-8000-0000000000f7"

    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO tenants (id, slug, name, status) VALUES "
                "(:id, 'retention-o', 'o', 'active') ON CONFLICT (slug) DO NOTHING"
            ),
            {"id": other_tenant},
        )
        conn.execute(
            text(
                "INSERT INTO knowledge_spaces (id, tenant_id, name) VALUES (:id, :t, 'o') "
                "ON CONFLICT DO NOTHING"
            ),
            {"id": other_space, "t": other_tenant},
        )
        conn.execute(
            text(
                "INSERT INTO documents (id, tenant_id, space_id, title, canonical_uri) VALUES "
                "(:id, :t, :s, 'o', 'minio://o/o.pdf') ON CONFLICT DO NOTHING"
            ),
            {"id": other_doc, "t": other_tenant, "s": other_space},
        )
        conn.execute(
            text(_VERSION_INSERT),
            {
                "id": other_version,
                "t": other_tenant,
                "doc": other_doc,
                "label": "v-other",
                "status": "superseded",
                "expires": NOW - 90 * DAY,
                "uri": "minio://o/other.pdf",
            },
        )
    try:
        counts = _run(_sweep(now=NOW))

        assert counts["document_versions_expired"] == 0
        assert _status_of(other_version) == "superseded"
    finally:
        with admin.begin() as conn:
            conn.execute(
                text("DELETE FROM document_versions WHERE tenant_id = :t"), {"t": other_tenant}
            )
            conn.execute(text("DELETE FROM documents WHERE tenant_id = :t"), {"t": other_tenant})
            conn.execute(
                text("DELETE FROM knowledge_spaces WHERE tenant_id = :t"), {"t": other_tenant}
            )
            conn.execute(text("DELETE FROM tenants WHERE id = :t"), {"t": other_tenant})
        admin.dispose()
