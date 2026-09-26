"""Shared plumbing for signed inbound deliveries.

Two things every signed inbound route needs, and which none of them should
reimplement:

1. **Resolving a connector id to its tenant, provider, secret reference and
   status.** This must go through the `resolve_connector_for_webhook`
   `SECURITY DEFINER` function, not a plain SELECT: `connectors` is FORCE-RLS'd
   on the tenant binding that the lookup is trying to discover, so an unbound
   app-role read returns zero rows and every delivery looks like an unknown
   connector. Migrations 0015, 0016, 0018, 0019, 0026 and 0032 are the prior
   instances of that bootstrap cycle.
2. **Reading the inbound signing secret from its reference.** `None` is a
   refusal rather than an empty secret: signing with `b""` would make a forged
   request verifiable by anyone who knows the algorithm.

They live here rather than inside one router because the connector webhook and
the channel webhooks both need them, and a second copy is how two verification
paths end up disagreeing about what "authentic" means.
"""

from __future__ import annotations

import uuid
from typing import Any

from platform_core.db import app_role_url, session_scope_with_url
from platform_core.integrations.credentials import resolve_credentials

# Generic provider headers, defined once. A provider that uses different names
# needs an adapter-level translation, not a second verification implementation.
#
# These are deliberately NOT the constants in `support_bridge.webhook_security`:
# those are Chatwoot's own (`X-Chatwoot-Signature`), and reading them from a
# channel adapter would make every email look unsigned. `webhook_security` keeps
# the Chatwoot names because the Chatwoot endpoint must keep accepting them.
SIGNATURE_HEADER = "X-Webhook-Signature"
TIMESTAMP_HEADER = "X-Webhook-Timestamp"
DELIVERY_HEADER = "X-Webhook-Delivery"

# The credential key the inbound secret is read from. `resolve_credentials` maps
# a bare `env://VAR` value to `{"api_token": ...}` and a JSON object to its own
# keys, so a connector may name it either way.
SECRET_KEYS = ("webhook_secret", "api_token")


async def resolve_connector(connector_id: uuid.UUID) -> dict[str, Any] | None:
    """Resolve tenant + provider + inbound secret reference + status."""
    from sqlalchemy import text

    async with session_scope_with_url(app_role_url()) as session:
        row = (
            await session.execute(
                text(
                    "SELECT tenant_id, provider, webhook_secret_ref, status "
                    "FROM resolve_connector_for_webhook(CAST(:cid AS uuid))"
                ),
                {"cid": str(connector_id)},
            )
        ).first()
    if row is None:
        return None
    return {
        "tenant_id": row[0],
        "provider": row[1],
        "webhook_secret_ref": row[2],
        "status": row[3],
    }


def secret_bytes(credential_ref: str | None) -> bytes | None:
    """Read the inbound signing secret, or None when it cannot be resolved."""
    if not credential_ref:
        return None
    credentials = resolve_credentials(credential_ref)
    for key in SECRET_KEYS:
        value = credentials.get(key)
        if value and value.strip():
            return value.encode()
    return None
