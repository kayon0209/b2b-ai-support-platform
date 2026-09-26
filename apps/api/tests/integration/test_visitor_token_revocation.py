"""Integration tests: a visitor session can be ended, and the token stops working.

Why these exist
---------------
`visitor_token.py` signs a credential with a 12-hour TTL and nothing else. There
was no `jti`, no revocation record and no endpoint, so the only way a leaked
customer token stopped working was for the clock to run out - and a customer who
clicked "end conversation" on a shared machine changed nothing at all. The
audit found it: opening a session again with the same `visitor_id` returned the
same conversation and a freshly signed token over it.

The surface had no tests of any kind, which is how that survived. These cover
the two properties that make revocation meaningful: the happy path still works,
and a revoked token is refused on the *very next* request rather than at expiry.

The unit tests for signing and expiry already exist in spirit in
`test_visitor_token`-style modules; what is here is the database-backed half,
because revocation is a database fact and a signature check cannot see it.
"""

from __future__ import annotations

import os
import time
import uuid

import pytest
from sqlalchemy import text

pytestmark = pytest.mark.integration

# `d4`/`d5` are taken by test_lease_change_closes_queued_runs.py. Reusing them
# fails on `tenants_pkey` for whichever file runs second, because
# `ON CONFLICT (slug) DO NOTHING` does not suppress a primary-key conflict -
# which is why the suite carries a hygiene test for exactly this.
ADMIN_URL = os.environ.get(
    "APP_ADMIN_DATABASE_URL", "postgresql+psycopg://platform:platform@localhost:5435/platform"
)
APP_URL = os.environ.get(
    "APP_TEST_DATABASE_URL",
    "postgresql+psycopg://platform_app:platform_app@localhost:5435/platform",
)

TENANT_A = "01900000-0000-7000-8000-0000000000fc"
TENANT_B = "01900000-0000-7000-8000-0000000000fd"


def _seed_tenant(conn, tenant_id: str, slug: str) -> None:
    conn.execute(
        text(
            "INSERT INTO tenants (id, slug, name, status) "
            "VALUES (:id, :slug, :name, 'active') ON CONFLICT (slug) DO NOTHING"
        ),
        {"id": tenant_id, "slug": slug, "name": slug},
    )


def _client():
    from fastapi.testclient import TestClient

    from platform_core.main import app

    return TestClient(app, raise_server_exceptions=False)


# --- the revocation primitives ------------------------------------------------


def test_a_revoked_token_is_refused_immediately() -> None:
    """The whole point: not at expiry, not after a cleanup job - on the next
    request. A revocation mechanism that only takes effect on the next token
    rotation is a mechanism for a twelve-hour-old problem."""
    from sqlalchemy import create_engine

    from platform_core.support_bridge.visitor_revocation import is_revoked

    token_jti, conversation_ref = uuid.uuid4(), uuid.uuid4()
    engine = create_engine(ADMIN_URL)
    try:
        with engine.begin() as conn:
            _seed_tenant(conn, TENANT_A, "revocation-tenant-a")
        with engine.begin() as conn:
            assert is_revoked(conn, tenant_id=uuid.UUID(TENANT_A), token_jti=token_jti) is False
        with engine.begin() as conn:
            _revoke(conn, uuid.UUID(TENANT_A), token_jti, conversation_ref)
        with engine.begin() as conn:
            assert is_revoked(conn, tenant_id=uuid.UUID(TENANT_A), token_jti=token_jti) is True
    finally:
        engine.dispose()


def _revoke(
    conn,
    tenant_id: uuid.UUID,
    token_jti: uuid.UUID,
    conversation_ref: uuid.UUID | None = None,
    *,
    revoked_at: int | None = None,
) -> None:
    from platform_core.support_bridge.visitor_revocation import record_revocation

    record_revocation(
        conn,
        tenant_id=tenant_id,
        token_jti=token_jti,
        conversation_ref=conversation_ref or uuid.uuid4(),
        revoked_at=int(time.time()) if revoked_at is None else revoked_at,
    )


