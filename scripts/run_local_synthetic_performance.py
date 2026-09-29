"""Compare local off/shadow ingress and enqueue latency under synthetic load.

Run against the repository's local PostgreSQL service:

    python scripts/run_local_synthetic_performance.py

The runner creates a disposable database, applies current migrations, then
measures three warm-pool rounds at concurrency 50 and 100 for both semantic
off and shadow modes. It exercises the inbox persistence function and the
real shadow enqueue gate, but does not call a model or measure production
HTTP/network latency. The database is dropped in `finally`.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path
from statistics import median
from typing import Any

import psycopg
from alembic import command
from alembic.config import Config
from psycopg import sql as psycopg_sql
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

REPO_ROOT = Path(__file__).resolve().parents[1]
ADMIN_DSN_BASE = "postgresql://platform:platform@127.0.0.1:5435"
ADMIN_SQLALCHEMY_BASE = "postgresql+psycopg://platform:platform@127.0.0.1:5435"
POOL_SIZE = 10
ROUNDS = 3
CONCURRENCIES = (50, 100)
MODES = ("off", "shadow")
FLAG_SHADOW = "agent.semantic_shadow"
EVIDENCE_DIR = Path(
    os.environ.get(
        "LOCAL_PERF_EVIDENCE_DIR",
        str(REPO_ROOT / "docs/implementation/ai-support-v2/evidence/local-perf-2026-09-29"),
    )
)


class PerformanceRunError(RuntimeError):
    """Raised when local benchmark invariants fail."""


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


def _percentile(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    index = max(0, math.ceil(percentile * len(ordered)) - 1)
    return round(ordered[index], 3)


def _ensure_source_paths() -> None:
    for path in (
        "apps/api/src",
        "apps/worker/src",
        "packages/policy/src",
        "packages/contracts/src",
        "packages/observability/src",
    ):
        sys.path.insert(0, str(REPO_ROOT / path))


def _prepare_flags(admin_dsn: str, tenants: dict[str, uuid.UUID]) -> None:
    now = int(time.time())
    with psycopg.connect(admin_dsn) as connection:
        for mode, tenant_id in tenants.items():
            connection.execute(
                "INSERT INTO tenants (id, slug, name, status) VALUES (%s, %s, %s, 'active')",
                (tenant_id, f"local-perf-{mode}-{tenant_id.hex[:8]}", f"Local perf {mode}"),
            )
            connection.execute(
                "INSERT INTO feature_flags "
                "(id, tenant_id, key, description, enabled, rollout_percent, created_at) "
                "VALUES (%s, %s, %s, %s, %s, 100, %s)",
                (
                    uuid.uuid4(),
                    tenant_id,
                    FLAG_SHADOW,
                    "Synthetic local performance mode comparison",
                    mode == "shadow",
                    now,
                ),
            )


async def _run_round(
    *,
    engine: Any,
    tenant_id: uuid.UUID,
    mode: str,
    concurrency: int,
    round_number: int,
) -> dict[str, Any]:
    from platform_core.agent_runtime.semantic.shadow import SHADOW_EVENT_TYPE
    from platform_core.support_bridge.inbox import persist_inbox_event
    from worker.inbox_consumer import _enqueue_shadow

    factory = async_sessionmaker(engine, expire_on_commit=False)
    prefix = f"perf-{mode}-{concurrency}-{round_number}"
    conversation_ids = [
        uuid.uuid5(uuid.NAMESPACE_URL, f"{tenant_id}:{prefix}:conversation:{index}")
        for index in range(concurrency)
    ]
    delivery_ids = [f"{prefix}-delivery-{index}" for index in range(concurrency)]
    async with factory() as session:
        await session.execute(
            text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
            {"tenant_id": str(tenant_id)},
        )
        shadow_rows_before = int(
            (
                await session.execute(
                    text(
                        "SELECT count(*) FROM outbox_events "
                        "WHERE tenant_id = :tenant_id AND event_type = :event_type"
                    ),
                    {"tenant_id": tenant_id, "event_type": SHADOW_EVENT_TYPE},
                )
            ).scalar_one()
        )

    async def persist_one(index: int) -> float:
        started = time.perf_counter()
        async with factory() as session:
            await session.execute(
                text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
                {"tenant_id": str(tenant_id)},
            )
            await persist_inbox_event(
                session,
                tenant_id=tenant_id,
                delivery_id=delivery_ids[index],
                event_type="message_created",
                raw_body=b"local synthetic performance fixture",
                minimized_payload={
                    "message_id": delivery_ids[index],
                    "message_type": "incoming",
                    "conversation_id": str(conversation_ids[index]),
                },
            )
            await session.commit()
        return (time.perf_counter() - started) * 1000

    inbox_ack_ms = list(await asyncio.gather(*(persist_one(index) for index in range(concurrency))))

    async def enqueue_shadow(index: int) -> tuple[float, bool]:
        turn_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{tenant_id}:{prefix}:turn:{index}"))
        started = time.perf_counter()
        async with factory() as session:
            await session.execute(
                text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
                {"tenant_id": str(tenant_id)},
            )
            enqueued = await _enqueue_shadow(
                session,
                tenant_id=tenant_id,
                conversation_ref_id=conversation_ids[index],
                turn_id=turn_id,
            )
            await session.commit()
        return (time.perf_counter() - started) * 1000, enqueued

    enqueue_results = await asyncio.gather(*(enqueue_shadow(index) for index in range(concurrency)))
    enqueue_latency_ms = [latency for latency, _ in enqueue_results]
    shadow_rows_written = sum(1 for _, enqueued in enqueue_results if enqueued)
    expected_shadow_rows = concurrency if mode == "shadow" else 0
    if shadow_rows_written != expected_shadow_rows:
        raise PerformanceRunError(
            f"mode {mode} enqueued {shadow_rows_written} shadow events; "
            f"expected {expected_shadow_rows}"
        )

    async with factory() as session:
        await session.execute(
            text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
            {"tenant_id": str(tenant_id)},
        )
        replay = await persist_inbox_event(
            session,
            tenant_id=tenant_id,
            delivery_id=delivery_ids[0],
            event_type="message_created",
            raw_body=b"local synthetic performance fixture",
            minimized_payload={
                "message_id": delivery_ids[0],
                "message_type": "incoming",
                "conversation_id": str(conversation_ids[0]),
            },
        )
        await session.commit()
    if not replay.duplicate:
        raise PerformanceRunError("delivery replay did not resolve as duplicate")

    async with factory() as session:
        await session.execute(
            text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
            {"tenant_id": str(tenant_id)},
        )
        outbox_count = int(
            (
                await session.execute(
                    text(
                        "SELECT count(*) FROM outbox_events "
                        "WHERE tenant_id = :tenant_id AND event_type = :event_type"
                    ),
                    {"tenant_id": tenant_id, "event_type": SHADOW_EVENT_TYPE},
                )
            ).scalar_one()
        )
    if outbox_count - shadow_rows_before != expected_shadow_rows:
        raise PerformanceRunError(
            f"shadow outbox added {outbox_count - shadow_rows_before} rows for {mode}; "
            f"expected {expected_shadow_rows}"
        )

    return {
        "mode": mode,
        "round": round_number + 1,
        "concurrency": concurrency,
        "poolSize": POOL_SIZE,
        "inboxStoreAckMs": {
            "p50": round(median(inbox_ack_ms), 3),
            "p95": _percentile(inbox_ack_ms, 0.95),
            "p99": _percentile(inbox_ack_ms, 0.99),
        },
        "shadowEnqueueMs": {
            "p50": round(median(enqueue_latency_ms), 3),
            "p95": _percentile(enqueue_latency_ms, 0.95),
            "p99": _percentile(enqueue_latency_ms, 0.99),
        },
        "shadowRequestsQueued": shadow_rows_written,
        "duplicateReplayDetected": replay.duplicate,
    }


async def _measure(admin_url: str, tenants: dict[str, uuid.UUID]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for mode in MODES:
        engine = create_async_engine(admin_url, pool_size=POOL_SIZE, max_overflow=0)
        warm = [await engine.connect() for _ in range(POOL_SIZE)]
        for connection in warm:
            await connection.execute(text("SELECT 1"))
        for connection in warm:
            await connection.close()
        try:
            for concurrency in CONCURRENCIES:
                for round_number in range(ROUNDS):
                    result = await _run_round(
                        engine=engine,
                        tenant_id=tenants[mode],
                        mode=mode,
                        concurrency=concurrency,
                        round_number=round_number,
                    )
                    results.append(result)
                    print(
                        f"mode={mode} concurrency={concurrency} round={round_number + 1} "
                        f"inbox_p95_ms={result['inboxStoreAckMs']['p95']} "
                        f"shadow_enqueue_p95_ms={result['shadowEnqueueMs']['p95']} "
                        f"queued={result['shadowRequestsQueued']}"
                    )
        finally:
            await engine.dispose()
    return results


def main() -> int:
    database = f"codex_r2r3_perf_{uuid.uuid4().hex[:8]}"
    admin_dsn = f"{ADMIN_DSN_BASE}/{database}"
    admin_url = f"{ADMIN_SQLALCHEMY_BASE}/{database}"
    app_url = f"postgresql+psycopg://platform_app:platform_app@127.0.0.1:5435/{database}"
    maintenance_dsn = f"{ADMIN_DSN_BASE}/postgres"
    tenants = {mode: uuid.uuid4() for mode in MODES}
    created = False
    old_environment = os.environ.copy()
    try:
        with psycopg.connect(maintenance_dsn, autocommit=True) as connection:
            connection.execute(
                psycopg_sql.SQL("CREATE DATABASE {}").format(psycopg_sql.Identifier(database))
            )
        created = True
        os.environ.update(
            {
                "APP_ENVIRONMENT": "test",
                "APP_BUSINESS_API_ADAPTER": "demo",
                "APP_ALLOW_BOOTSTRAP_TOKENS": "true",
                "APP_DATABASE_URL": admin_url,
                "APP_DATABASE_APP_URL": admin_url,
                "APP_ADMIN_DATABASE_URL": admin_url,
                "APP_TEST_DATABASE_URL": admin_url,
                "PYTHONDONTWRITEBYTECODE": "1",
            }
        )
        _ensure_source_paths()
        config_path = REPO_ROOT / "apps/api/migrations/alembic.ini"
        config = Config(str(config_path))
        config.set_main_option("script_location", str(config_path.parent))
        command.upgrade(config, "head")
        _prepare_flags(admin_dsn, tenants)
        os.environ["APP_DATABASE_APP_URL"] = app_url
        rounds = asyncio.run(_measure(app_url, tenants))
        document = {
            "evidenceType": "local synthetic performance comparison",
            "implementationRevision": _code_identity(),
            "database": "disposable local PostgreSQL database",
            "roundsPerModeConcurrency": ROUNDS,
            "modes": list(MODES),
            "concurrencyLevels": list(CONCURRENCIES),
            "measurement": "warm pooled inbox persistence plus real shadow outbox enqueue gate",
            "modelCalls": 0,
            "httpTransportMeasured": False,
            "workerCompletionLatencyMeasured": False,
            "runs": rounds,
            "limitations": [
                "Synthetic local data and one host; not a production capacity or latency claim.",
                "Inbox metric measures persist_inbox_event, not socket, signature, "
                "or full API latency.",
                "Shadow enqueue is measured after inbox persistence; no model is invoked.",
                "Multi-worker queue drain latency is captured by the separate local worker drill.",
            ],
        }
        EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
        report_path = EVIDENCE_DIR / "performance-comparison.json"
        report_path.write_text(
            json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(f"local_synthetic_performance=passed report={report_path}")
        return 0
    finally:
        os.environ.clear()
        os.environ.update(old_environment)
        if created:
            with psycopg.connect(maintenance_dsn, autocommit=True) as connection:
                connection.execute(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = %s AND pid <> pg_backend_pid()",
                    (database,),
                )
                connection.execute(
                    psycopg_sql.SQL("DROP DATABASE IF EXISTS {}").format(
                        psycopg_sql.Identifier(database)
                    )
                )


if __name__ == "__main__":
    raise SystemExit(main())
