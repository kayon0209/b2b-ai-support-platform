"""The one definition of a conversation's platform identity.

A conversation is known to Chatwoot by its own id and to this platform by a
UUID, and the two have to be joined by exactly one rule. Every writer and
reader of a conversation - the worker that answers it, the endpoint that
accepts the customer's message, the endpoint that queues and lists runs -
must produce the same UUID or they are silently talking about different
conversations.

That is not a hypothetical. The derivation used to be written out in two
modules and skipped entirely in a third, so a run queued by the trigger
endpoint was filed under the raw external id while the run that actually
answered the question was filed under the derived id. The consequence was
invisible from any single call site: an admin listing "runs for this
conversation" saw the queued placeholder and never the run that produced
the answer, and no error was raised anywhere.

Why a derived id at all, rather than storing the external id: the inbox row
is written from a webhook payload before any conversation row exists, and a
stable UUIDv5 lets the worker key runs, turns and the control lease on it
without a schema change or a lookup. See docs/api-contracts.md.

The formula is frozen. Changing it would orphan every turn, run and lease
already stored under the old derivation, so it is a data migration, not a
refactor.
"""

import uuid

# Prefix namespace, so a conversation id can never collide with another
# uuid5 use over the same tenant namespace.
_PREFIX = "chatwoot:conversation"


def conversation_ref_for(tenant_id: uuid.UUID, external_id: str) -> uuid.UUID:
    """The platform UUID for a Chatwoot conversation, stable across callers.

    `external_id` is Chatwoot's own conversation id as it arrives in the
    webhook payload. It is caller-supplied but never trusted for
    authorization: it only feeds a deterministic hash, and every read of the
    resulting row is still governed by RLS on `tenant_id`.

    Raises `ValueError` on an empty or whitespace-only id, because such an
    id would derive a perfectly valid UUID for "no conversation at all" - a
    value that then looks like a real conversation in every downstream
    query. Callers that receive an untrusted payload must check for
    presence first; this exists so the failure is loud if they do not.

    The id is hashed exactly as given. Trimming it here would quietly
    change the result for any value that ever carried padding, and the
    stored rows depend on the current derivation.
    """
    if not external_id or not external_id.strip():
        raise ValueError("external_id must be a non-empty conversation id")
    return uuid.uuid5(tenant_id, f"{_PREFIX}:{external_id}")
