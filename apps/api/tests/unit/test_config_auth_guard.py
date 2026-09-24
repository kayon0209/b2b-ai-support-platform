"""The bootstrap-token guard must fail closed on an undeclared environment."""

from pathlib import Path

import pytest


def test_bootstrap_tokens_require_a_declared_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A deployment with no APP_ENVIRONMENT must not silently enable it.

    `environment` defaults to "local", so a process that never declares it is
    indistinguishable from a laptop - and the older guard ("bootstrap tokens
    only in local/test") would pass, leaving the impersonation path live.

    `_env_file=None` is the point of the test: it simulates a deployment, where
    there is no local `.env` supplying the value. (pydantic-settings counts a
    `.env` as a *source*, so with the file present the field reads as declared
    - which is why this test cannot use the ambient configuration.)

    The test creates its own premise rather than assuming it: CI exports
    APP_ENVIRONMENT for the whole job (a fresh checkout has no .env), so the
    variable is deleted here explicitly instead of the test relying on a
    process that happens not to have it.
    """
    from platform_core.config import Settings

    monkeypatch.delenv("APP_ENVIRONMENT", raising=False)

    settings = Settings(allow_bootstrap_tokens=True, _env_file=None)

    assert settings.environment == "local", "the default is still local"
    assert "environment" not in settings.model_fields_set, "but it was never declared"

    with pytest.raises(RuntimeError, match="APP_ENVIRONMENT was never declared"):
        from platform_core.config import _assert_auth_is_configured

        _assert_auth_is_configured(settings)


def test_a_declared_local_environment_still_works() -> None:
    """Local development keeps working - this is not a ban on the scheme."""
    from platform_core.config import Settings, _assert_auth_is_configured

    settings = Settings(environment="local", allow_bootstrap_tokens=True, _env_file=None)
    assert "environment" in settings.model_fields_set

    _assert_auth_is_configured(settings)  # does not raise


def test_a_deployed_environment_still_refuses_bootstrap_tokens() -> None:
    """The original guard, unchanged."""
    from platform_core.config import Settings, _assert_auth_is_configured

    settings = Settings(
        environment="production", allow_bootstrap_tokens=True, secret_key="x", _env_file=None
    )
    with pytest.raises(RuntimeError, match="only permitted in local/test"):
        _assert_auth_is_configured(settings)


def test_production_refuses_demo_business_data_and_reference_prices(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from platform_core.config import get_settings

    monkeypatch.setenv("APP_ENVIRONMENT", "production")
    monkeypatch.setenv("APP_SECRET_KEY", "local-test-secret")
    monkeypatch.setenv("APP_OIDC_ISSUER", "https://idp.example.test/realms/platform")
    monkeypatch.setenv("APP_ALLOW_BOOTSTRAP_TOKENS", "false")
    monkeypatch.setenv("APP_PRICING_RULESET", "empty")
    monkeypatch.setenv("APP_BUSINESS_API_ADAPTER", "demo")
    get_settings.cache_clear()
    try:
        with pytest.raises(RuntimeError, match="sample ERP data"):
            get_settings()
        monkeypatch.setenv("APP_BUSINESS_API_ADAPTER", "http")
        monkeypatch.setenv("APP_PRICING_RULESET", "public-reference")
        get_settings.cache_clear()
        with pytest.raises(RuntimeError, match="reference prices"):
            get_settings()
    finally:
        get_settings.cache_clear()


def test_deployed_environment_requires_dedicated_app_database_url(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from platform_core.config import get_settings

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("APP_ENVIRONMENT", "staging")
    monkeypatch.setenv("APP_SECRET_KEY", "local-test-secret")
    monkeypatch.setenv("APP_OIDC_ISSUER", "https://idp.example.test/realms/platform")
    monkeypatch.setenv("APP_ALLOW_BOOTSTRAP_TOKENS", "false")
    monkeypatch.delenv("APP_DATABASE_APP_URL", raising=False)
    get_settings.cache_clear()
    try:
        with pytest.raises(RuntimeError, match="APP_DATABASE_APP_URL is required"):
            get_settings()
    finally:
        get_settings.cache_clear()


def test_app_role_url_and_pool_limits_read_the_documented_environment_names(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from platform_core.config import get_settings

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("APP_ENVIRONMENT", "local")
    monkeypatch.setenv("APP_ALLOW_BOOTSTRAP_TOKENS", "true")
    monkeypatch.setenv(
        "APP_DATABASE_APP_URL",
        "postgresql+psycopg://platform_app:app-test@localhost:5435/platform",
    )
    monkeypatch.setenv("APP_DATABASE_APP_POOL_SIZE", "37")
    monkeypatch.setenv("APP_DATABASE_APP_MAX_OVERFLOW", "0")
    get_settings.cache_clear()
    try:
        settings = get_settings()
        assert settings.app_database_url == (
            "postgresql+psycopg://platform_app:app-test@localhost:5435/platform"
        )
        assert settings.app_database_pool_size == 37
        assert settings.app_database_max_overflow == 0
    finally:
        get_settings.cache_clear()
