"""Database connection-pool sizing is an explicit deployment setting."""

from types import SimpleNamespace
from typing import Any

from platform_core import db


def test_engine_uses_role_specific_configured_pool_caps(monkeypatch: Any) -> None:
    captured: list[dict[str, Any]] = []
    monkeypatch.setattr(
        db,
        "get_settings",
        lambda: SimpleNamespace(
            database_url="postgresql+psycopg://owner@db/platform",
            app_database_url="postgresql+psycopg://app@db/platform",
            database_pool_size=7,
            database_max_overflow=3,
            app_database_pool_size=25,
            app_database_max_overflow=0,
            # Added with the explicit checkout timeout; this fake has to carry
            # it or `create_engine` raises AttributeError before it can assert
            # anything. The stub is a `SimpleNamespace` precisely because it
            # fails loudly on a missing field - which is how this was found.
            database_pool_timeout=2.5,
        ),
    )
    monkeypatch.setattr(
        db,
        "create_async_engine",
        lambda url, **kwargs: captured.append({"url": url, **kwargs}) or object(),
    )

    db.create_engine("postgresql+psycopg://app@db/platform")
    db.create_engine()

    assert captured[0]["url"] == "postgresql+psycopg://app@db/platform"
    assert captured[0]["pool_size"] == 25
    assert captured[0]["max_overflow"] == 0
    assert captured[1]["url"] == "postgresql+psycopg://owner@db/platform"
    assert captured[1]["pool_size"] == 7
    assert captured[1]["max_overflow"] == 3
    assert all(config["pool_pre_ping"] is True for config in captured)
    # Both roles get the same checkout timeout, unlike the size caps: the
    # timeout is about how long a caller waits, and a caller does not care which
    # role the request runs under. It is asserted on both engines because a
    # value passed to one and not the other would leave the other on the 30
    # second default - which is the failure this whole change is about.
    assert all(config["pool_timeout"] == 2.5 for config in captured)
