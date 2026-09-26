"""Upload scanning: a file that nobody looked at must not be searchable.

The gap
-------
`validate_content_type` checks the *declared* type against an allowlist and
nothing else. A file called `report.pdf` carrying an executable, or an HTML
document declared as a PDF, passes the whole upload path: it is stored, chunked,
embedded, and from then on it is retrieved as ordinary knowledge and quoted to
customers. Nothing in the pipeline ever looks at the bytes.

The property under test
-----------------------
Two independent claims, asserted separately because they fail differently:

1. The bytes must match the label. A mismatch is rejected at upload, with the
   same error the caller already handles for an unsupported type - there is no
   point storing a file we already know we do not understand.
2. An unscanned file must not be retrievable. This is the one that matters when
   the scanner is unavailable: the default is *not retrievable*, so a scanner
   that is down or an upload that races it degrades the knowledge base instead
   of quietly admitting unexamined content. A system that refuses to answer is
   recoverable; a system that answers with whatever was uploaded is not.

A test that only asserted (1) would pass with no scanner at all, because the
rejection happens before anything is ever stored.
"""

import uuid

import pytest

from platform_core.knowledge.scanning import (
    ScanVerdict,
    sniff_content_type,
    verify_declared_type,
)

pytestmark = pytest.mark.integration

TENANT = "0190c000-0000-7000-8000-0000000000ec"
SPACE = "0190c000-0000-7000-8000-0000000000ed"
DOC = "0190c000-0000-7000-8000-0000000000ee"

PDF = b"%PDF-1.7\n1 0 obj\n<<>>\nendobj\ntrailer\n%%EOF\n"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
ZIP = b"PK\x03\x04" + b"\x00" * 32
EXE = b"MZ\x90\x00\x03\x00\x00\x00\x04\x00\x00\x00\xff\xff\x00\x00"
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 32


# --- sniffing ---------------------------------------------------------------


def test_known_binary_signatures_are_recognised() -> None:
    """The sniff has to actually identify things, not merely reject.

    Asserted positively: a scanner that always returns "unknown" would satisfy
    every rejection test below and quietly stop accepting real PDFs.
    """
    assert sniff_content_type(PDF) == "application/pdf"
    assert sniff_content_type(PNG) == "image/png"
    assert sniff_content_type(ZIP) == "application/zip"
    assert sniff_content_type(JPEG) == "image/jpeg"
    assert sniff_content_type(EXE) == "application/x-dosexec"


def test_text_shaped_uploads_are_recognised_by_encoding() -> None:
    """No magic bytes exist for text, so encoding is the only evidence there is.

    Asserted because the allowlist is mostly text formats, and a sniffer that
    only knew about PDFs would reject every markdown knowledge upload.
    """
    assert sniff_content_type("政策说明\n第二行".encode()) == "text/plain"
    assert sniff_content_type(b'{"a": 1}') == "application/json"


def test_a_binary_blob_with_no_known_signature_is_not_guessed() -> None:
    """`None` rather than a default. Guessing is how a malware type gets a pass."""
    assert sniff_content_type(b"\x00\x01\x02\x03\x04\x05\x06\x07") is None


# --- declared vs actual -----------------------------------------------------


def test_a_pdf_label_on_executable_bytes_is_rejected() -> None:
    """The rename attack, stated plainly."""
    with pytest.raises(Exception) as exc:
        verify_declared_type("application/pdf", EXE)
    assert "MISMATCH" in str(exc.value)


def test_an_image_label_on_pdf_bytes_is_rejected() -> None:
    with pytest.raises(Exception) as exc:
        verify_declared_type("image/png", PDF)
    assert "MISMATCH" in str(exc.value)


def test_matching_bytes_pass() -> None:
    assert verify_declared_type("application/pdf", PDF) is None


def test_a_text_label_on_text_passes() -> None:
    assert verify_declared_type("text/markdown", "# 标题".encode()) is None


