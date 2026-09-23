"""The bootstrap-token guard must fail closed on an undeclared environment."""

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
