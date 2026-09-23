"""Probe a *running* API for concurrency and rate-limit behaviour.

Why a script rather than a pytest case: the defects this looks for only appear
with true request parallelism, and the in-process test client drives the ASGI
app through a single portal, so a test written against it can pass while the
real server races. A guard that cannot fail is worse than no guard, so the
check runs against a live process.

    APP_BASE_URL=http://127.0.0.1:8000 \
    APP_TOKEN=pt_<tenant-slug>_<user-id> \
    APP_ADMIN_DATABASE_URL=postgresql://platform:platform@localhost:5435/platform \
    ./.venv/Scripts/python.exe scripts/concurrency_probe.py

It writes and then deletes its own probe rows, and reports PASS/FAIL per check.

What it found the first time it ran (2026-09-21), before the fix: ten parallel
submissions of the same message produced **four** rows with a shared
idempotency key and **two** with distinct keys - the replay guard is a
select-then-insert, and ten concurrent readers all found nothing. The table
already held duplicate groups from earlier runs, so this was not theoretical.
"""

from __future__ import annotations

import os
import statistics
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import httpx

BASE = os.environ.get("APP_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
TOKEN = os.environ.get("APP_TOKEN", "")
DB_URL = os.environ.get(
    "APP_ADMIN_DATABASE_URL", "postgresql+psycopg://platform:platform@localhost:5435/platform"
).replace("+psycopg", "")
TENANT_SLUG = os.environ.get("APP_TENANT_SLUG", "admin-demo")

# trust_env=False: a host proxy answers localhost with 502, which reads as a
# broken API. httpx rather than urllib so the request is explicit about its
# scheme rather than flagged by the S310 audit rule.
CLIENT = httpx.Client(base_url=BASE, timeout=30.0, trust_env=False)


def open_conversation() -> str:
    """The platform ref of a probe conversation, from the session endpoint.

    `/v1/customer/conversations/{ref}/messages` takes the platform's own
    conversation id and uses it verbatim - since the double-derivation fix, a
    path segment that is not a UUID is a 400, which is what this probe's
    invented `probe-*` key collided with (all ten posts rejected, zero rows,
    and the concurrency invariant untested). The one place that mints a ref
    for a client is the session endpoint, so this asks it the way every real
    client does. A fresh visitor per run; the probe cleans up its own turns
    either way.
    """
    resp = CLIENT.post(
        "/v1/support/sessions",
        json={
            "tenant_slug": TENANT_SLUG,
            "visitor_id": "concurrency-probe-" + uuid.uuid4().hex,
        },
        headers={"Authorization": "Bearer " + TOKEN},
    )
    if resp.status_code != 200:
        raise SystemExit(
            f"could not open a probe conversation: {resp.status_code} {resp.text[:200]}"
        )
    return str(resp.json()["conversation_ref"])


def _db():
    import psycopg

    return psycopg.connect(DB_URL, autocommit=True)


def count_turns(fragment: str) -> int:
    conn = _db()
    try:
        row = conn.execute(
            "SELECT count(*) FROM conversation_turns t JOIN tenants tn ON tn.id = t.tenant_id "
            "WHERE tn.slug = %s AND t.text_redacted LIKE %s",
            (TENANT_SLUG, "%" + fragment + "%"),
        ).fetchone()
        return int(row[0])
    finally:
        conn.close()


def cleanup(fragment: str) -> None:
    conn = _db()
    try:
        conn.execute(
            "DELETE FROM conversation_turns t USING tenants tn WHERE tn.id = t.tenant_id "
            "AND tn.slug = %s AND t.text_redacted LIKE %s",
            (TENANT_SLUG, "%" + fragment + "%"),
        )
    finally:
        conn.close()


def post(path: str, body: dict, key: str) -> int:
    try:
        resp = CLIENT.post(
            path,
            json=body,
            headers={"Authorization": "Bearer " + TOKEN, "Idempotency-Key": key},
        )
        return resp.status_code
    except Exception:  # noqa: BLE001 - a connection failure is a result
        return 0


def get(path: str) -> tuple[int, float]:
    started = time.perf_counter()
    try:
        resp = CLIENT.get(path, headers={"Authorization": "Bearer " + TOKEN})
        return resp.status_code, (time.perf_counter() - started) * 1000
    except Exception:  # noqa: BLE001
        return 0, (time.perf_counter() - started) * 1000


def race(label: str, fragment: str, same_key: bool, parallel: int = 10) -> bool:
    cleanup(fragment)
    key = "probe-" + fragment
    payload = {"text": "concurrency probe " + fragment}
    # `key` is the idempotency key here; the path ref comes from the session
    # endpoint (see `open_conversation`).
    ref = open_conversation()
    with ThreadPoolExecutor(max_workers=parallel) as pool:
        statuses = list(
            pool.map(
                lambda i: post(
                    f"/v1/customer/conversations/{ref}/messages",
                    payload,
                    key if same_key else f"{key}-{i}",
                ),
                range(parallel),
            )
        )
    rows = count_turns(fragment)
    ok = rows == 1
    print(
        f"{'PASS' if ok else 'FAIL'}  {label}: {parallel} parallel -> {rows} row(s); "
        f"statuses={ {s: statuses.count(s) for s in set(statuses)} }",
        flush=True,
    )
    cleanup(fragment)
    return ok


def main() -> int:
    if not TOKEN:
        print("APP_TOKEN is required (pt_<tenant-slug>_<user-id>)", file=sys.stderr)
        return 2

    results = [
        race("same key, parallel", "samekey", same_key=True),
        race("distinct keys, same text, parallel", "diffkey", same_key=False),
    ]

    for concurrency in (1, 20):
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            samples = list(
                pool.map(
                    lambda _: get("/v1/quality/metrics?window_seconds=3600"), range(concurrency * 3)
                )
            )
        ok = sorted(ms for status, ms in samples if status == 200)
        errors = [status for status, _ in samples if status != 200]
        if ok:
            print(
                f"      metrics @concurrency={concurrency}: p50={statistics.median(ok):.0f}ms "
                f"p95={ok[int(len(ok) * 0.95) - 1]:.0f}ms errors={len(errors)}"
            )
        results.append(not errors)

    # The limiter is configured at 600 requests / 60s; only a burst can show it.
    burst = int(os.environ.get("APP_PROBE_BURST", "700"))
    with ThreadPoolExecutor(max_workers=20) as pool:
        statuses = list(
            pool.map(lambda _: get("/v1/quality/metrics?window_seconds=3600")[0], range(burst))
        )
    limited = statuses.count(429)
    print(f"{'PASS' if limited else 'FAIL'}  limiter: {limited}/{burst} answered 429", flush=True)
    results.append(limited > 0)

    print(f"\nSUMMARY {sum(results)}/{len(results)} checks passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
