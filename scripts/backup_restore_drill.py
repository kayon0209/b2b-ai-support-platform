"""Backup/restore drill: prove a restore actually reproduces the platform.

`docs/deployment-and-operations.md` promises quarterly restore drills that
demonstrate target RPO/RTO, tenant data integrity, external mapping
consistency, audit continuity, and the ability to resume jobs without
duplicate actions. There was no script to run one, so that promise was
untestable.

Rules this script obeys
-----------------------
1. **It never writes to the source database.** The source is read with
   `pg_dump` only. The restore target is a scratch database created by this
   script and dropped by it. A drill that can damage the thing it is testing
   is not a drill.
2. **It fails loudly.** Any mismatch exits non-zero, because a drill that
   always reports success is worse than no drill: it converts an untested
   recovery path into a believed-working one.
3. **It states what it cannot measure.** A logical dump includes everything
   committed before it started, so RPO at dump time is 0 *by construction* -
   reporting that as a measurement would be theatre. The real RPO is the
   interval between dumps, which is an operational setting. What this script
   measures is RTO (wall-clock restore) and the integrity of what came back.

Run:
    ./.venv/Scripts/python.exe scripts/backup_restore_drill.py
    ./.venv/Scripts/python.exe scripts/backup_restore_drill.py --keep   # keep the scratch db
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from dataclasses import dataclass, field

CONTAINER = "b2b-ai-support-ai-postgres-1"
SOURCE_DB = "platform"
DB_USER = "platform"
SCRATCH_PREFIX = "platform_drill_"

# Tenant-owned tables whose per-tenant distribution must survive the round
# trip. A total that matches while the distribution does not would mean rows
# migrated between tenants - the failure that would be invisible to a count.
TENANT_TABLES = (
    "memberships",
    "cases",
    "documents",
    "document_versions",
    "chunks",
    "agent_runs",
    "citations",
    "audit_events",
    "outbox_events",
    "inbox_events",
    "connectors",
    "sync_cursors",
    "dead_letter_items",
    "external_resource_refs",
)

# Constraints whose absence would let a resumed job duplicate a side effect.
RESUME_CONSTRAINTS = (
    ("inbox_events", "uq_inbox_delivery"),
    ("sync_cursors", "uq_cursor_per_resource"),
    ("citations", "uq_citation_claim"),
)


def _run(cmd: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    # Every command is a fixed argv list built by this script; no shell and no
    # value from outside it, so there is nothing to quote or escape.
    return subprocess.run(cmd, capture_output=True, text=True, check=check)  # noqa: S603


def psql(db: str, sql: str, *, check: bool = True) -> str:
    result = _run(
        ["docker", "exec", CONTAINER, "psql", "-U", DB_USER, "-d", db, "-tAc", sql],
        check=check,
    )
    return result.stdout.strip()


def psql_maybe(db: str, sql: str) -> str | None:
    """Run a query that may fail because a table does not exist yet."""
    result = _run(
        ["docker", "exec", CONTAINER, "psql", "-U", DB_USER, "-d", db, "-tAc", sql],
        check=False,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip()


@dataclass
class Snapshot:
    """The facts a restore must reproduce, gathered from one database."""

    revision: str = ""
    table_counts: dict[str, int] = field(default_factory=dict)
    tenant_counts: dict[str, dict[str, int]] = field(default_factory=dict)
    audit_events: int = 0
    audit_min: int = 0
    audit_max: int = 0
    outbox_by_status: dict[str, int] = field(default_factory=dict)
    orphan_refs: int = 0
    constraints: dict[str, bool] = field(default_factory=dict)


def snapshot(db: str) -> Snapshot:
    snap = Snapshot()
    snap.revision = psql(db, "SELECT version_num FROM alembic_version") or ""

    tables = psql_maybe(
        db,
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = 'public' AND table_type = 'BASE TABLE' ORDER BY table_name",
    )
    for table in (tables or "").splitlines():
        table = table.strip()
        if not table:
            continue
        count = psql_maybe(db, f"SELECT count(*) FROM {table}")  # noqa: S608 - name from catalog
        if count is not None:
            snap.table_counts[table] = int(count)

    for table in TENANT_TABLES:
        rows = psql_maybe(
            db,
            f"SELECT tenant_id, count(*) FROM {table} GROUP BY tenant_id",  # noqa: S608
        )
        if rows is None:
            continue
        snap.tenant_counts[table] = {
            line.split("|")[0]: int(line.split("|")[1]) for line in rows.splitlines() if "|" in line
        }

    audit = psql_maybe(
        db,
        "SELECT count(*), coalesce(min(occurred_at),0), coalesce(max(occurred_at),0) "
        "FROM audit_events",
    )
    if audit:
        parts = audit.split("|")
        snap.audit_events, snap.audit_min, snap.audit_max = (int(p) for p in parts)

    outbox = psql_maybe(db, "SELECT status, count(*) FROM outbox_events GROUP BY status")
    if outbox:
        snap.outbox_by_status = {
            line.split("|")[0]: int(line.split("|")[1])
            for line in outbox.splitlines()
            if "|" in line
        }

    # An external mapping pointing at a tenant that no longer exists is a
    # dangling reference; it would fail silently at request time rather than
    # during recovery.
    orphans = psql_maybe(
        db,
        "SELECT count(*) FROM external_resource_refs r "
        "LEFT JOIN tenants t ON t.id = r.tenant_id WHERE t.id IS NULL",
    )
    if orphans:
        snap.orphan_refs = int(orphans)

    for table, constraint in RESUME_CONSTRAINTS:
        present = psql_maybe(
            db,
            # Both names are module constants, not input. The noqa has to sit
            # on the line ruff reports, which for an implicitly concatenated
            # string is the *first* part.
            "SELECT count(*) FROM pg_constraint WHERE conname = "  # noqa: S608
            f"'{constraint}' AND conrelid = 'public.{table}'::regclass",
        )
        if present is not None:
            snap.constraints[f"{table}.{constraint}"] = int(present) == 1

    return snap


@dataclass
class Finding:
    check: str
    ok: bool
    detail: str


def differences(before: Snapshot, after: Snapshot) -> list[str]:
    """Names of the facts that changed between two source snapshots.

    The drill compares a *restored* copy against the source, which is only
    meaningful if the source held still while it was dumped. This was not
    theoretical: running the drill while the test suite was mid-flight
    produced seven confident FAILs - "per-tenant distribution documents:
    missing tenant" - that were a concurrent fixture deleting its own rows, not
    a restore losing data. A drill that reports that as data loss trains people
    to ignore it, so quiescence is checked rather than assumed.
    """
    changed: list[str] = []
    if before.revision != after.revision:
        changed.append("alembic revision")
    for table in sorted(set(before.table_counts) | set(after.table_counts)):
        if before.table_counts.get(table) != after.table_counts.get(table):
            changed.append(f"row count {table}")
    for table in sorted(set(before.tenant_counts) | set(after.tenant_counts)):
        if before.tenant_counts.get(table) != after.tenant_counts.get(table):
            changed.append(f"per-tenant distribution {table}")
    if before.audit_events != after.audit_events:
        changed.append("audit event count")
    if before.outbox_by_status != after.outbox_by_status:
        changed.append("outbox status distribution")
    return changed


def compare(source: Snapshot, restored: Snapshot) -> list[Finding]:
    findings: list[Finding] = []

    findings.append(
        Finding(
            "alembic revision",
            source.revision == restored.revision,
            f"{source.revision!r} -> {restored.revision!r}",
        )
    )

    for table, expected in sorted(source.table_counts.items()):
        actual = restored.table_counts.get(table)
        findings.append(
            Finding(
                f"row count {table}",
                actual == expected,
                f"{expected} -> {actual}",
            )
        )

    for table, expected_counts in sorted(source.tenant_counts.items()):
        actual = restored.tenant_counts.get(table, {})
        ok = actual == expected_counts
        detail = "identical" if ok else f"{expected_counts} -> {actual}"
        findings.append(Finding(f"per-tenant distribution {table}", ok, detail))

    # Audit continuity: same event count and the same time span. A restore that
    # lost only the newest events would still have a plausible min, so the
    # count is what catches it.
    findings.append(
        Finding(
            "audit continuity",
            (source.audit_events, source.audit_min, source.audit_max)
            == (restored.audit_events, restored.audit_min, restored.audit_max),
            f"count/min/max {source.audit_events}/{source.audit_min}/{source.audit_max} -> "
            f"{restored.audit_events}/{restored.audit_min}/{restored.audit_max}",
        )
    )

    findings.append(
        Finding(
            "outbox status distribution",
            source.outbox_by_status == restored.outbox_by_status,
            f"{source.outbox_by_status} -> {restored.outbox_by_status}",
        )
    )

    # Two separate questions, and conflating them would misreport which one is
    # broken. "The restore reproduced the mappings" is about the backup;
    # "the source has no dangling mappings" is about the database. A restore
    # that faithfully carries an orphan across is a *successful* restore of an
    # inconsistent source, and an operator needs to know which of the two to
    # go and fix.
    findings.append(
        Finding(
            "external mappings survive the restore",
            restored.orphan_refs == source.orphan_refs,
            f"orphans {source.orphan_refs} -> {restored.orphan_refs}",
        )
    )
    findings.append(
        Finding(
            "source has no dangling external mappings",
            source.orphan_refs == 0,
            (
                "none"
                if source.orphan_refs == 0
                else f"{source.orphan_refs} ref(s) point at a tenant that does not exist "
                "- source integrity, not a backup problem"
            ),
        )
    )

    for name, expected in sorted(source.constraints.items()):
        actual = restored.constraints.get(name, False)
        findings.append(
            Finding(f"resume guard {name}", actual == expected and actual, f"present={actual}")
        )

    return findings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keep", action="store_true", help="keep the scratch database")
    args = parser.parse_args()

    scratch = f"{SCRATCH_PREFIX}{int(time.time())}"
    # /tmp here is the *container's* filesystem, reached through `docker exec`;
    # nothing is written to the host's temporary directory. The dump is removed
    # by this script unless --keep is passed.
    dump_path = f"/tmp/{scratch}.dump"  # noqa: S108

    print(f"source : {SOURCE_DB} (read-only; pg_dump only)")
    print(f"target : {scratch} (created and dropped by this drill)")
    print()

    source = snapshot(SOURCE_DB)

    print("dumping ...")
    t0 = time.monotonic()
    _run(["docker", "exec", CONTAINER, "pg_dump", "-U", DB_USER, "-Fc", "-f", dump_path, SOURCE_DB])
    dump_seconds = time.monotonic() - t0

    # Re-read the source: a comparison against a copy is only meaningful if the
    # original held still while it was taken.
    drifted = differences(source, snapshot(SOURCE_DB))
    if drifted:
        _run(["docker", "exec", CONTAINER, "rm", "-f", dump_path], check=False)
        print()
        print("SOURCE WAS NOT QUIESCENT - drill not attempted")
        for name in drifted:
            print(f"  changed during the dump: {name}")
        print()
        print("This is not a restore failure. Re-run against a database that is not")
        print("being written to (stop the API, workers and test suite first).")
        return 2

    print("restoring into the scratch database ...")
    _run(["docker", "exec", CONTAINER, "createdb", "-U", DB_USER, scratch])
    try:
        t1 = time.monotonic()
        # `--no-owner` because the restore runs as the same superuser but a
        # future drill may not; ownership is not what is under test.
        _run(
            [
                "docker",
                "exec",
                CONTAINER,
                "pg_restore",
                "-U",
                DB_USER,
                "-d",
                scratch,
                "--no-owner",
                "--no-acl",
                dump_path,
            ]
        )
        restore_seconds = time.monotonic() - t1

        restored = snapshot(scratch)
        findings = compare(source, restored)
    finally:
        if args.keep:
            print(f"kept scratch database {scratch}")
        else:
            _run(["docker", "exec", CONTAINER, "dropdb", "-U", DB_USER, scratch], check=False)
            _run(["docker", "exec", CONTAINER, "rm", "-f", dump_path], check=False)

    failed = [f for f in findings if not f.ok]
    width = max(len(f.check) for f in findings)

    print()
    print("=" * (width + 12))
    for finding in findings:
        print(f"{'PASS' if finding.ok else 'FAIL'}  {finding.check:<{width}}  {finding.detail}")
    print("=" * (width + 12))
    print()
    print(f"tables compared      : {len(source.table_counts)}")
    print(f"tenant tables        : {len(source.tenant_counts)}")
    print(
        "RTO (this drill)     : "
        f"{restore_seconds:.1f}s restore + {dump_seconds:.1f}s dump "
        f"= {restore_seconds + dump_seconds:.1f}s"
    )
    print(
        "RPO                  : 0 at dump time by construction (a logical dump contains "
        "everything committed before it started). The real RPO is the interval between "
        "dumps, which is an operational setting - see docs/deployment-and-operations.md."
    )
    print(f"report               : {json.dumps({'checks': len(findings), 'failed': len(failed)})}")

    if failed:
        print()
        print(f"DRILL FAILED: {len(failed)} check(s) did not reproduce")
        return 1

    print()
    print("DRILL PASSED: the restore reproduced the platform")
    return 0


if __name__ == "__main__":
    sys.exit(main())
