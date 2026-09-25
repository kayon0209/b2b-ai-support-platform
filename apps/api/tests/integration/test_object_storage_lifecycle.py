"""Object storage lifecycle: create, enumerate, and actually delete bytes.

Why this suite talks to a real server
-------------------------------------
The existing storage tests assert on the *string* a presign call returns. That
is enough to catch a mangled URL but cannot catch a request the server
rejects - and the two failures this module exists to prevent are both of that
shape: a signature computed over the wrong canonical form, or a method the
server answers with 405. Both look perfectly healthy as a Python string.

So these tests run against a live S3 endpoint. `APP_S3_ENDPOINT` names it;
without one the module skips, because a storage client that cannot delete is
not something to assert about in the abstract.

The property under test
-----------------------
Deleting a row is not deleting data. The retention sweep marks a document
version EXPIRED and stops there, leaving the bytes in the bucket forever - a
promise to erase something, kept only in the index. Nothing in that path can
currently delete, or even enumerate, so the gap is not a missed call site: it
is a missing capability in the client. These tests are what make the
capability real rather than assumed.
"""

import os
import uuid

import pytest

pytestmark = pytest.mark.integration

ENDPOINT = os.environ.get("APP_S3_ENDPOINT")
BUCKET = os.environ.get("APP_S3_BUCKET", "documents")

if not ENDPOINT:
    pytest.skip(
        "APP_S3_ENDPOINT is not set - object storage lifecycle needs a live endpoint",
        allow_module_level=True,
    )


def _storage() -> object:
    from platform_core.knowledge.storage import MinioStorage

    return MinioStorage(endpoint=ENDPOINT, bucket=BUCKET)


def test_deleting_an_object_removes_the_bytes() -> None:
    """The whole point. A status flag is not erasure.

    Asserted by reading the object back after the delete: a delete that
    silently no-ops still returns success from most S3 clients, because
    DELETE is idempotent on the server - so "no exception" would prove
    nothing at all about whether the bytes are gone.
    """
    from platform_core.knowledge.storage import ObjectNotFound

    storage = _storage()
    storage.ensure_bucket()
    key = f"lifecycle-test/{uuid.uuid4().hex}/erase-me.txt"

    storage.put_object(key, b"payload that must not survive", "text/plain")
    assert storage.get_object(key) == b"payload that must not survive"

    assert storage.delete_object(key) is True

    with pytest.raises(ObjectNotFound):
        storage.get_object(key)


def test_deleting_an_absent_object_reports_absence_without_raising() -> None:
    """Retention retries, and a sweep is not the only caller.

    Raising here would make an idempotent operation fail on the second
    attempt, which is precisely the shape that turns a retrying worker into
    a stuck one. The caller distinguishes "it was there and is gone" from
    "it was never there" by the return value, not by catching.
    """
    storage = _storage()
    storage.ensure_bucket()

    assert storage.delete_object(f"lifecycle-test/{uuid.uuid4().hex}/never-existed") is False


def test_list_objects_enumerates_only_keys_under_the_prefix() -> None:
    """Reconciliation needs to walk a tenant's objects to find orphans.

    The prefix is the tenant id. Enumerating without it would make every
    reconciliation pass read every tenant's objects, which is both slow and
    a cross-tenant read by a code path that has no business doing one.
    """
    storage = _storage()
    storage.ensure_bucket()
    mine = f"lifecycle-test/{uuid.uuid4().hex}"
    theirs = f"lifecycle-test/{uuid.uuid4().hex}"

    storage.put_object(f"{mine}/a.txt", b"a", "text/plain")
    storage.put_object(f"{mine}/b.txt", b"b", "text/plain")
    storage.put_object(f"{theirs}/c.txt", b"c", "text/plain")

    found = sorted(storage.list_objects(prefix=mine))
    assert found == sorted([f"{mine}/a.txt", f"{mine}/b.txt"]), (
        f"prefix leaked into another tenant's namespace: {found}"
    )

    for key in found + [f"{theirs}/c.txt"]:
        storage.delete_object(key)


def test_ensure_bucket_is_idempotent() -> None:
    """It runs from a migration job and from the retention worker.

    Creating a bucket that already exists is not an error in S3's data model,
    but it is a 409 from some endpoints, and a job that fails on its second
    run is a job that cannot be re-run after a partial failure.
    """
    storage = _storage()

    assert storage.ensure_bucket() in (True, False)
    assert storage.ensure_bucket() is False, "second call reported the bucket as newly created"


def test_a_deleted_object_leaves_no_key_behind() -> None:
    """The negative direction of the enumeration test.

    A delete that removes the bytes but leaves the key listed would make
    reconciliation report a phantom orphan forever - it would find a key with
    no row, try to delete it, find it already gone, and flag it again.
    """
    storage = _storage()
    storage.ensure_bucket()
    prefix = f"lifecycle-test/{uuid.uuid4().hex}"
    key = f"{prefix}/vanish.txt"

    storage.put_object(key, b"x", "text/plain")
    assert storage.list_objects(prefix=prefix) == [key]

    storage.delete_object(key)
    assert storage.list_objects(prefix=prefix) == [], "object is gone but still enumerated"