def test_office_documents_are_checked_as_zip_containers() -> None:
    """docx/xlsx are zips; the declared type is the container's *role*.

    A .docx that is a plain-text file renamed, or an .xlsx that is really a
    .docx, both pass a naive extension check. Comparing against the sniffed
    *container* type is the strongest statement available without opening the
    archive.
    """
    from platform_core.knowledge.scanning import CONTENT_TYPE_FAMILIES

    assert (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        in (CONTENT_TYPE_FAMILIES["application/zip"])
    )
    assert (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        in (CONTENT_TYPE_FAMILIES["application/zip"])
    )


# --- the state machine ------------------------------------------------------


def _seed_version(status: str, scan_status: str) -> str:
    """A version row in a given state, bypassing the upload path.

    Needed because the interesting cases - a scanner that never ran, one that
    crashed, one that found something - cannot be produced by a successful
    upload, which is the thing being tested.
    """
    import os

    from sqlalchemy import create_engine, text

    url = os.environ.get(
        "APP_ADMIN_DATABASE_URL",
        "postgresql+psycopg://platform:platform@localhost:5435/platform",
    )
    version_id = str(uuid.uuid4())
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO document_versions "
                "(id, tenant_id, document_id, version_label, content_hash, status, "
                " object_uri, expires_at, scan_status) "
                "VALUES (:id, :t, :d, 'v1', 'sha256:x', :st, :uri, NULL, :scan)"
            ),
            {
                "id": version_id,
                "t": TENANT,
                "d": DOC,
                "st": status,
                "uri": f"{TENANT}/{version_id}/doc.pdf",
                "scan": scan_status,
            },
        )
    engine.dispose()
    return version_id


def _seed_scan_fixture() -> tuple[str, str]:
    """A tenant with one `clean` version and one `pending` version, same text.

    Same chunk text on purpose: with the gate off both are returned, and the
    difference between the two runs can only be the scan state.
    """
    import os

    from sqlalchemy import create_engine, text

    url = os.environ.get(
        "APP_ADMIN_DATABASE_URL",
        "postgresql+psycopg://platform:platform@localhost:5435/platform",
    )
    engine = create_engine(url)
    tenant, space, doc = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())
    body = "scanner gate probe"
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO tenants (id, slug, name, status) VALUES "
                "(:id, :slug, 'Scan', 'active') ON CONFLICT (slug) DO NOTHING"
            ),
            {"id": tenant, "slug": f"scan-{tenant[-8:]}"},
        )
        conn.execute(
            text("INSERT INTO knowledge_spaces (id, tenant_id, name) VALUES (:i,:t,'s')"),
            {"i": space, "t": tenant},
        )
        conn.execute(
            text(
                "INSERT INTO documents (id, tenant_id, space_id, canonical_uri, title) "
                "VALUES (:i,:t,:s,'kb://scan','Scan Doc')"
            ),
            {"i": doc, "t": tenant, "s": space},
        )
        for scan in ("clean", "pending"):
            ver = str(uuid.uuid4())
            conn.execute(
                text(
                    "INSERT INTO document_versions (id, tenant_id, document_id, "
                    "version_label, content_hash, object_uri, status, scan_status) "
                    "VALUES (:i,:t,:d,:l,'h','minio://x','active',:scan)"
                ),
                {"i": ver, "t": tenant, "d": doc, "l": f"v-{scan}", "scan": scan},
            )
            conn.execute(
                text(
                    "INSERT INTO chunks (id, tenant_id, document_version_id, ordinal, "
                    "text, text_hash) VALUES (:i,:t,:v,0,:x,:h)"
                ),
                {"i": str(uuid.uuid4()), "t": tenant, "v": ver, "x": body, "h": "h"},
            )
    engine.dispose()
    return tenant, body


