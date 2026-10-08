"""One-command local Compose drill for duplicate outbox delivery and recovery.

Run from an installed project environment with Docker available:

    python scripts/run_local_worker_drill.py

The script creates a uniquely named Compose project with a disposable
PostgreSQL database, Redis, and two copies of the same outbox Worker. It
interrupts one Worker while a synthetic billing delivery is inside its DB
transaction, lets its peer recover the rolled-back event, replays the same
event to verify consumer idempotency, then runs the existing stale-task,
inbox-crash, rollback, and warm-pool performance tests. Every result is local
synthetic evidence; the Compose volumes are removed on exit.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
from alembic import command
from alembic.config import Config
from psycopg import sql as psycopg_sql
from uuid6 import uuid7

REPO_ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILE = REPO_ROOT / "infra/compose/docker-compose.r2-r3-drill.yml"
TESTS = (
    "apps/api/tests/integration/test_billing_ledger.py",
    "apps/api/tests/integration/test_outbox_relay.py",
    "apps/api/tests/integration/test_customer_journey_to_tasks.py",
    "apps/api/tests/integration/test_collect_fields_persistence.py",
    "apps/api/tests/integration/test_inbox_worker_isolation.py",
    "apps/api/tests/integration/test_migration_and_performance.py",
    "apps/api/tests/integration/test_agent_run_replay.py",
    "apps/api/tests/unit/agent_runtime/test_task_planner.py",
    "apps/api/tests/unit/agent_runtime/test_task_privacy.py",
    "apps/api/tests/integration/test_demo_crm_tool_gateway.py",
    "apps/api/tests/unit/integrations/test_demo_crm.py",
)
EVIDENCE_DIR = Path(
    os.environ.get(
        "LOCAL_DRILL_EVIDENCE_DIR",
        str(REPO_ROOT / "docs/implementation/ai-support-v2/evidence/local-worker-drill-2026-09-29"),
    )
)


class DrillError(RuntimeError):
    pass


def _run(
    argv: list[str],
    *,
    cwd: Path = REPO_ROOT,
    env: dict[str, str] | None = None,
    check: bool = True,
    capture: bool = True,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(  # noqa: S603 - argv is assembled from this local harness's constants
        argv,
        cwd=cwd,
        env=env,
        text=True,
        capture_output=capture,
        check=False,
    )
    if check and result.returncode:
        tail = "\n".join((result.stdout + result.stderr).splitlines()[-35:])
        raise DrillError(f"command failed ({result.returncode}): {argv!r}\n{tail}")
    return result


def _compose(project: str, compose_env: dict[str, str], *args: str, check: bool = True) -> str:
    result = _run(
        ["docker", "compose", "--project-name", project, "--file", str(COMPOSE_FILE), *args],
        env=compose_env,
        check=check,
    )
    return result.stdout.strip()


def _free_local_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _wait_for_database(host: str, port: int, *, timeout_seconds: float = 90.0) -> None:
    deadline = time.monotonic() + timeout_seconds
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with psycopg.connect(
                host=host,
                port=port,
                dbname="platform",
                user="platform",
                password="platform",  # noqa: S106 - disposable Compose-only test credential
                connect_timeout=2,
            ):
                return
        except Exception as exc:  # noqa: BLE001 - cold Compose startup is transient
            last_error = exc
            time.sleep(0.5)
    raise DrillError(f"drill Postgres did not become ready: {last_error}")


def _seed_drill_database(admin_dsn: str, tenant_id: uuid.UUID) -> tuple[uuid.UUID, uuid.UUID]:
    event_id = uuid.uuid4()
    run_id = uuid.uuid4()
    with psycopg.connect(admin_dsn) as connection:
        connection.execute(
            "INSERT INTO tenants (id, name, slug, status) VALUES (%s, %s, %s, 'active')",
            (tenant_id, "R2/R3 worker drill", f"r2r3-drill-{tenant_id.hex[:12]}"),
        )
        # A test-only gate allows the orchestrator to kill a real Compose
        # worker after it enters the billing insert, but before transaction
        # commit. The next worker must recover the rolled-back outbox row.
        connection.execute(
            "CREATE TABLE r2r3_drill_gate (id boolean PRIMARY KEY, paused boolean NOT NULL)"
        )
        connection.execute("INSERT INTO r2r3_drill_gate (id, paused) VALUES (true, true)")
        connection.execute("GRANT SELECT ON r2r3_drill_gate TO platform_app")
        connection.execute(
            "CREATE FUNCTION r2r3_pause_billing_insert() RETURNS trigger "
            "LANGUAGE plpgsql AS $$ BEGIN "
            "IF (SELECT paused FROM r2r3_drill_gate WHERE id = true) THEN "
            "PERFORM pg_sleep(30); "
            "END IF; RETURN NEW; END $$"
        )
        connection.execute(
            "CREATE TRIGGER r2r3_pause_billing_insert "
            "BEFORE INSERT ON billing_entries FOR EACH ROW "
            "EXECUTE FUNCTION r2r3_pause_billing_insert()"
        )
        payload = json.dumps({"run_id": str(run_id), "prompt_tokens": 17, "completion_tokens": 9})
        connection.execute(
            "INSERT INTO outbox_events "
            "(id, tenant_id, event_id, event_type, event_version, aggregate_type, aggregate_id, "
            "payload, status, created_at, attempts, trace_id) "
            "VALUES (%s, %s, %s, 'usage.recorded', 1, 'agent_run', %s, %s::jsonb, "
            "'queued', %s, 0, 'r2r3-worker-drill')",
            (uuid7(), tenant_id, event_id, str(run_id), payload, int(time.time())),
        )
    return event_id, run_id


def _worker_container_ips(project: str, compose_env: dict[str, str]) -> dict[str, str]:
    ids = _compose(project, compose_env, "ps", "-q", "drill-outbox-worker").splitlines()
    if len(ids) != 2:
        raise DrillError(f"expected two outbox worker containers, found {len(ids)}")
    result: dict[str, str] = {}
    for container_id in ids:
        name = (
            _run(["docker", "inspect", "--format", "{{.Name}}", container_id])
            .stdout.strip()
            .lstrip("/")
        )
        ip = _run(
            [
                "docker",
                "inspect",
                "--format",
                "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}",
                container_id,
            ]
        ).stdout.strip()
        result[ip.removeprefix("::ffff:")] = container_id
        print(f"worker_started={name} container_ip={ip}")
    return result


def _wait_for_sleeping_worker(
    admin_dsn: str, worker_ips: dict[str, str], *, timeout_seconds: float = 60.0
) -> tuple[str, str]:
    deadline = time.monotonic() + timeout_seconds
    last_sessions: list[tuple[Any, ...]] = []
    while time.monotonic() < deadline:
        with psycopg.connect(admin_dsn) as connection:
            last_sessions = connection.execute(
                "SELECT pid, client_addr::text, application_name, wait_event_type, "
                "wait_event, query "
                "FROM pg_stat_activity "
                "WHERE datname = current_database() AND state = 'active' ORDER BY query_start"
            ).fetchall()
        for row in last_sessions:
            client_ip = str(row[1]).split("/", 1)[0].removeprefix("::ffff:") if row[1] else ""
            if (
                row[4] == "PgSleep"
                and "billing_entries" in str(row[5]).lower()
                and client_ip in worker_ips
            ):
                return worker_ips[client_ip], client_ip
        time.sleep(0.25)
    raise DrillError(
        "no outbox worker reached the synthetic billing fault-injection gate; "
        f"observed_sessions={last_sessions[:3]}"
    )


def _wait_until(
    admin_dsn: str,
    event_id: uuid.UUID,
    *,
    expected_attempts: int,
    timeout_seconds: float = 60.0,
) -> tuple[str, int, int]:
    deadline = time.monotonic() + timeout_seconds
    row: tuple[Any, ...] | None = None
    count = 0
    while time.monotonic() < deadline:
        with psycopg.connect(admin_dsn) as connection:
            row = connection.execute(
                "SELECT status, attempts FROM outbox_events WHERE event_id = %s",
                (event_id,),
            ).fetchone()
            billing_row = connection.execute(
                "SELECT count(*) FROM billing_entries WHERE event_id = %s", (event_id,)
            ).fetchone()
        if billing_row is None:
            raise DrillError("billing count query returned no row")
        count = int(billing_row[0])
        if row is not None and row[0] == "sent" and int(row[1]) >= expected_attempts and count == 1:
            return str(row[0]), int(row[1]), count
        time.sleep(0.25)
    raise DrillError(
        f"outbox recovery timed out; last status={row[0] if row is not None else None}, "
        f"attempts={row[1] if row is not None else None}, billing_rows={count}"
    )


def _enqueue_usage_batch(admin_dsn: str, tenant_id: uuid.UUID, batch_size: int) -> list[uuid.UUID]:
    event_ids: list[uuid.UUID] = []
    now = int(time.time())
    with psycopg.connect(admin_dsn) as connection:
        for _ in range(batch_size):
            event_id = uuid.uuid4()
            run_id = uuid.uuid4()
            payload = json.dumps(
                {"run_id": str(run_id), "prompt_tokens": 7, "completion_tokens": 3}
            )
            connection.execute(
                "INSERT INTO outbox_events "
                "(id, tenant_id, event_id, event_type, event_version, aggregate_type, "
                "aggregate_id, "
                "payload, status, created_at, attempts, trace_id) "
                "VALUES (%s, %s, %s, 'usage.recorded', 1, 'agent_run', %s, %s::jsonb, "
                "'queued', %s, 0, 'r2r3-queue-delay')",
                (uuid7(), tenant_id, event_id, str(run_id), payload, now),
            )
            event_ids.append(event_id)
    return event_ids


def _wait_for_usage_batch(
    admin_dsn: str,
    event_ids: list[uuid.UUID],
    *,
    started_at: float,
    timeout_seconds: float = 60.0,
) -> float:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        with psycopg.connect(admin_dsn) as connection:
            pending = connection.execute(
                "SELECT count(*) FROM outbox_events WHERE event_id = ANY(%s) AND status <> 'sent'",
                (event_ids,),
            ).fetchone()
            billed = connection.execute(
                "SELECT count(*) FROM billing_entries WHERE event_id = ANY(%s)", (event_ids,)
            ).fetchone()
        if pending is None or billed is None:
            raise DrillError("queue delay batch query returned no row")
        if int(pending[0]) == 0 and int(billed[0]) == len(event_ids):
            return round((time.monotonic() - started_at) * 1000, 3)
        time.sleep(0.025)
    raise DrillError(f"queue drain timed out for batch_size={len(event_ids)}")


def _run_pytest(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    python = sys.executable
    command = [
        python,
        "-m",
        "pytest",
        "--override-ini",
        "addopts=-q --strict-markers -p no:cacheprovider "
        "-p no:pytest_plugins_release.gate_evidence",
        *TESTS,
    ]
    result = _run(command, env=env, check=False, capture=True)
    output = result.stdout + result.stderr
    print(output, end="")
    return result


def _code_identity() -> str:
    git = shutil.which("git")
    if git is None:
        return "unrecorded"
    commit = subprocess.check_output(  # noqa: S603 - fixed local git command
        [git, "rev-parse", "--short", "HEAD"], cwd=REPO_ROOT, text=True
    ).strip()
    source_roots = (
        "apps/",
        "packages/",
        "infra/",
        "scripts/",
        "tests/",
        "pyproject.toml",
        "requirements",
    )
    tracked_diff = subprocess.check_output(  # noqa: S603 - fixed local git command
        [git, "diff", "--binary", "HEAD", "--", *source_roots], cwd=REPO_ROOT
    )
    untracked = subprocess.check_output(  # noqa: S603 - fixed local git command
        [git, "ls-files", "--others", "--exclude-standard"],
        cwd=REPO_ROOT,
        text=True,
    ).splitlines()
    digest = hashlib.sha256()
    digest.update(tracked_diff)
    for relative_path in sorted(untracked):
        if not relative_path.startswith(source_roots):
            continue
        path = REPO_ROOT / relative_path
        if path.is_file():
            digest.update(relative_path.encode("utf-8"))
            digest.update(path.read_bytes())
    if not tracked_diff and not any(path.startswith(source_roots) for path in untracked):
        return commit
    return f"{commit}+source:{digest.hexdigest()[:12]}"


def main() -> int:
    started_epoch = time.time()
    project = f"codex-r2r3-worker-drill-{uuid.uuid4().hex[:8]}"
    port = _free_local_port()
    database = f"codex_r2r3_drill_{uuid.uuid4().hex[:8]}"
    compose_env = os.environ.copy()
    compose_env.update({"DRILL_POSTGRES_PORT": str(port), "DRILL_DATABASE": database})
    admin_dsn = f"postgresql://platform:platform@127.0.0.1:{port}/{database}"
    admin_url = f"postgresql+psycopg://platform:platform@127.0.0.1:{port}/{database}"
    app_url = f"postgresql+psycopg://platform_app:platform_app@127.0.0.1:{port}/{database}"
    test_env = os.environ.copy()
    artifact_dir = Path("/private/tmp") / f"{project}-artifacts"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    test_env.update(
        {
            "APP_ENVIRONMENT": "test",
            "APP_BUSINESS_API_ADAPTER": "demo",
            "APP_ALLOW_BOOTSTRAP_TOKENS": "true",
            "APP_DATABASE_URL": admin_url,
            "APP_DATABASE_APP_URL": app_url,
            "APP_ADMIN_DATABASE_URL": admin_url,
            "APP_TEST_DATABASE_URL": app_url,
            "APP_TEST_ARTIFACT_DIR": str(artifact_dir),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    compose_started = False
    try:
        print(f"compose_project={project}")
        _compose(project, compose_env, "up", "-d", "drill-postgres", "drill-redis")
        compose_started = True
        _wait_for_database("127.0.0.1", port)

        maintenance_dsn = f"postgresql://platform:platform@127.0.0.1:{port}/platform"
        with psycopg.connect(maintenance_dsn, autocommit=True) as connection:
            connection.execute(
                psycopg_sql.SQL("CREATE DATABASE {} ").format(psycopg_sql.Identifier(database))
            )

        migration = REPO_ROOT / "apps/api/migrations/alembic.ini"
        cfg = Config(str(migration))
        cfg.set_main_option("script_location", str(migration.parent))
        os.environ.update(test_env)
        command.upgrade(cfg, "head")
        tenant_id = uuid.uuid4()
        event_id, run_id = _seed_drill_database(admin_dsn, tenant_id)
        _compose(
            project,
            compose_env,
            "up",
            "-d",
            "--build",
            "--scale",
            "drill-outbox-worker=2",
            "drill-outbox-worker",
        )
        worker_ips = _worker_container_ips(project, compose_env)
        active_container, active_ip = _wait_for_sleeping_worker(admin_dsn, worker_ips)

        # Turn the pause off before killing the in-flight worker, so the peer
        # processes the retried row instead of entering the same fault gate.
        with psycopg.connect(admin_dsn) as connection:
            connection.execute("UPDATE r2r3_drill_gate SET paused = false WHERE id = true")
        _run(["docker", "kill", active_container])
        print(f"interrupted_worker_ip={active_ip}; recovery_worker_count=1")
        # The production stale-claim lease is ten minutes. The test should
        # prove recovery without sleeping through that full lease, so advance
        # only this disposable synthetic receipt past its cutoff after killing
        # the worker. This models lease expiry; it does not change worker
        # configuration or shorten the production recovery fence.
        with psycopg.connect(admin_dsn) as connection:
            expired_claim = connection.execute(
                "UPDATE outbox_events SET processing_started_at = 0 "
                "WHERE event_id = %s AND status = 'processing'",
                (event_id,),
            )
            if expired_claim.rowcount != 1:
                raise DrillError("expected the interrupted synthetic delivery to remain processing")
        status, attempts, rows = _wait_until(admin_dsn, event_id, expected_attempts=1)
        print(f"interrupted_delivery_recovered={status == 'sent'} billing_rows={rows}")

        # Requeue the same event id: the at-least-once relay must not create a
        # second billing row for the duplicate delivery.
        with psycopg.connect(admin_dsn) as connection:
            connection.execute(
                "UPDATE outbox_events SET status = 'queued', published_at = NULL "
                "WHERE event_id = %s",
                (event_id,),
            )
        status, attempts, rows = _wait_until(admin_dsn, event_id, expected_attempts=2)
        print(f"duplicate_replay={status == 'sent'} attempts={attempts} billing_rows={rows}")
        if rows != 1:
            raise DrillError("duplicate outbox delivery produced more than one billing row")

        queue_delay_runs: list[dict[str, Any]] = []
        for batch_size in (1, 10, 50):
            started_at = time.monotonic()
            batch_event_ids = _enqueue_usage_batch(admin_dsn, tenant_id, batch_size)
            elapsed_ms = _wait_for_usage_batch(admin_dsn, batch_event_ids, started_at=started_at)
            queue_delay_runs.append(
                {"batchSize": batch_size, "enqueueToDrainMs": elapsed_ms, "billingRows": batch_size}
            )
            print(f"queue_batch_size={batch_size} enqueue_to_drain_ms={elapsed_ms}")

        _compose(project, compose_env, "stop", "drill-outbox-worker", check=False)
        test_result = _run_pytest(test_env)
        if test_result.returncode:
            raise DrillError(
                "worker recovery/rollback integration tests failed with exit code "
                f"{test_result.returncode}"
            )

        # Exercise schema rollback on this same disposable database and check
        # that unrelated durable data survives the one-step down/up roundtrip.
        command.downgrade(cfg, "-1")
        with psycopg.connect(admin_dsn) as connection:
            retained_row = connection.execute(
                "SELECT count(*) FROM billing_entries WHERE event_id = %s", (event_id,)
            ).fetchone()
        if retained_row is None:
            raise DrillError("billing rollback query returned no row")
        retained = int(retained_row[0])
        command.upgrade(cfg, "head")
        with psycopg.connect(admin_dsn) as connection:
            version_row = connection.execute("SELECT version_num FROM alembic_version").fetchone()
            retained_after_row = connection.execute(
                "SELECT count(*) FROM billing_entries WHERE event_id = %s", (event_id,)
            ).fetchone()
        if version_row is None or retained_after_row is None:
            raise DrillError("rollback verification query returned no row")
        version = str(version_row[0])
        retained_after_upgrade = int(retained_after_row[0])
        if retained != 1 or retained_after_upgrade != 1:
            raise DrillError("billing ledger row was lost during the one-step migration rollback")
        print(
            "one_step_schema_rollback_roundtrip=true "
            f"version={version} billing_rows={retained_after_upgrade}"
        )
        test_output = test_result.stdout + test_result.stderr
        test_summary = re.search(
            r"(\d+ passed(?:, \d+ skipped)?(?:, \d+ deselected)?(?: in [0-9.]+s)?)",
            test_output,
        )
        performance_path = artifact_dir / "performance_report.json"
        performance_report = (
            json.loads(performance_path.read_text(encoding="utf-8"))
            if performance_path.exists()
            else None
        )
        pool_comparison_path = artifact_dir / "pool_comparison_report.json"
        pool_comparison_report = (
            json.loads(pool_comparison_path.read_text(encoding="utf-8"))
            if pool_comparison_path.exists()
            else None
        )
        report = {
            "evidenceType": "local synthetic multi-worker drill",
            "implementationRevision": _code_identity(),
            "startedAtUtc": datetime.fromtimestamp(started_epoch, UTC).isoformat(),
            "finishedAtUtc": datetime.now(UTC).isoformat(),
            "composeProject": project,
            "isolatedDatabase": database,
            "initialWorkerReplicas": 2,
            "faultInjection": {
                "inFlightWorkerKilled": True,
                "recoveredEventStatus": status,
                "recoveredEventAttempts": attempts,
                "billingRowsAfterDuplicateReplay": rows,
                "duplicateDeliveryCreatedOneBillingRow": rows == 1,
            },
            "queueDrainRuns": queue_delay_runs,
            "integrationSuiteExitCode": test_result.returncode,
            "integrationSuiteSummary": (
                test_summary.group(1) if test_summary else "passed; summary unavailable"
            ),
            "performanceSmoke": performance_report,
            "poolComparison": pool_comparison_report,
            "migrationRollback": {
                "downgradeOneRevisionThenUpgradeHead": True,
                "headVersion": version,
                "billingRowsPreserved": retained_after_upgrade,
            },
            "limitations": [
                "Synthetic data and one local Docker host; not a multi-host production drill.",
                "Fake/local handlers only; no external ERP, CRM, or provider failure was run.",
                "Queue latency is enqueue-to-drain time for synthetic usage events on this host.",
            ],
        }
        EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
        report_path = EVIDENCE_DIR / "worker-drill-report.json"
        report_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(f"synthetic_local_drill=passed report={report_path}")
        return 0
    finally:
        if compose_started:
            _compose(project, compose_env, "down", "--volumes", "--remove-orphans", check=False)


if __name__ == "__main__":
    raise SystemExit(main())
