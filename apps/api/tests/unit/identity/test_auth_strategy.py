"""Unit tests: authentication strategy selection (Phase 2, docs/security.md).

The bootstrap token scheme is unsigned: `pt_<tenant-slug>_<user-uuid>` proves
nothing, so possession of a slug and a user id is enough to impersonate that
user. It exists to make local development possible before a Keycloak realm is
available.

That makes the *selection* logic security-critical, not the token format. Two
properties have to hold:

1. OIDC wins whenever an issuer is configured, so a deployment that has a
   realm never silently falls back to the unsigned path.
2. The unsigned path can only be reached by explicitly enabling it, and
   enabling it outside local/test refuses to start rather than logging a
   warning nobody reads.

A third property, from the other direction: a deployment with neither
configured must fail at startup. Otherwise "I forgot to configure auth" would
produce a platform where every request 401s, which reads as a token bug.
"""

import pytest

from platform_core.config import Settings, _assert_auth_is_configured


def _settings(**overrides: object) -> Settings:
    """Settings built explicitly, so the developer's .env cannot affect this."""
    base: dict[str, object] = {
        "environment": "local",
        "secret_key": "test-secret",  # noqa: S106 - not a real credential
        "oidc_issuer": None,
        "allow_bootstrap_tokens": False,
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


# --- Selection ------------------------------------------------------------


def test_oidc_is_selected_when_an_issuer_is_configured() -> None:
    """A configured realm is the authentication path."""
    from platform_core.config import get_settings
    from platform_core.identity.middleware import build_resolver, oidc_token_resolver

    get_settings.cache_clear()
    try:
        import os

        os.environ["APP_OIDC_ISSUER"] = "http://localhost:8081/realms/platform"
        assert build_resolver() is oidc_token_resolver
    finally:
        os.environ.pop("APP_OIDC_ISSUER", None)
        get_settings.cache_clear()


def test_oidc_wins_over_bootstrap_tokens_even_when_both_are_enabled() -> None:
    """Enabling the dev switch must not shadow a real realm.

    The dangerous ordering is the other way round: if bootstrap tokens were
    checked first, a developer who left the flag on in staging would silently
    downgrade to unsigned auth.
    """
    from platform_core.config import get_settings
    from platform_core.identity.middleware import build_resolver, oidc_token_resolver

    get_settings.cache_clear()
    try:
        import os

        os.environ["APP_OIDC_ISSUER"] = "http://localhost:8081/realms/platform"
        os.environ["APP_ALLOW_BOOTSTRAP_TOKENS"] = "true"
        assert build_resolver() is oidc_token_resolver
    finally:
        os.environ.pop("APP_OIDC_ISSUER", None)
        os.environ.pop("APP_ALLOW_BOOTSTRAP_TOKENS", None)
        get_settings.cache_clear()


def test_bootstrap_is_selected_only_when_explicitly_enabled() -> None:
    from platform_core.config import get_settings
    from platform_core.identity.middleware import bootstrap_token_resolver, build_resolver

    get_settings.cache_clear()
    try:
        import os

        os.environ["APP_ALLOW_BOOTSTRAP_TOKENS"] = "true"
        assert build_resolver() is bootstrap_token_resolver
    finally:
        os.environ.pop("APP_ALLOW_BOOTSTRAP_TOKENS", None)
        get_settings.cache_clear()


# --- Startup gates --------------------------------------------------------


def test_no_auth_configured_refuses_to_start() -> None:
    """Silence is the failure mode being prevented.

    With neither OIDC nor the dev switch, every request would 401 and the
    cause would look like a bad token rather than a missing configuration.
    """
    with pytest.raises(RuntimeError, match="no authentication configured"):
        _assert_auth_is_configured(_settings())


@pytest.mark.parametrize("environment", ["staging", "production"])
def test_bootstrap_tokens_refused_outside_local_and_test(environment: str) -> None:
    """The unsigned path is a hard startup failure in a deployed environment."""
    with pytest.raises(RuntimeError, match="unsigned"):
        _assert_auth_is_configured(_settings(environment=environment, allow_bootstrap_tokens=True))


@pytest.mark.parametrize("environment", ["local", "test"])
def test_bootstrap_tokens_allowed_for_local_and_test(environment: str) -> None:
    _assert_auth_is_configured(_settings(environment=environment, allow_bootstrap_tokens=True))


def test_oidc_only_configuration_is_valid_anywhere() -> None:
    for environment in ("local", "test", "staging", "production"):
        _assert_auth_is_configured(
            _settings(environment=environment, oidc_issuer="https://idp.example/realm")
        )


def test_bootstrap_default_is_off() -> None:
    """Opt-in, not opt-out.

    Reading the default from a clean Settings instance (not from `.env`) is the
    point: a developer whose local env enables it must not change what a fresh
    deployment gets.
    """
    assert Settings.model_fields["allow_bootstrap_tokens"].default is False
