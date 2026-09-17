"""Shared test configuration.

Windows: psycopg async requires a selector event loop. pytest-asyncio and
TestClient create their own loops, so the policy must be set before any
loop is created - a per-module import-time fix (like platform_core.main)
does not cover all cases.
"""

import sys
from collections.abc import Callable
from typing import Any

import pytest

if sys.platform == "win32":
    import asyncio

    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


def _assert_denied(resp: Any, code: str) -> dict:
    """Assert an access denial on both the transport and the body.

    Every guard in this suite exists because checking the body alone is not
    enough: five routers used to answer a policy denial with HTTP 200 plus
    an `{"error": {...}}` body, so a client that branched on the status code
    - a retry wrapper, a dashboard, a test - read a refusal as a success.
    The tests did not catch it precisely because they only asserted the
    body. Use this helper so the status can never be forgotten again.
    """
    assert resp.status_code == 403, f"expected 403, got {resp.status_code}: {resp.text[:200]}"
    payload = resp.json()
    assert payload["error"]["code"] == code, f"expected {code}, got {payload}"
    return payload


@pytest.fixture
def assert_denied() -> Callable[[Any, str], dict]:
    """Request the denial helper: `assert_denied(resp, "SOME_CODE")`."""
    return _assert_denied


@pytest.fixture(autouse=True)
def _selector_loop_policy():
    if sys.platform == "win32":
        import asyncio

        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    yield
