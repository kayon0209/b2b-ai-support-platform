"""Verify local Keycloak JWT authentication and app-side membership revocation.

Run from a checkout with Docker Compose and local PostgreSQL available:

    python scripts/run_local_keycloak_revocation.py

The runner creates a random disposable Keycloak realm and PostgreSQL database,
requests a real signed access token from the local Keycloak instance, verifies
an authenticated API request, suspends its tenant membership, and confirms
the same still-valid JWT is denied on the next request. It does not claim to
test enterprise IdP session or token revocation.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
from alembic import command
from alembic.config import Config
from psycopg import sql as psycopg_sql

REPO_ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILE = REPO_ROOT / "infra/compose/docker-compose.yml"
BASE_URL = "http://127.0.0.1:8081"
ADMIN_DATABASE_DSN = "postgresql://platform:platform@127.0.0.1:5435"
ADMIN_DATABASE_URL = "postgresql+psycopg://platform:platform@127.0.0.1:5435"
EVIDENCE_DIR = Path(
    os.environ.get(
        "LOCAL_OIDC_EVIDENCE_DIR",
        str(REPO_ROOT / "docs/implementation/ai-support-v2/evidence/local-keycloak-2026-09-29"),
    )
)


class LocalOidcError(RuntimeError):
    """Raised when local Keycloak or the API does not meet the expected contract."""


def _run(argv: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(  # noqa: S603 - fixed local docker/git commands
        argv,
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    if check and result.returncode:
        raise LocalOidcError(
            f"command failed ({result.returncode}): {argv!r}\n{result.stderr[-2000:]}"
        )
    return result


def _http(
    url: str,
    *,
    method: str = "GET",
    data: bytes | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 10.0,
) -> tuple[int, bytes]:
    request = urllib.request.Request(  # noqa: S310 - request URL is fixed loopback Keycloak
        url, data=data, headers=headers or {}, method=method
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - loopback Keycloak only
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def _json_request(
    url: str,
    *,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    token: str | None = None,
) -> tuple[int, dict[str, Any] | None]:
    headers = {"Accept": "application/json"}
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    status, body = _http(url, method=method, data=data, headers=headers)
    try:
        return status, json.loads(body) if body else None
    except ValueError:
        return status, None


def _wait_keycloak(timeout_seconds: float = 120.0) -> None:
    url = f"{BASE_URL}/realms/master/.well-known/openid-configuration"
    deadline = time.monotonic() + timeout_seconds
    last_error: object = "no response"
    while time.monotonic() < deadline:
        try:
            status, body = _http(url, timeout=3)
            if status == 200 and json.loads(body).get("issuer"):
                return
            last_error = f"HTTP {status}"
        except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
            last_error = exc
        time.sleep(1)
    raise LocalOidcError(f"local Keycloak did not become ready: {last_error}")


def _admin_token() -> str:
    form = urllib.parse.urlencode(
        {
            "client_id": "admin-cli",
            "grant_type": "password",
            "username": "admin",
            "password": "admin",
        }
    ).encode("ascii")
    status, body = _http(
        f"{BASE_URL}/realms/master/protocol/openid-connect/token",
        method="POST",
        data=form,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    document = json.loads(body) if body else {}
    token = document.get("access_token")
    if status != 200 or not isinstance(token, str):
        raise LocalOidcError(f"could not obtain local Keycloak admin token: HTTP {status}")
    return token


def _create_realm(admin_token: str, realm: str) -> None:
    status, _ = _json_request(
        f"{BASE_URL}/admin/realms",
        method="POST",
        payload={"realm": realm, "enabled": True},
        token=admin_token,
    )
    if status not in (201, 204):
        raise LocalOidcError(f"could not create disposable Keycloak realm: HTTP {status}")


def _seed_keycloak_client_and_user(admin_token: str, realm: str) -> tuple[str, str]:
    base = f"{BASE_URL}/admin/realms/{urllib.parse.quote(realm)}"
    status, _ = _json_request(
        f"{base}/clients",
        method="POST",
        payload={
            "clientId": "platform-api",
            "enabled": True,
            "publicClient": True,
            "directAccessGrantsEnabled": True,
            "standardFlowEnabled": False,
        },
        token=admin_token,
    )
    if status not in (201, 204):
        raise LocalOidcError(f"could not create local OIDC client: HTTP {status}")

    username = "local-reviewer"
    password = f"local-only-{uuid.uuid4().hex}"
    status, _ = _json_request(
        f"{base}/users",
        method="POST",
        payload={
            "username": username,
            "enabled": True,
            "email": f"{username}@example.test",
            "emailVerified": True,
            "firstName": "Local",
            "lastName": "Reviewer",
            "requiredActions": [],
        },
        token=admin_token,
    )
    if status not in (201, 204):
        raise LocalOidcError(f"could not create local OIDC user: HTTP {status}")

    query = urllib.parse.urlencode({"username": username, "exact": "true"})
    status, body = _http(
        f"{base}/users?{query}",
        headers={"Authorization": f"Bearer {admin_token}", "Accept": "application/json"},
    )
    users = json.loads(body) if body else []
    user_id = users[0].get("id") if status == 200 and users else None
    if not isinstance(user_id, str):
        raise LocalOidcError(f"could not resolve local OIDC user id: HTTP {status}")
    status, _ = _json_request(
        f"{base}/users/{urllib.parse.quote(user_id)}",
        method="PUT",
        payload={
            "id": user_id,
            "username": username,
            "enabled": True,
            "email": f"{username}@example.test",
            "emailVerified": True,
            "firstName": "Local",
            "lastName": "Reviewer",
            "requiredActions": [],
        },
        token=admin_token,
    )
    if status not in (200, 204):
        raise LocalOidcError(f"could not clear local OIDC required actions: HTTP {status}")
    status, _ = _json_request(
        f"{base}/users/{urllib.parse.quote(user_id)}/reset-password",
        method="PUT",
        payload={"type": "password", "value": password, "temporary": False},
        token=admin_token,
    )
    if status not in (200, 204):
        raise LocalOidcError(f"could not set local OIDC password: HTTP {status}")
    return username, password


def _issue_access_token(realm: str, username: str, password: str) -> str:
    form = urllib.parse.urlencode(
        {
            "client_id": "platform-api",
            "grant_type": "password",
            "username": username,
            "password": password,
            "scope": "openid profile email",
        }
    ).encode("ascii")
    status, body = _http(
        f"{BASE_URL}/realms/{urllib.parse.quote(realm)}/protocol/openid-connect/token",
        method="POST",
        data=form,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    document = json.loads(body) if body else {}
    access_token = document.get("access_token")
    if status != 200 or not isinstance(access_token, str):
        detail = "/".join(
            str(document.get(key)) for key in ("error", "error_description") if document.get(key)
        )
        raise LocalOidcError(
            f"could not issue local Keycloak access token: HTTP {status} {detail}".strip()
        )
    return access_token


def _create_database(database: str) -> tuple[str, str]:
    admin_dsn = f"{ADMIN_DATABASE_DSN}/{database}"
    admin_url = f"{ADMIN_DATABASE_URL}/{database}"
    with psycopg.connect(f"{ADMIN_DATABASE_DSN}/postgres", autocommit=True) as connection:
        connection.execute(
            psycopg_sql.SQL("CREATE DATABASE {}").format(psycopg_sql.Identifier(database))
        )
    return admin_dsn, admin_url


def _seed_identity(
    admin_dsn: str, *, realm_issuer: str, subject: str
) -> tuple[uuid.UUID, uuid.UUID]:
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    with psycopg.connect(admin_dsn) as connection:
        connection.execute(
            "INSERT INTO tenants (id, slug, name, status) VALUES (%s, %s, %s, 'active')",
            (tenant_id, f"local-oidc-{tenant_id.hex[:10]}", "Local Keycloak acceptance"),
        )
        connection.execute(
            "INSERT INTO users (id, primary_email, display_name, is_service_account) "
            "VALUES (%s, %s, %s, false)",
            (user_id, f"oidc-{user_id.hex[:12]}@example.test", "Local OIDC reviewer"),
        )
        connection.execute(
            "INSERT INTO memberships (id, tenant_id, user_id, role, status) "
            "VALUES (%s, %s, %s, 'tenant_owner', 'active')",
            (uuid.uuid4(), tenant_id, user_id),
        )
        connection.execute(
            "INSERT INTO external_identities (id, tenant_id, user_id, system, subject) "
            "VALUES (%s, %s, %s, %s, %s)",
            (uuid.uuid4(), tenant_id, user_id, realm_issuer, subject),
        )
    return tenant_id, user_id


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
    docker = shutil.which("docker")
    if docker is None:
        raise LocalOidcError("Docker CLI is required for the local Keycloak drill")
    compose = [docker, "compose", "--file", str(COMPOSE_FILE)]
    realm = f"r2r3-local-{uuid.uuid4().hex[:8]}"
    database = f"codex_r2r3_oidc_{uuid.uuid4().hex[:8]}"
    realm_issuer = f"{BASE_URL}/realms/{realm}"
    started_keycloak_here = False
    realm_created = False
    database_created = False
    admin_token: str | None = None
    client: Any | None = None
    access_token: str | None = None
    request_status_before: int | None = None
    request_status_after: int | None = None

    try:
        keycloak_id = _run([*compose, "ps", "-q", "keycloak"]).stdout.strip()
        if keycloak_id:
            running = _run(
                [docker, "inspect", "--format", "{{.State.Running}}", keycloak_id]
            ).stdout.strip()
        else:
            running = "false"
        if running != "true":
            _run([*compose, "up", "-d", "keycloak"])
            started_keycloak_here = True

        _wait_keycloak()
        admin_token = _admin_token()
        _create_realm(admin_token, realm)
        realm_created = True
        username, password = _seed_keycloak_client_and_user(admin_token, realm)
        access_token = _issue_access_token(realm, username, password)
        status, user_info = _json_request(
            f"{realm_issuer}/protocol/openid-connect/userinfo",
            token=access_token,
        )
        subject = user_info.get("sub") if status == 200 and user_info else None
        if not isinstance(subject, str) or not subject:
            raise LocalOidcError(f"local Keycloak userinfo did not return a subject: HTTP {status}")

        admin_dsn, admin_url = _create_database(database)
        database_created = True
        app_url = f"postgresql+psycopg://platform_app:platform_app@127.0.0.1:5435/{database}"
        os.environ.update(
            {
                "APP_ENVIRONMENT": "local",
                "APP_OIDC_ISSUER": realm_issuer,
                "APP_OIDC_AUDIENCE": "platform-api",
                "APP_ALLOW_BOOTSTRAP_TOKENS": "false",
                "APP_DATABASE_URL": admin_url,
                "APP_DATABASE_APP_URL": app_url,
                "APP_ADMIN_DATABASE_URL": admin_url,
                "APP_TEST_DATABASE_URL": app_url,
                "APP_REDIS_URL": "redis://127.0.0.1:6380/0",
                "APP_SECRET_KEY": "local-keycloak-drill-only",
                "PYTHONDONTWRITEBYTECODE": "1",
            }
        )
        sys.path[0:0] = [
            str(REPO_ROOT / path)
            for path in (
                "apps/api/src",
                "apps/worker/src",
                "packages/policy/src",
                "packages/contracts/src",
                "packages/observability/src",
            )
        ]
        config_path = REPO_ROOT / "apps/api/migrations/alembic.ini"
        config = Config(str(config_path))
        config.set_main_option("script_location", str(config_path.parent))
        command.upgrade(config, "head")
        tenant_id, user_id = _seed_identity(admin_dsn, realm_issuer=realm_issuer, subject=subject)

        from fastapi.testclient import TestClient

        from platform_core.main import app

        client = TestClient(app, raise_server_exceptions=False)
        headers = {"Authorization": f"Bearer {access_token}"}
        first = client.get("/v1/cases", headers=headers)
        request_status_before = first.status_code
        if first.status_code != 200:
            raise LocalOidcError(
                "a locally signed Keycloak JWT should resolve to the active membership; "
                f"API returned HTTP {first.status_code}: {first.text[:500]}"
            )

        with psycopg.connect(admin_dsn) as connection:
            connection.execute(
                "UPDATE memberships SET status = 'suspended' WHERE tenant_id = %s AND user_id = %s",
                (tenant_id, user_id),
            )
        second = client.get("/v1/cases", headers=headers)
        request_status_after = second.status_code
        if second.status_code != 401:
            raise LocalOidcError(
                "a suspended membership should reject the still-valid JWT on the next request; "
                f"API returned HTTP {second.status_code}"
            )

        report = {
            "evidenceType": "local Keycloak signed-token and membership-revocation simulation",
            "implementationRevision": _code_identity(),
            "issuer": realm_issuer,
            "signedJwtIssuedByLocalKeycloak": True,
            "apiStatusBeforeMembershipSuspension": request_status_before,
            "apiStatusAfterMembershipSuspension": request_status_after,
            "sameJwtReusedAfterSuspension": True,
            "enterpriseIdpSessionOrTokenRevocationTested": False,
            "tokenOrPasswordIncluded": False,
            "limitations": [
                "Local Keycloak dev realm and synthetic identity only.",
                "The platform re-checks membership on each API request; this does not prove "
                "an enterprise IdP invalidates an already-issued JWT.",
            ],
            "completedAtUtc": datetime.now(UTC).isoformat(),
        }
        EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
        report_path = EVIDENCE_DIR / "keycloak-membership-revocation.json"
        report_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(
            "local_keycloak_membership_revocation=passed "
            f"before={first.status_code} after={second.status_code}"
        )
        print(f"report={report_path}")
        return 0
    finally:
        if client is not None:
            client.close()
        if database_created:
            with psycopg.connect(f"{ADMIN_DATABASE_DSN}/postgres", autocommit=True) as connection:
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
        if realm_created and admin_token:
            _json_request(
                f"{BASE_URL}/admin/realms/{urllib.parse.quote(realm)}",
                method="DELETE",
                token=admin_token,
            )
        if started_keycloak_here:
            _run([*compose, "stop", "keycloak"], check=False)


if __name__ == "__main__":
    raise SystemExit(main())
