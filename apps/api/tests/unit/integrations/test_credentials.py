"""Unit tests: connector credential resolution.

`credential_ref` was stored but never dereferenced, so every adapter received
an empty credentials mapping and no external call could authenticate. These
pin the pilot resolver and, more importantly, its fail-closed behaviour for
anything it does not understand.
"""

from platform_core.integrations.credentials import resolve_credentials


def test_bare_token_becomes_the_api_token(monkeypatch) -> None:
    monkeypatch.setenv("CONNECTOR_TOKEN", "s3cret")
    assert resolve_credentials("env://CONNECTOR_TOKEN") == {"api_token": "s3cret"}


def test_json_object_is_used_as_the_credentials_mapping(monkeypatch) -> None:
    monkeypatch.setenv("CONNECTOR_JSON", '{"api_token": "t", "user_email": "svc@example.com"}')
    assert resolve_credentials("env://CONNECTOR_JSON") == {
        "api_token": "t",
        "user_email": "svc@example.com",
    }


def test_non_string_json_values_are_dropped(monkeypatch) -> None:
    """A nested object would hide a misconfigured secret, not describe one."""
    monkeypatch.setenv("CONNECTOR_JSON", '{"api_token": "t", "retries": 3}')
    assert resolve_credentials("env://CONNECTOR_JSON") == {"api_token": "t"}


def test_unknown_scheme_resolves_to_nothing(monkeypatch) -> None:
    """Fail closed: the adapter sends no header rather than an empty one."""
    assert resolve_credentials("vault://kv/jira") == {}


def test_unset_variable_resolves_to_nothing(monkeypatch) -> None:
    monkeypatch.delenv("CONNECTOR_MISSING", raising=False)
    assert resolve_credentials("env://CONNECTOR_MISSING") == {}


def test_empty_or_malformed_reference_resolves_to_nothing() -> None:
    assert resolve_credentials(None) == {}
    assert resolve_credentials("") == {}
    assert resolve_credentials("env://") == {}


def test_malformed_json_falls_back_to_the_raw_value(monkeypatch) -> None:
    """A token that merely starts with `{` is still a token."""
    monkeypatch.setenv("CONNECTOR_WEIRD", "{not json")
    assert resolve_credentials("env://CONNECTOR_WEIRD") == {"api_token": "{not json"}
