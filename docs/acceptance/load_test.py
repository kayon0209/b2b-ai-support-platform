"""Round 3 backend load test. Usage:
  python docs/acceptance/load_test.py <url> <token> <concurrency> <seconds>
Measures p50/p95/p99 latency, throughput, error rate for three endpoints.
"""

import asyncio
import statistics
import sys
import time

import httpx

TARGETS = [
    ("GET", "/healthz", None),
    ("GET", "/v1/cases?limit=50", None),
    ("POST", "/v1/retrieval/query", {"query": "refund window", "top_k": 8}),
]


async def worker(client, method, path, body, token, deadline, latencies, errors):
    headers = {"Authorization": f"Bearer {token}"}
    while time.monotonic() < deadline:
        t0 = time.perf_counter()
        try:
            if method == "GET":
                r = await client.get(path, headers=headers)
            else:
                r = await client.post(path, headers=headers, json=body)
            dt = (time.perf_counter() - t0) * 1000
            latencies.append(dt)
            if r.status_code >= 400:
                errors.append((r.status_code, r.text[:80]))
        except Exception as exc:  # noqa: BLE001
            errors.append((0, repr(exc)[:80]))
            latencies.append((time.perf_counter() - t0) * 1000)


async def run(base, token, conc, seconds):
    async with httpx.AsyncClient(base_url=base, timeout=15) as client:
        for method, path, body in TARGETS:
            latencies: list[float] = []
            errors: list[tuple] = []
            deadline = time.monotonic() + seconds
            await asyncio.gather(
                *[worker(client, method, path, body, token, deadline, latencies, errors) for _ in range(conc)]
            )
            latencies.sort()
            n = len(latencies)
            if n == 0:
                print(f"{method} {path}: no samples")
                continue
            pct = lambda p: latencies[min(n - 1, int(n * p / 100))]  # noqa: E731
            print(
                f"{method} {path}  conc={conc}  n={n}  "
                f"p50={pct(50):.1f}ms  p95={pct(95):.1f}ms  p99={pct(99):.1f}ms  "
                f"max={latencies[-1]:.1f}ms  rps={n / seconds:.1f}  errors={len(errors)}"
            )
            for status, text in errors[:3]:
                print(f"   sample error {status}: {text}")


if __name__ == "__main__":
    base, token = sys.argv[1], sys.argv[2]
    conc, seconds = int(sys.argv[3]), float(sys.argv[4])
    asyncio.run(run(base, token, conc, seconds))