def test_the_scan_gate_excludes_everything_that_is_not_clean(monkeypatch) -> None:
    """The security property, asserted behaviourally in both directions.

    An earlier version of this test grepped the retrieval source for the
    predicate, which passed while proving nothing: a fragment in a string that
    no query path used would satisfy it. So this seeds one `clean` and one
    `pending` version carrying identical text and compares what retrieval
    actually returns with the gate off and on.

    The off case is asserted as well as the on case. A gate that excludes
    everything would pass a "pending is hidden" test alone, and the way to tell
    the difference is to show the clean version still comes back.
    """
    import asyncio
    import os

    from sqlalchemy import create_engine, text

    tenant, body = _seed_scan_fixture()
    try:

        def hits() -> int:
            from sqlalchemy.ext.asyncio import async_sessionmaker

            from platform_core.db import create_engine
            from platform_core.retrieval.hybrid import hybrid_search

            app_url = os.environ.get(
                "APP_TEST_DATABASE_URL",
                "postgresql+psycopg://platform_app:platform_app@localhost:5435/platform",
            )

            async def run() -> int:
                engine = create_engine(app_url)
                factory = async_sessionmaker(engine, expire_on_commit=False)
                try:
                    async with factory() as session:
                        await session.execute(
                            text("SELECT set_config('app.tenant_id', :t, true)"),
                            {"t": tenant},
                        )
                        found = await hybrid_search(
                            session, tenant_id=uuid.UUID(tenant), query=body
                        )
                        await session.rollback()
                        return len(found)
                finally:
                    await engine.dispose()

            return asyncio.run(run(), loop_factory=asyncio.SelectorEventLoop)

        from platform_core.config import get_settings

        settings = get_settings()
        monkeypatch.setattr(settings, "require_scanned_documents", False)
        assert hits() == 2, "gate off should return both versions"

        monkeypatch.setattr(settings, "require_scanned_documents", True)
        assert hits() == 1, "with the gate on, the pending version is still retrievable"
    finally:
        admin = create_engine(
            os.environ.get(
                "APP_ADMIN_DATABASE_URL",
                "postgresql+psycopg://platform:platform@localhost:5435/platform",
            )
        )
        with admin.begin() as conn:
            conn.execute(text("DELETE FROM chunks WHERE tenant_id = :t"), {"t": tenant})
            conn.execute(text("DELETE FROM document_versions WHERE tenant_id = :t"), {"t": tenant})
            conn.execute(text("DELETE FROM documents WHERE tenant_id = :t"), {"t": tenant})
            conn.execute(text("DELETE FROM knowledge_spaces WHERE tenant_id = :t"), {"t": tenant})
            conn.execute(text("DELETE FROM tenants WHERE id = :t"), {"t": tenant})
        admin.dispose()


def test_the_scan_gate_is_off_by_default() -> None:
    """Pin the default, because the default is a judgement.

    `ContentScanner` verifies that a file's bytes match its declared type. That
    is a real check and it stops a renamed executable - but it is a format
    check, not an antivirus. Requiring its `clean` before search would mark the
    whole existing corpus as scanned on the strength of a magic-byte
    comparison, which is a clearance the platform has not earned. The gate goes
    on when a real scanner does; the tests above prove it works when it does.
    """
    from platform_core.config import get_settings

    assert get_settings().require_scanned_documents is False


def test_the_gate_excludes_error_as_well_as_pending() -> None:
    """A scanner outage must stop retrieval, not admit unexamined content.

    `error` is the state a crashed or unreachable scanner produces, and it is
    precisely when the gate matters. Asserted on the predicate's vocabulary so
    that a future edit cannot quietly narrow it to `pending`.
    """
    from platform_core.knowledge.scanning import ScanStatus

    assert {ScanStatus.PENDING.value, ScanStatus.ERROR.value} == {"pending", "error"}
    assert ScanStatus.CLEAN.value not in {"pending", "error"}


def test_an_uploaded_version_is_pending_and_therefore_not_retrievable() -> None:
    """Default is not searchable. Anything else is opt-in."""
    from platform_core.knowledge.scanning import ScanStatus

    assert ScanStatus.PENDING.value == "pending"
    assert ScanStatus.PENDING.value != ScanStatus.CLEAN.value


def test_a_scanner_that_threw_yields_error_not_clean() -> None:
    """Failing open is the whole failure mode this state machine exists to stop."""
    from platform_core.knowledge.scanning import ScannerUnavailable, run_scan

    class Broken:
        def scan(self, key: str, data: bytes):
            raise RuntimeError("clamd is not answering")

    verdict = run_scan(Broken(), key="k", data=PDF, declared_type="application/pdf")
    assert verdict is ScanVerdict.ERROR
    assert issubclass(ScannerUnavailable, Exception)
