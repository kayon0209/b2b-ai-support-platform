"""Scheduled backup: a logical database dump, uploaded to the backup bucket.

What this does
--------------
1. Creates the backup bucket if it is absent and turns versioning on.
2. Runs `pg_dump -Fc` against the owner database.
3. Uploads the dump under a timestamped key.
4. Prunes dumps older than the retention window.

The rules it obeys
------------------
1. **A failed backup exits non-zero.** A backup that logs an error and returns
   success is worse than no backup, because the schedule looks healthy and
   nobody discovers the gap until a restore is needed.
2. **It reads the source only.** `pg_dump` is the only thing that touches the
   source database, and it is read-only by construction.
3. **The upload is the last step, and a failure there is fatal.** A dump on the
   pod's ephemeral `/tmp` dies with the pod. A dump that is produced and not
   uploaded has not been backed up, and treating that as success is how a
   month of "backups" ends up being zero backups.

Why the backup bucket is not the documents bucket
------------------------------------------------
The live documents bucket must stay unversioned, or retention's erasure stops
meaning anything: a DELETE on a versioned bucket leaves the earlier bytes
stored and readable by `version_id` (measured - see
`MinioStorage.bucket_versioning_enabled`). This bucket is where versioning is
therefore safe, and it is enabled here precisely because a backup that can be
silently overwritten by the next run is not a backup.

Retention is enforced here rather than left to bucket lifecycle rules because
the lifecycle configuration is not visible from here, and a retention policy
that exists only in a console nobody reads is not a policy.

Run:
    python scripts/backup.py --dump
    python scripts/backup.py --dump --keep 14
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import pathlib
import subprocess
import sys
import tempfile
import uuid

DUMP_PREFIX = "pgdump/platform-"
DEFAULT_KEEP = 30


def _run(cmd: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    # Fixed argv built here; no shell, nothing from outside this file.
    return subprocess.run(cmd, capture_output=True, text=True, check=check)  # noqa: S603


def _database_url() -> str:
    """The owner DSN, with the `+psycopg` driver suffix removed.

    `psycopg`'s URL form is for SQLAlchemy. `pg_dump` wants a plain
    `postgresql://` URL and rejects the driver segment outright, so the two
    spellings cannot be the same string.
    """
    url = os.environ.get("APP_DATABASE_URL", "").strip()
    if not url:
        raise SystemExit("APP_DATABASE_URL is not set; cannot dump")
    return url.replace("postgresql+psycopg://", "postgresql://", 1)


def _bucket_name() -> str:
    bucket = os.environ.get("APP_BACKUP_BUCKET", "").strip()
    if not bucket:
        raise SystemExit(
            "APP_BACKUP_BUCKET is not set. It must be a bucket other than the "
            "one the platform serves: a versioned documents bucket makes "
            "retention erasure a delete marker instead of a deletion."
        )
    return bucket


def _storage(bucket: str) -> object:
    from platform_core.config import get_settings
    from platform_core.knowledge.service import object_storage

    return object_storage(get_settings(), bucket=bucket)


def _stamp(now: dt.datetime | None = None) -> str:
    return (now or dt.datetime.now(dt.UTC)).strftime("%Y%m%dT%H%M%SZ")


def _prune(storage: object, *, keep: int) -> int:
    """Delete dumps beyond the newest `keep`. Returns how many were removed.

    Sorted by the timestamp in the key rather than by modification time,
    because the key is what this script wrote and the endpoint's notion of
    "last modified" is not something the bucket lifecycle is guaranteed to
    preserve across a copy or a restore.
    """
    keys = [k for k in storage.list_objects(prefix=DUMP_PREFIX) if k.endswith(".dump")]  # type: ignore[attr-defined]
    if len(keys) <= keep:
        return 0
    doomed = sorted(keys)[: len(keys) - keep]
    removed = 0
    for key in doomed:
        if storage.delete_object(key):  # type: ignore[attr-defined]
            removed += 1
    return removed


def take_dump(*, keep: int, now: dt.datetime | None = None) -> int:
    bucket = _bucket_name()
    storage = _storage(bucket)

    # Idempotent, and the versioning call is the reason this bucket exists.
    created = storage.ensure_bucket()  # type: ignore[attr-defined]
    storage.set_bucket_versioning(True)  # type: ignore[attr-defined]
    if created:
        print(f"created backup bucket {bucket}")

    url = _database_url()
    # Unique per run so a retried backup cannot overwrite a good dump with a
    # partial one - a collision here would replace a known-good object with an
    # unknown-quality one and versioning would faithfully keep both.
    key = f"{DUMP_PREFIX}{_stamp(now)}-{uuid.uuid4().hex[:8]}.dump"

    with tempfile.TemporaryDirectory() as tmp:
        path = pathlib.Path(tmp) / "platform.dump"
        print(f"dumping to {key} ...")
        _run(["pg_dump", url, "-Fc", "-f", str(path)])
        size = path.stat().st_size
        if size == 0:
            raise SystemExit("pg_dump produced an empty file; refusing to upload it")
        storage.put_object(key, path.read_bytes(), "application/octet-stream")  # type: ignore[attr-defined]
        print(f"uploaded {size} bytes")

    removed = _prune(storage, keep=keep)
    if removed:
        print(f"pruned {removed} dump(s) beyond the newest {keep}")
    print("BACKUP OK")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dump", action="store_true", help="take a dump and upload it")
    parser.add_argument(
        "--keep",
        type=int,
        default=DEFAULT_KEEP,
        help=f"how many dumps to retain (default {DEFAULT_KEEP})",
    )
    args = parser.parse_args()

    if not args.dump:
        parser.error("nothing to do; pass --dump")
    if args.keep < 1:
        # keep=0 would prune every backup including the one just uploaded.
        parser.error("--keep must be at least 1")
    try:
        return take_dump(keep=args.keep)
    except subprocess.CalledProcessError as exc:
        print(f"pg_dump failed: {exc.stderr.strip()}", file=sys.stderr)
        return 1
    except SystemExit as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
