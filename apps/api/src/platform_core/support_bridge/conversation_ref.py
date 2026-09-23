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

Two halves, and a request must use the right one
------------------------------------------------
A platform ref is derived **once**, by whoever holds the external id, and is
then the conversation's identity everywhere else:

- **Mint** (`conversation_ref_for`) — for a caller that holds a *channel* id:
  the visitor session endpoint, the channel adapter, the webhook consumer.
- **Read** (`parse_conversation_ref`) — for a caller that holds a *platform*
  ref, which is every endpoint whose path segment is named `conversation_ref`.
  It parses and does not derive.

Deriving a value that is already a ref is the defect this docstring exists to
prevent, and it fails silently: `/v1/conversations` listed refs while
`/{ref}/replay` derived them again, so the console's replay screen looked up a
second conversation. Four of four sampled conversations returned either
`NOT_FOUND` or — worse — a different conversation's transcript. The same shape
made `POST /{ref}/replies` file an agent's reply where the customer could not
address it. See `docs/api-contracts.md`.
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


def parse_conversation_ref(value: str) -> uuid.UUID:
    """The platform ref taken from a request path — used verbatim, never derived.

    The counterpart of `conversation_ref_for`, and the only reader of a
    `conversation_ref` path segment. Whoever mints a ref hands it out (the
    visitor session endpoint returns it, the console listing returns it), and
    every endpoint that receives it back uses it as-is. Deriving here would
    hash an id that is already a hash: the endpoint would then answer about a
    conversation that no writer ever wrote to, which reads as `NOT_FOUND` when
    it is lucky and as somebody else's conversation when it is not.

    Each path segment has exactly one meaning under this rule. The alternative
    — accepting an external id here and deriving — cannot work, because the
    two are indistinguishable: both are UUIDs, and a caller has no way to say
    which it is passing.

    Raises `ValueError` with the field name, matching `api.parse_uuid`, so
    callers return the same 400 envelope they already do for a malformed path.
    """
    try:
        return uuid.UUID(value)
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError("conversation_ref is not a valid uuid") from exc


__all__ = ["conversation_ref_for", "parse_conversation_ref"]
