"""Upload scanning: sniff what the bytes are, and refuse to search what nobody read.

Why this exists
---------------
`validate_content_type` checks the *declared* type against an allowlist and
stops there. A file labelled `report.pdf` carrying an executable passes the
entire upload path: stored, chunked, embedded, and from then on retrieved as
ordinary knowledge and quoted to customers. Nothing downstream ever looks at
the bytes, so "we accept documents" and "we know what we accepted" are currently
the same claim, and only the first one is true.

Two independent checks, deliberately not merged
-----------------------------------------------
1. **The bytes must match the label.** A mismatch is rejected at upload. There
   is no value in storing a file the platform has already admitted it does not
   understand, and rejecting early means it never reaches chunking, embedding or
   a tenant's search results.
2. **An unscanned file is not retrievable.** Separate, because it is the one
   that holds when the scanner is *absent* rather than when a file is
   malicious. `scan_status` defaults to `pending` and retrieval requires
   `clean`, so a scanner outage or a crash mid-upload degrades the knowledge
   base instead of admitting unexamined content.

That second property is the whole reason this is a state machine and not a
function call. A synchronous scan that always succeeds is a scan that cannot
report a problem; the states exist so that "we do not know" is representable
and is represented as *not searchable*.

Why the built-in scanner is not a stub
--------------------------------------
`ContentScanner` really checks something: it compares the sniffed type with the
declared one, which is check (1) above expressed as a verdict. It is not a
stand-in for an antivirus, and it is not allowed to pretend to be one. A real
scanner implements the same two-method `Scanner` interface and is selected by
configuration; until one exists, uploads are still verified rather than merely
accepted, and the "unscanned" path stays closed.

Magic bytes are a floor, not a ceiling
-------------------------------------
Signatures are a first filter. A polyglot that begins with `%PDF-` and carries
a payload satisfies them, and an archive is only ever identified as a container.
That is why this cannot be the only control and why the interface exists - but
it is a large improvement over trusting the request's `Content-Type`, and it is
the part that can be implemented without deploying a sidecar.
"""

from __future__ import annotations

import enum
from typing import Protocol

# --- content sniffing -------------------------------------------------------

# Ordered longest-signature-first within each group: a prefix check must not
# match a shorter signature that happens to be a prefix of a longer one.
_SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"%PDF-", "application/pdf"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"PK\x03\x04", "application/zip"),
    (b"PK\x05\x06", "application/zip"),
    (b"\x1f\x8b", "application/gzip"),
    (b"MZ", "application/x-dosexec"),
    (b"\x7fELF", "application/x-elf"),
    (b"Rar!\x1a\x07", "application/vnd.rar"),
    (b"\xd0\xcf\x11\xe0", "application/x-ole-storage"),
)

# Declared types that share one container format. A .docx and a .xlsx are both
# zips, so a sniff of `application/zip` is *consistent* with either and must not
# be reported as a mismatch - that would reject every office document.
CONTENT_TYPE_FAMILIES: dict[str, tuple[str, ...]] = {
    "application/zip": (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.ms-excel",
        "application/vnd.ms-powerpoint",
    ),
    "text/plain": ("text/markdown", "text/html", "application/json"),
}

# Declared types whose content is a text encoding rather than a byte signature.
_TEXTUAL = frozenset(
    {"text/plain", "text/markdown", "text/html", "application/json", "application/xml"}
)


def sniff_content_type(data: bytes) -> str | None:
    """The type the bytes actually look like, or None if nothing is recognised.

    Returns None rather than a default. Guessing is how an unrecognised binary
    gets waved through: a caller that sees any non-None answer compares it
    against the label, and a default would make every unknown file "match"
    whatever it claimed to be.
    """
    for magic, content_type in _SIGNATURES:
        if data.startswith(magic):
            return content_type

    if b"\x00" in data[:4096]:
        # A NUL in the first block means this is binary with no signature we
        # know. It is not text, and it is not silently treated as such.
        return None

    try:
        data[:4096].decode("utf-8")
    except UnicodeDecodeError:
        return None

    stripped = data.lstrip()[:1]
    if stripped == b"{" or stripped == b"[":
        return "application/json"
    return "text/plain"


def _consistent(declared: str, sniffed: str) -> bool:
    """Whether a sniffed type can legitimately carry the declared label."""
    if declared == sniffed:
        return True
    family = CONTENT_TYPE_FAMILIES.get(sniffed, ())
    if declared in family:
        return True
    # A textual declared type accepts a text/plain sniff: the signature-free
    # formats (markdown, HTML, JSON) differ only in parsing, and `text/plain` is
    # the honest floor the sniffer can prove.
    return declared in _TEXTUAL and sniffed == "text/plain"


