"""Withdrawing a visitor session's token.

`visitor_token.verify` is a pure function: it checks a signature and an expiry
and needs no database. Revocation cannot live there without turning the most
hot function on the customer surface into a query, and the customer surface is
the one path measured under load. So it lives here instead, as a separate lookup
the router makes once per request against an already-open session.

Why the key is a jti and not the conversation
---------------------------------------------
The first version of this revoked by (tenant, conversation_ref), reasoning that
"end session" means the conversation. Testing that design showed it is wrong in
the one case customers actually hit: they end a session and come back later.

`POST /v1/support/sessions` mints a token for the same conversation, and
because the revocation was keyed on the conversation, the newly issued token was
rejected too - the customer could never return to a conversation they had not
deleted. The two promises are genuinely different. "End" means *stop holding this
credential*; only conversation deletion, which this platform does not offer,
should make returning impossible.

So each token carries a `jti` and revocation withdraws that one credential.
Ending a session kills the token the customer is holding now; reopening mints a
new one and works. The old token stays dead, because its own jti is withdrawn -
which is the property the audit asked for, and the earlier design got only by
accident of the customer never returning.

The jti is a random uuid inside the signed payload, so it adds no new secret
and is not guessable. Tokens minted before this change have no `jti` and are
treated as unrevoked: nothing can withdraw them, which is the same state they
were in before, and it means the deploy that introduces revocation does not log
every customer out at once.

Why the tenant is still part of the key
---------------------------------------
The check is a defence in depth, not the isolation boundary - RLS is that. But
`jti` is a global uuid, so without the tenant in the predicate a bug in one
caller's binding could query another tenant's revocations. Cheap to prevent.
"""

from __future__ import annotations

import time
import uuid
from typing import Any

from sqlalchemy import text

# Longest lifetime `visitor_token.issue` can mint, in seconds. A revocation
# older than this cannot match a token that still verifies, because every token
# it could match has already expired. Used by retention, and asserted against
# the token module's own constant so the two cannot drift apart.
MAX_TOKEN_TTL_SECONDS = 12 * 60 * 60

# Why a session was ended. `visitor` is the only value the customer surface can
# produce today; `operator` exists so a support agent closing a chat from the
# workbench records a different reason without a second migration.
ENDED_BY_VISITOR = "visitor"
ENDED_BY_OPERATOR = "operator"


def record_revocation(
    conn: Any,
    *,
    tenant_id: uuid.UUID,
    token_jti: uuid.UUID,
    conversation_ref: uuid.UUID,
    revoked_at: int | None = None,
    ended_by: str = ENDED_BY_VISITOR,
) -> bool:
    """Withdraw the conversation. Returns True if this call was the one that did.

    Idempotent by way of a unique index on (tenant, conversation_ref): a second
    call is a no-op that reports False, rather than an error. The customer page
    retries, double-clicks, and can fire the request again from a tab closing
    mid-flight, and a 500 on the second attempt would teach the client that
    ending a session is unreliable.

    `conn` is the caller's own connection and the caller's own transaction. The
    router is already inside a session scope, and opening a second one here
    would make the write invisible to the transaction that is about to commit.
    """
    result = conn.execute(
        text(
            "INSERT INTO visitor_session_revocations "
            "(id, tenant_id, token_jti, conversation_ref, revoked_at, ended_by) "
            "VALUES (:id, :tenant, :jti, :conversation, :revoked_at, :ended_by) "
            "ON CONFLICT (tenant_id, token_jti) DO NOTHING"
        ),
        {
            "id": str(uuid.uuid4()),
            "tenant": str(tenant_id),
            "jti": str(token_jti),
            "conversation": str(conversation_ref),
            "revoked_at": int(time.time() if revoked_at is None else revoked_at),
            "ended_by": ended_by,
        },
    )
    # `rowcount` is 0 when the conflict branch fired, so it distinguishes
    # "this request withdrew the session" from "it was already withdrawn".
    return bool(result.rowcount)


def is_revoked(conn: Any, *, tenant_id: uuid.UUID, token_jti: uuid.UUID | None) -> bool:
    """Whether this token is withdrawn. A token with no jti cannot be withdrawn."""
    found = conn.execute(
        text(
            "SELECT 1 FROM visitor_session_revocations "
            "WHERE tenant_id = :tenant AND token_jti = :jti "
            "LIMIT 1"
        ),
        {"tenant": str(tenant_id), "jti": str(token_jti) if token_jti else ""},
    ).first()
    return found is not None


def purge_unreachable(conn: Any, *, now: int | None = None) -> int:
    """Delete revocations that can no longer match a live token.

    A revocation older than the maximum token lifetime is dead weight: any token
    it could match expired before the row was written. Without this the table
    grows with all conversations ever ended rather than with concurrent ones.

    Returns the number of rows removed, so a caller can log the sweep.
    """
    cutoff = int(time.time() if now is None else now) - MAX_TOKEN_TTL_SECONDS
    result = conn.execute(
        text("DELETE FROM visitor_session_revocations WHERE revoked_at < :cutoff"),
        {"cutoff": cutoff},
    )
    return int(result.rowcount or 0)


async def async_is_revoked(
    session: Any,
    *,
    tenant_id: uuid.UUID,
    token_jti: uuid.UUID | None,
) -> bool:
    """The async form, for the router's existing session scope.

    The query is the same primary-key lookup; only the driver differs. The
    customer page polls this on every timeline read, so it must not open a
    second connection - it reuses the session the request already has, which is
    also the one whose RLS binding is already in place.
    """
    # `AsyncSession.execute` is awaited; its result's `first()` is not. That is
    # the same shape as `lease_service`, which is where the convention comes
    # from, and the opposite of the mistake this line replaces.
    row = (
        await session.execute(
            text(
                "SELECT 1 FROM visitor_session_revocations "
                "WHERE tenant_id = :tenant AND token_jti = :jti "
                "LIMIT 1"
            ),
            {"tenant": str(tenant_id), "jti": str(token_jti) if token_jti else ""},
        )
    ).first()
    return row is not None


async def async_record_revocation(
    session: Any,
    *,
    tenant_id: uuid.UUID,
    token_jti: uuid.UUID,
    conversation_ref: uuid.UUID,
    revoked_at: int | None = None,
    ended_by: str = ENDED_BY_VISITOR,
) -> bool:
    """The async form of `record_revocation`, for the router's session scope.

    Same statement and same idempotence; kept beside `async_is_revoked` so the
    write and the check that must agree with it are in one file. The write runs
    in the caller's transaction, so a rollback of the request rolls the
    revocation back with it - which is correct: a session that was never ended
    must not be left revoked.
    """
    result = await session.execute(
        text(
            "INSERT INTO visitor_session_revocations "
            "(id, tenant_id, token_jti, conversation_ref, revoked_at, ended_by) "
            "VALUES (:id, :tenant, :jti, :conversation, :revoked_at, :ended_by) "
            "ON CONFLICT (tenant_id, token_jti) DO NOTHING"
        ),
        {
            "id": str(uuid.uuid4()),
            "tenant": str(tenant_id),
            "jti": str(token_jti),
            "conversation": str(conversation_ref),
            "revoked_at": int(time.time() if revoked_at is None else revoked_at),
            "ended_by": ended_by,
        },
    )
    return bool(result.rowcount)
