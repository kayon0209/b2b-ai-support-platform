"""The ListObjectsV2 XML is attacker-shaped input, not trusted protocol output.

Storage responses arrive over whatever transport the endpoint is configured
with, and `secure=False` is the default here - so in the response direction
there is nothing authenticating the bytes. A ListObjectsV2 reply is the one
place this client feeds remote input to an XML parser.

`defusedxml` would solve it and is not a declared dependency, so the control is
a refusal: no DOCTYPE, no ENTITY, no parse. This asserts that refusal actually
happens, because a guard nobody has exercised is a comment.
"""

from __future__ import annotations

import httpx
import pytest

from platform_core.knowledge.storage import MinioStorage, StorageValidationError

BILLION_LAUGHS = b"""<?xml version="1.0"?>
<!DOCTYPE lolz [
 <!ENTITY lol "lol">
 <!ENTITY lol1 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">
 <!ENTITY lol2 "&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;">
]>
<ListBucketResult><Contents><Key>&lol2;</Key></Contents></ListBucketResult>
"""

XXE_FILE_READ = b"""<?xml version="1.0"?>
<!DOCTYPE r [<!ENTITY x SYSTEM "file:///etc/passwd">]>
<ListBucketResult><Contents><Key>&x;</Key></Contents></ListBucketResult>
"""


@pytest.fixture
def hostile_xml(monkeypatch: pytest.MonkeyPatch) -> list[bytes]:
    """Intercept the wire so the endpoint answer can be chosen by the test."""
    box: list[bytes] = []

    def fake_request(method: str, url: str, **kwargs: object) -> httpx.Response:
        return httpx.Response(200, content=box[0])

    monkeypatch.setattr(httpx, "request", fake_request)
    return box


def _storage() -> MinioStorage:
    return MinioStorage(endpoint="localhost:9000", bucket="documents")


def test_a_response_carrying_a_dtd_is_refused(hostile_xml: list[bytes]) -> None:
    """Entity expansion is the attack. The DTD is refused, not expanded."""
    hostile_xml.append(BILLION_LAUGHS)

    with pytest.raises(StorageValidationError, match="DTD or entity"):
        _storage().list_objects(prefix="tenant")


def test_an_external_entity_declaration_is_refused(hostile_xml: list[bytes]) -> None:
    """XXE: a SYSTEM entity naming a local file is the same refusal."""
    hostile_xml.append(XXE_FILE_READ)

    with pytest.raises(StorageValidationError, match="DTD or entity"):
        _storage().list_objects(prefix="tenant")


def test_a_well_formed_response_is_still_parsed(hostile_xml: list[bytes]) -> None:
    """The negative case, so the guard cannot pass by refusing everything.

    A guard that always refuses would satisfy both tests above and would break
    the reconciliation job completely - worse than the attack, because it fails
    silently and reads as "no orphans found".
    """
    hostile_xml.append(
        b'<?xml version="1.0" encoding="UTF-8"?>'
        b'<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
        b"<Name>documents</Name><IsTruncated>false</IsTruncated>"
        b"<Contents><Key>tenant-a/ver/f.md</Key><Size>12</Size></Contents>"
        b"<Contents><Key>tenant-a/ver/g.md</Key><Size>3</Size></Contents>"
        b"</ListBucketResult>"
    )

    keys = _storage().list_objects(prefix="tenant-a")
    assert keys == ["tenant-a/ver/f.md", "tenant-a/ver/g.md"], keys


def test_a_namespaced_response_parses_without_declaring_the_namespace(
    hostile_xml: list[bytes],
) -> None:
    """MinIO and AWS both emit an xmlns; the local-name match must survive it.

    Asserted separately because it is a different failure from the DTD guard:
    here the keys come back empty rather than an error, which reconciliation
    would read as a clean bill of health.
    """
    hostile_xml.append(
        b'<?xml version="1.0"?>'
        b'<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
        b"<IsTruncated>false</IsTruncated>"
        b"<Contents><Key>ns/key.md</Key></Contents></ListBucketResult>"
    )

    assert _storage().list_objects(prefix="ns") == ["ns/key.md"]