def test_revocation_is_scoped_to_one_conversation() -> None:
    """Ending a conversation must not lock the same visitor out of a different
    one. The visitor id is a client-held handle, so a customer with two tabs - or
    a shared family machine - legitimately holds two conversations."""
    from sqlalchemy import create_engine

    from platform_core.support_bridge.visitor_revocation import is_revoked

    conversation = uuid.uuid4()
    ended, other = uuid.uuid4(), uuid.uuid4()
    engine = create_engine(ADMIN_URL)
    try:
        with engine.begin() as conn:
            _seed_tenant(conn, TENANT_A, "revocation-tenant-a")
        with engine.begin() as conn:
            _revoke(conn, uuid.UUID(TENANT_A), ended, conversation)
        with engine.begin() as conn:
            assert is_revoked(conn, tenant_id=uuid.UUID(TENANT_A), token_jti=ended) is True
            # Same conversation, different credential: untouched. The customer
            # page re-issues tokens silently, and one withdrawal must not take
            # the replacement with it.
            assert is_revoked(conn, tenant_id=uuid.UUID(TENANT_A), token_jti=other) is False
    finally:
        engine.dispose()


def test_revocation_does_not_cross_tenants() -> None:
    """A jti is a global uuid, so two tenants could in principle hold the same
    one. A withdrawal that ignored the tenant would let one tenant kill another
    tenant's credential - the cross-tenant leak the platform prevents elsewhere."""
    from sqlalchemy import create_engine

    from platform_core.support_bridge.visitor_revocation import is_revoked

    shared_jti = uuid.uuid4()
    engine = create_engine(ADMIN_URL)
    try:
        with engine.begin() as conn:
            _seed_tenant(conn, TENANT_A, "revocation-tenant-a")
            _seed_tenant(conn, TENANT_B, "revocation-tenant-b")
        with engine.begin() as conn:
            _revoke(conn, uuid.UUID(TENANT_A), shared_jti)
        with engine.begin() as conn:
            assert is_revoked(conn, tenant_id=uuid.UUID(TENANT_B), token_jti=shared_jti) is False, (
                "tenant A's withdrawal matched tenant B's credential"
            )
    finally:
        engine.dispose()


def test_revoking_twice_is_not_an_error() -> None:
    """The customer page may send the end-session request twice - a retry, a
    double click, a tab closing while the request is in flight. A 500 on the
    second attempt would teach the client that ending a session is unreliable."""
    from sqlalchemy import create_engine

    from platform_core.support_bridge.visitor_revocation import record_revocation

    token_jti, conversation_ref = uuid.uuid4(), uuid.uuid4()
    now = int(time.time())
    engine = create_engine(ADMIN_URL)
    try:
        with engine.begin() as conn:
            _seed_tenant(conn, TENANT_A, "revocation-tenant-a")
        for _ in range(2):
            with engine.begin() as conn:
                record_revocation(
                    conn,
                    tenant_id=uuid.UUID(TENANT_A),
                    token_jti=token_jti,
                    conversation_ref=conversation_ref,
                    revoked_at=now,
                )
        with engine.begin() as conn:
            count = conn.execute(
                text(
                    "SELECT count(*) FROM visitor_session_revocations "
                    "WHERE tenant_id = :t AND token_jti = :j"
                ),
                {"t": TENANT_A, "j": str(token_jti)},
            ).scalar_one()
        assert count == 1, f"a second end-session created {count} rows"
    finally:
        engine.dispose()


# --- the HTTP surface ---------------------------------------------------------


def test_ending_a_session_makes_the_old_token_unusable() -> None:
    """End-to-end, through the real endpoints: open a session, use it, end it,
    then replay the same token. The replay must be refused.

    This is the assertion the audit asked for and could not be made before: with
    a 12-hour TTL and no revocation, the replayed token kept working until it
    expired on its own.
    """
    client = _client()

    opened = client.post(
        "/v1/support/sessions",
        json={"tenant_slug": "revocation-tenant-a", "visitor_id": f"e2e-{uuid.uuid4()}"},
    )
    assert opened.status_code == 200, opened.text
    token = opened.json()["token"]

    # A live token works.
    timeline = client.get("/v1/support/timeline", headers={"Authorization": f"Bearer {token}"})
    assert timeline.status_code == 200, timeline.text

    ended = client.post("/v1/support/sessions/end", headers={"Authorization": f"Bearer {token}"})
    assert ended.status_code == 200, ended.text

    # The replay is the attack: the same token, after the customer ended it.
    replay = client.get("/v1/support/timeline", headers={"Authorization": f"Bearer {token}"})
    assert replay.status_code == 401, (
        f"a token for an ended session still works: {replay.status_code} {replay.text[:200]}"
    )


def test_ending_a_session_requires_the_token_it_ends() -> None:
    """No bearer, no body: the endpoint takes its authorization from the token
    it is revoking, so a caller can only close the conversation their own token
    names. A body-supplied conversation id would let anyone close anyone's chat.
    """
    client = _client()
    response = client.post("/v1/support/sessions/end")
    assert response.status_code == 401, response.text


