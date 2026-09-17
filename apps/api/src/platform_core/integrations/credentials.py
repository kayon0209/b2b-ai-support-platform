"""Connector credential resolution.

`credential_ref` is a *reference*, never a secret (docs/integrations.md:
"Credentials are secret references, not application fields"). Nothing
dereferenced it, so every adapter received `credentials={}`: the tool gateway
could not authenticate to a real Jira or CRM at all, and `health_check` could
never succeed. The registry deliberately does not read secrets itself, so
resolution is injected as a function instead.

Pilot scheme (the only one supported here):

    env://VAR_NAME

The environment variable's value is either a JSON object, used as the
credentials mapping (`{"api_token": "...", "user_email": "..."}`), or a bare
token, returned as `{"api_token": value}`.

Every other scheme -- including the `vault://` refs the tests use -- resolves
to no credentials. That is fail-closed on purpose: an adapter then sends no
Authorization header and the call fails loudly, rather than looking like a
real attempt with an empty credential. A production deployment swaps this
function for a secret-manager client; the call sites do not change.
"""

import json
import os
from typing import Any

ENV_SCHEME = "env://"


def resolve_credentials(credential_ref: str | None) -> dict[str, str]:
    """Resolve a credential reference to a credentials mapping.

    Returns `{}` when the reference is empty, uses an unsupported scheme, or
    points at an unset variable -- never a partial or placeholder value.
    """
    if not credential_ref:
        return {}
    if not credential_ref.startswith(ENV_SCHEME):
        return {}

    var_name = credential_ref[len(ENV_SCHEME) :].strip()
    if not var_name:
        return {}

    raw = os.environ.get(var_name)
    if not raw:
        return {}

    parsed = _maybe_json(raw)
    if isinstance(parsed, dict):
        # Only string values: an adapter expects a header-shaped mapping, and
        # coercing nested structures would hide a misconfigured secret.
        return {str(k): str(v) for k, v in parsed.items() if isinstance(v, str)}
    return {"api_token": raw}


def _maybe_json(raw: str) -> Any:
    stripped = raw.strip()
    if not stripped.startswith("{"):
        return None
    try:
        return json.loads(stripped)
    except ValueError:
        return None


__all__ = ["ENV_SCHEME", "resolve_credentials"]
