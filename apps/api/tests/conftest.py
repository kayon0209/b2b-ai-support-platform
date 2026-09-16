"""Shared test configuration.

Windows: psycopg async requires a selector event loop. pytest-asyncio and
TestClient create their own loops, so the policy must be set before any
loop is created - a per-module import-time fix (like platform_core.main)
does not cover all cases.
"""

import sys

import pytest

if sys.platform == "win32":
    import asyncio

    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


@pytest.fixture(autouse=True)
def _selector_loop_policy():
    if sys.platform == "win32":
        import asyncio

        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    yield
