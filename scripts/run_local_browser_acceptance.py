"""Save synthetic Workbench screenshots and focused accessibility evidence.

Run from an installed local project environment:

    python scripts/run_local_browser_acceptance.py

The runner creates a random local Postgres database with one synthetic
account, case and three-turn conversation, starts the current checkout's API
and Vite server on loopback, and runs Playwright/axe against that page. It
archives screenshots and an accessibility report under
`docs/implementation/ai-support-v2/evidence/`. The database and temporary
servers are removed on exit. It does not send requests outside localhost.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import psycopg
from alembic import command
from alembic.config import Config
from psycopg import sql as psycopg_sql

REPO_ROOT = Path(__file__).resolve().parents[1]
PYTHON = sys.executable
ADMIN_BASE_URL = "postgresql://platform:platform@localhost:5435/postgres"
EVIDENCE_DIR = Path(
    os.environ.get(
        "LOCAL_A11Y_EVIDENCE_DIR",
        str(REPO_ROOT / "docs/implementation/ai-support-v2/evidence/local-browser-2026-09-29"),
    )
)
CONVERSATION_ID = uuid.UUID("0190f000-0000-7000-8000-000000000010")
TENANT_ID = uuid.UUID("0190f000-0000-7000-8000-000000000001")
USER_ID = uuid.uuid5(uuid.NAMESPACE_URL, "codex-r2r3-browser-agent")
TENANT_SLUG = "codex-r2r3-a11y"


class AcceptanceError(RuntimeError):
    pass


def _free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _code_identity(git: str) -> str:
    commit = subprocess.check_output(  # noqa: S603 - fixed local git commands
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
    tracked_diff = subprocess.check_output(  # noqa: S603 - fixed local git commands
        [git, "diff", "--binary", "HEAD", "--", *source_roots], cwd=REPO_ROOT
    )
    untracked = subprocess.check_output(  # noqa: S603 - fixed local git commands
        [git, "ls-files", "--others", "--exclude-standard"], cwd=REPO_ROOT, text=True
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


def _wait_http(url: str, *, expected_status: int = 200, timeout_seconds: float = 45.0) -> None:
    deadline = time.monotonic() + timeout_seconds
    last_error: object = "no response"
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as response:  # noqa: S310 - URL is loopback-only
                if response.status == expected_status:
                    return
                last_error = f"HTTP {response.status}"
        except (urllib.error.URLError, TimeoutError) as exc:
            last_error = exc
        time.sleep(0.25)
    raise AcceptanceError(f"local service did not become ready at {url}: {last_error}")


def _seed_database(admin_url: str) -> None:
    now = int(time.time())
    account_id = uuid.uuid4()
    case_id = uuid.uuid4()
    with psycopg.connect(admin_url) as connection:
        connection.execute(
            "INSERT INTO tenants (id, slug, name, status) VALUES (%s, %s, %s, 'active')",
            (TENANT_ID, TENANT_SLUG, "Synthetic browser acceptance tenant"),
        )
        connection.execute(
            "INSERT INTO users (id, primary_email, display_name, is_service_account) "
            "VALUES (%s, %s, %s, false)",
            (USER_ID, "codex-r2r3-browser@example.test", "Demo support agent"),
        )
        connection.execute(
            "INSERT INTO memberships (id, tenant_id, user_id, role, status) "
            "VALUES (%s, %s, %s, 'support_agent', 'active')",
            (uuid.uuid4(), TENANT_ID, USER_ID),
        )
        connection.execute(
            "INSERT INTO enterprise_accounts "
            "(id, tenant_id, name, tier, contract_status, attributes, created_at, updated_at) "
            "VALUES (%s, %s, %s, 'standard', 'active', %s::jsonb, %s, %s)",
            (
                account_id,
                TENANT_ID,
                "Synthetic Demo Account",
                json.dumps({"business_system_refs": {"business_api": "acme"}}),
                now,
                now,
            ),
        )
        connection.execute(
            "INSERT INTO cases "
            "(id, tenant_id, enterprise_account_id, subject, description, category, priority, "
            "status, version, opened_at, elapsed_running_seconds, last_state_changed_at) "
            "VALUES (%s, %s, %s, %s, %s, 'general', 'p2', 'new', 1, %s, 0, %s)",
            (
                case_id,
                TENANT_ID,
                account_id,
                "Synthetic demo conversation",
                "Local accessibility review only.",
                now,
                now,
            ),
        )
        connection.execute(
            "INSERT INTO case_conversations "
            "(id, tenant_id, case_id, conversation_ref_id, relationship) "
            "VALUES (%s, %s, %s, %s, 'origin')",
            (uuid.uuid4(), TENANT_ID, case_id, CONVERSATION_ID),
        )
        connection.execute(
            "INSERT INTO conversation_control_leases "
            "(id, tenant_id, conversation_ref_id, owner_type, owner_ref, mode, lease_version, "
            "changed_reason, updated_at) "
            "VALUES (%s, %s, %s, 'human', %s, 'HUMAN_ACTIVE', 1, 'synthetic browser review', %s)",
            (uuid.uuid4(), TENANT_ID, CONVERSATION_ID, str(USER_ID), now),
        )
        turns = (
            ("customer", "我们要核验 PCB-DEMO-100 的规格和演示库存。", now - 120),
            ("agent", "我会检查本地合成资料，不会把样例当成客户承诺。", now - 80),
            ("customer", "请再让销售人工复核报价有效期。", now - 30),
        )
        for index, (role, content, created_at) in enumerate(turns):
            connection.execute(
                "INSERT INTO conversation_turns "
                "(id, tenant_id, conversation_ref_id, role, text_redacted, text_hash, ts, ref, "
                "source, origin, source_refs, created_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, '', 'channel', '', '[]'::jsonb, %s)",
                (
                    uuid.uuid5(CONVERSATION_ID, f"browser-turn-{index}"),
                    TENANT_ID,
                    CONVERSATION_ID,
                    role,
                    content,
                    hashlib.sha256(content.encode("utf-8")).hexdigest(),
                    created_at,
                    created_at,
                ),
            )
        source_turn_id = str(uuid.uuid5(CONVERSATION_ID, "browser-turn-2"))
        connection.execute(
            "INSERT INTO conversation_tasks "
            "(id, tenant_id, conversation_ref_id, source_turn_id, task_local_key, sequence, "
            "kind, status, version, action_revision, content_hash, depends_on, slots, "
            "missing_slots, created_at, updated_at) "
            "VALUES (%s, %s, %s, %s, 'browser-accessibility-task', 0, 'read', "
            "'awaiting_input', 1, 1, %s, '[]'::jsonb, '[]'::jsonb, %s::jsonb, %s, %s)",
            (
                uuid.uuid5(CONVERSATION_ID, "browser-task"),
                TENANT_ID,
                CONVERSATION_ID,
                source_turn_id,
                "a" * 64,
                json.dumps(["order_id"]),
                now - 30,
                now - 30,
            ),
        )


def _stop_processes(processes: list[subprocess.Popen[bytes]]) -> None:
    for process in processes:
        try:
            process.terminate()
        except ProcessLookupError:
            pass
    for process in processes:
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            process.wait(timeout=5)


def main() -> int:
    admin_database = f"codex_r2r3_a11y_{uuid.uuid4().hex[:8]}"
    admin_dsn = f"postgresql://platform:platform@localhost:5435/{admin_database}"
    admin_url = f"postgresql+psycopg://platform:platform@localhost:5435/{admin_database}"
    app_url = f"postgresql+psycopg://platform_app:platform_app@localhost:5435/{admin_database}"
    api_port = _free_port()
    web_port = _free_port()
    token = f"pt_{TENANT_SLUG}_{USER_ID}"
    processes: list[subprocess.Popen[bytes]] = []
    log_handles = []
    created = False
    success = False
    node = shutil.which("node")
    git = shutil.which("git")
    if node is None:
        raise AcceptanceError("Node.js must be on PATH to run Playwright")
    if git is None:
        raise AcceptanceError(
            "Git must be on PATH to bind the evidence report to the current commit"
        )
    current_commit = _code_identity(git)
    api_log = Path("/private/tmp/r2r3-browser-api.log")
    web_log = Path("/private/tmp/r2r3-browser-vite.log")

    try:
        with psycopg.connect(ADMIN_BASE_URL, autocommit=True) as connection:
            connection.execute(
                psycopg_sql.SQL("CREATE DATABASE {}").format(psycopg_sql.Identifier(admin_database))
            )
        created = True

        api_env = os.environ.copy()
        api_env.update(
            {
                "APP_ENVIRONMENT": "local",
                "APP_BUSINESS_API_ADAPTER": "demo",
                "APP_ALLOW_BOOTSTRAP_TOKENS": "true",
                "APP_DATABASE_URL": admin_url,
                "APP_DATABASE_APP_URL": app_url,
                "APP_ADMIN_DATABASE_URL": admin_url,
                "APP_TEST_DATABASE_URL": app_url,
                "APP_REDIS_URL": "redis://127.0.0.1:6380/0",
                "APP_SECRET_KEY": "local-browser-acceptance-only",
                "PYTHONPATH": os.pathsep.join(
                    str(REPO_ROOT / path)
                    for path in (
                        "apps/api/src",
                        "apps/worker/src",
                        "packages/policy/src",
                        "packages/contracts/src",
                        "packages/observability/src",
                        ".",
                    )
                ),
                "PYTHONDONTWRITEBYTECODE": "1",
            }
        )
        os.environ.update(api_env)
        migration = REPO_ROOT / "apps/api/migrations/alembic.ini"
        config = Config(str(migration))
        config.set_main_option("script_location", str(migration.parent))
        command.upgrade(config, "head")
        _seed_database(admin_dsn)

        api_log_handle = api_log.open("wb")
        log_handles.append(api_log_handle)
        processes.append(
            subprocess.Popen(  # noqa: S603 - fixed local Uvicorn command
                [
                    PYTHON,
                    "-m",
                    "uvicorn",
                    "platform_core.main:app",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(api_port),
                ],
                cwd=REPO_ROOT,
                env=api_env,
                stdout=api_log_handle,
                stderr=subprocess.STDOUT,
            )
        )
        web_env = os.environ.copy()
        web_env.update(
            {
                "VITE_API_TARGET": f"http://127.0.0.1:{api_port}",
                "VITE_API_BASE_URL": "/api",
                "VITE_API_TOKEN": token,
                "VITE_CACHE_DIR": f"/private/tmp/r2r3-vite-cache-{uuid.uuid4().hex[:8]}",
            }
        )
        web_log_handle = web_log.open("wb")
        log_handles.append(web_log_handle)
        processes.append(
            subprocess.Popen(  # noqa: S603 - fixed local Vite command
                [
                    node,
                    str(REPO_ROOT / "apps/admin-web/node_modules/vite/bin/vite.js"),
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(web_port),
                    "--configLoader",
                    "runner",
                ],
                cwd=REPO_ROOT / "apps/admin-web",
                env=web_env,
                stdout=web_log_handle,
                stderr=subprocess.STDOUT,
            )
        )
        _wait_http(f"http://127.0.0.1:{api_port}/healthz")
        _wait_http(f"http://127.0.0.1:{web_port}/admin/workbench?tab=mine")

        evidence_dir = EVIDENCE_DIR.resolve()
        browser_env = os.environ.copy()
        browser_env.update(
            {
                "WORKBENCH_URL": f"http://127.0.0.1:{web_port}/admin/workbench?tab=mine",
                "WORKBENCH_CONVERSATION_REF": str(CONVERSATION_ID),
                "LOCAL_A11Y_EVIDENCE_DIR": str(evidence_dir),
                "GIT_COMMIT": current_commit,
                "CHROME_PATH": "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            }
        )
        result = subprocess.run(  # noqa: S603 - fixed local browser script
            [node, str(REPO_ROOT / "apps/admin-web/tests/local-workbench-accessibility.mjs")],
            cwd=REPO_ROOT / "apps/admin-web",
            env=browser_env,
            check=False,
        )
        if result.returncode:
            raise AcceptanceError(
                f"Playwright/axe accessibility run failed with code {result.returncode}"
            )
        success = True
        print(f"synthetic_browser_acceptance=passed evidence_dir={evidence_dir}")
        return 0
    finally:
        _stop_processes(processes)
        for handle in log_handles:
            handle.close()
        if created:
            with psycopg.connect(ADMIN_BASE_URL, autocommit=True) as connection:
                connection.execute(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = %s AND pid <> pg_backend_pid()",
                    (admin_database,),
                )
                connection.execute(
                    psycopg_sql.SQL("DROP DATABASE IF EXISTS {}").format(
                        psycopg_sql.Identifier(admin_database)
                    )
                )
        for path in (api_log, web_log):
            if success:
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
            else:
                print(f"failure_log={path}")
        if not success:
            print("synthetic browser test environment cleaned; review failure above")


if __name__ == "__main__":
    raise SystemExit(main())
