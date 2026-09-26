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

# Import the model registry before anything maps a model.
#
# `models_registry` exists so that `Base.metadata` is complete whenever a
# process can open a session - its own docstring says so, and `platform_core.db`
# imports it for exactly that reason. Tests that build their own engine (or
# import a model module directly) bypass `db`, and then the completeness depends
# on **collection order**: if `agent_runtime.models` is mapped before
# `cases.canned_models`, SQLAlchemy resolves `conversation_turns.canned_reply_id`
# against a metadata that has no `canned_replies` table and raises
#
#     NoReferencedTableError: Foreign key associated with column
#     'conversation_turns.canned_reply_id' could not find table 'canned_replies'
#
# Measured 2026-09-23: 13 tests in `unit/tool_gateway/test_gateway.py` failed in
# a batch run and passed alone, with a different set each time, which reads as
# flakiness and is not - it is one import that has to happen first. Four files
# trigger it (`test_intent`, `test_channel_dispatch`, `test_prompt_release`,
# `test_rerank_flag`), all of which map a model without going through `db`.
#
# Doing it here rather than in each test file makes the suite order-independent,
# which is the property that was actually missing.
import platform_core.models_registry  # noqa: E402, F401  (side-effect import)

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