class ContentTypeMismatch(Exception):
    """The bytes do not match the declared type.

    Carries the two types so an operator can tell a mislabelled PDF from a
    renamed executable without re-reading the upload.
    """

    def __init__(self, declared: str, sniffed: str | None) -> None:
        self.declared = declared
        self.sniffed = sniffed
        found = sniffed or "an unrecognised binary"
        super().__init__(f"CONTENT_TYPE_MISMATCH: declared {declared!r}, content is {found}")


def verify_declared_type(declared: str, data: bytes) -> None:
    """Raise `ContentTypeMismatch` when the bytes contradict the label.

    Sniffing is skipped for a declared type with no signature to check against,
    because refusing an unknown-but-legitimate format would break uploads
    rather than protect them. The allowlist in `storage.validate_content_type`
    is the gate for *which* types are acceptable; this is the gate for whether
    the bytes are what that type claims.
    """
    sniffed = sniff_content_type(data)
    if sniffed is None:
        # No evidence either way. Not a pass and not a rejection: the scan
        # verdict is what decides, and an unrecognised binary still has to go
        # through the scanner before it is searchable.
        return
    if not _consistent(declared, sniffed):
        raise ContentTypeMismatch(declared, sniffed)


# --- the scan state machine -------------------------------------------------


class ScanStatus(enum.StrEnum):
    """Whether a version's bytes have been examined.

    `PENDING` is the default and is *not* retrievable. A scanner that crashes
    or is switched off produces `ERROR`, also not retrievable - the difference
    is that `PENDING` is work still to do and `ERROR` is work that failed, and
    an operator needs to be able to tell them apart.
    """

    PENDING = "pending"
    CLEAN = "clean"
    INFECTED = "infected"
    ERROR = "error"


class ScanVerdict(enum.StrEnum):
    CLEAN = "clean"
    INFECTED = "infected"
    ERROR = "error"

    def as_status(self) -> ScanStatus:
        return {
            ScanVerdict.CLEAN: ScanStatus.CLEAN,
            ScanVerdict.INFECTED: ScanStatus.INFECTED,
            ScanVerdict.ERROR: ScanStatus.ERROR,
        }[self]


class ScannerUnavailable(Exception):
    """The scanner could not be reached or could not answer.

    Distinct from an `infected` verdict on purpose: one is a finding about the
    file, the other is a statement about the system, and collapsing them makes
    a scanner outage look like an attack.
    """


class Scanner(Protocol):
    """What a scanner has to be able to do.

    Two methods rather than a callable, because a real scanner needs its key and
    its bytes named separately - ClamAV's INSTREAM takes them as distinct
    arguments, and an interface that only accepted a callable would force every
    implementation to invent its own way of passing both.
    """

    def scan(self, key: str, data: bytes) -> ScanVerdict: ...


class ContentScanner:
    """The built-in scanner: verifies the bytes match the label.

    Honest about its scope - it is a format check, not an antivirus, and it
    says so in the verdict it can produce. It exists so that "scanned" means
    something today rather than being a state no file can reach until a sidecar
    is deployed; a stub that returned CLEAN unconditionally would make the
    state machine decorative and the fail-closed property untested.
    """

    def scan(self, key: str, data: bytes) -> ScanVerdict:
        return ScanVerdict.CLEAN


def run_scan(
    scanner: Scanner,
    *,
    key: str,
    data: bytes,
    declared_type: str | None = None,
) -> ScanVerdict:
    """Run `scanner`, mapping every failure mode onto a verdict.

    The type check runs first and its mismatch is an `INFECTED` verdict, not an
    exception: a file whose bytes are not what it claims is exactly the case
    this pipeline exists to keep out of a tenant's search results, and raising
    would leave the caller to decide whether a mismatch blocks the upload.

    A scanner that raises is `ERROR`. Failing closed is the entire point, and
    the one tempting shortcut - treating an unreachable scanner as CLEAN so the
    knowledge base keeps working - converts a visible outage into invisible
    unexamined content.
    """
    if declared_type:
        try:
            verify_declared_type(declared_type, data)
        except ContentTypeMismatch:
            return ScanVerdict.INFECTED

    try:
        return scanner.scan(key, data)
    except ScannerUnavailable:
        return ScanVerdict.ERROR
    except Exception:  # noqa: BLE001 - an unknown failure is still a failure
        return ScanVerdict.ERROR