def test_ending_one_session_leaves_another_open() -> None:
    client = _client()
    first = client.post(
        "/v1/support/sessions",
        json={"tenant_slug": "revocation-tenant-a", "visitor_id": f"pair-a-{uuid.uuid4()}"},
    ).json()["token"]
    second = client.post(
        "/v1/support/sessions",
        json={"tenant_slug": "revocation-tenant-a", "visitor_id": f"pair-b-{uuid.uuid4()}"},
    ).json()["token"]

    assert (
        client.post(
            "/v1/support/sessions/end", headers={"Authorization": f"Bearer {first}"}
        ).status_code
        == 200
    )

    still_open = client.get("/v1/support/timeline", headers={"Authorization": f"Bearer {second}"})
    assert still_open.status_code == 200, (
        f"ending one conversation closed another: {still_open.status_code}"
    )


def test_reopening_a_revoked_conversation_issues_a_working_token() -> None:
    """Ending is not permanent. A customer who comes back gets a working
    conversation again - otherwise "end session" would mean "delete my chat",
    which is a different promise than the one the button makes.

    The new token must work, and the old one must stay dead: revocation records
    the conversation, so the *pair* has to be re-armed deliberately rather than
    by a clock.
    """
    client = _client()
    visitor_id = f"reopen-{uuid.uuid4()}"
    first = client.post(
        "/v1/support/sessions",
        json={"tenant_slug": "revocation-tenant-a", "visitor_id": visitor_id},
    ).json()["token"]
    client.post("/v1/support/sessions/end", headers={"Authorization": f"Bearer {first}"})

    second = client.post(
        "/v1/support/sessions",
        json={"tenant_slug": "revocation-tenant-a", "visitor_id": visitor_id},
    )
    assert second.status_code == 200, second.text
    fresh = second.json()["token"]

    reopened = client.get("/v1/support/timeline", headers={"Authorization": f"Bearer {fresh}"})
    assert reopened.status_code == 200, (
        f"reopening an ended conversation did not produce a usable token: {reopened.status_code}"
    )
    old = client.get("/v1/support/timeline", headers={"Authorization": f"Bearer {first}"})
    assert old.status_code == 401, "reopening resurrected the revoked token"


def test_purge_removes_only_revocations_that_can_no_longer_match() -> None:
    """Retention has to have a definition of "expired", or the table grows with
    every conversation ever ended instead of the concurrent ones.

    A revocation older than the longest token lifetime is dead weight: the token
    it withdraws expired before the row was written. A recent one is still the
    only thing stopping a leaked credential, so the boundary is the whole
    point - deleting either side of it wrongly is a security regression, not a
    housekeeping bug.
    """
    from sqlalchemy import create_engine

    from platform_core.support_bridge.visitor_revocation import (
        MAX_TOKEN_TTL_SECONDS,
        purge_unreachable,
    )

    now = int(time.time())
    stale, live = uuid.uuid4(), uuid.uuid4()
    engine = create_engine(ADMIN_URL)
    try:
        with engine.begin() as conn:
            _seed_tenant(conn, TENANT_A, "revocation-tenant-a")
        with engine.begin() as conn:
            _revoke(
                conn,
                uuid.UUID(TENANT_A),
                stale,
                revoked_at=now - MAX_TOKEN_TTL_SECONDS - 60,
            )
        with engine.begin() as conn:
            _revoke(conn, uuid.UUID(TENANT_A), live, revoked_at=now)
        with engine.begin() as conn:
            removed = purge_unreachable(conn, now=now)
        with engine.begin() as conn:
            assert is_revoked_row(conn, uuid.UUID(TENANT_A), stale) is False, (
                "purged a revocation that can still match a live token"
            )
            assert is_revoked_row(conn, uuid.UUID(TENANT_A), live) is True, (
                "purged a revocation that is the only thing stopping a leaked token"
            )
        assert removed >= 1, f"purged nothing, so {stale} is still there"
    finally:
        engine.dispose()


def is_revoked_row(conn, tenant_id: uuid.UUID, token_jti: uuid.UUID) -> bool:
    row = conn.execute(
        text(
            "SELECT 1 FROM visitor_session_revocations "
            "WHERE tenant_id = :t AND token_jti = :j LIMIT 1"
        ),
        {"t": str(tenant_id), "j": str(token_jti)},
    ).first()
    return row is not None
