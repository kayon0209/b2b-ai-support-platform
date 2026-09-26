"""Integration tests: inbound email and WeChat channel webhooks (ADR 0013).

The load-bearing assertions here are not about the happy path:

- `test_a_channel_sender_is_stored_as_anonymous` - the ownership gate is
  three-state, and `None` means "operator run, no gate". A channel sender has
  proved ownership of nothing, so if the payload omitted `verified_account`
  every email or WeChat message would become an un-gated run able to read any
  order the tenant can. That is a hole, not a default, and this is the test that
  keeps it shut.
- `test_the_wechat_challenge_is_not_answered_without_a_valid_signature` - the
  GET handshake must not run before verification, or anyone could discover which
  server URLs belong to this deployment.
- `test_a_retry_does_not_become_a_second_question` - WeChat retries three times.
  Without a stable delivery id one question becomes three.

A channel is a `connectors` row, so the tenant resolution goes through
`resolve_connector_for_webhook` (migration 0026) exactly as the generic
connector webhook does.
"""

import hashlib
import json
import os
import time
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from platform_core.support_bridge.webhook_security import sign_payload

pytestmark = pytest.mark.integration

ADMIN_URL = os.environ.get(
    "APP_ADMIN_DATABASE_URL",
    "postgresql+psycopg://platform:platform@localhost:5435/platform",
)

# Derived, not hand-picked. These were `...c3`/`...c4`, which
# `test_identity_admin.py` also uses - the same "shared id across files" defect
# that broke `test_membership_resolution`. `ON CONFLICT (slug)` does not suppress
# a primary-key conflict, so whichever file ran second died on `tenants_pkey`.
# Deriving makes them distinct by construction instead of by a lucky guess.
_CHANNEL_NS = uuid.uuid5(uuid.NAMESPACE_URL, "b2b-ai-support/tests/channel-webhooks")

TENANT = str(uuid.uuid5(_CHANNEL_NS, "tenant"))
TENANT_OTHER = str(uuid.uuid5(_CHANNEL_NS, "tenant-other"))

SECRET_ENV = "TEST_CHANNEL_WEBHOOK_SECRET"
SECRET_VALUE = "channel-signing-key"

# WeChat's "Token" is the shared secret the connector already stores: the
# adapter reads it from `webhook_secret_ref`, so the signing value must be the
# resolved secret and not a separate literal.
WECHAT_TOKEN = SECRET_VALUE

_CLEAN: tuple[str, ...] = (
    "DELETE FROM inbox_events WHERE tenant_id IN (:a, :b)",
    "DELETE FROM conversation_turns WHERE tenant_id IN (:a, :b)",
    # `contact_facts` too, and this is not optional. `pg_constraint` lists
    # exactly two tables with an FK to `tenants` - this one and
    # `conversation_turns` - and omitting it meant `DELETE FROM tenants` raised
    # `fk_contact_facts_tenant` and rolled the whole cleanup back.
    #
    # It stayed hidden because the worker only writes contact facts when it is
    # *running*: every earlier run had the workers stopped for the suite, so the
    # row never existed. The first run with them up surfaced it as 18 setup
    # errors. A cleanup that depends on the worker being down is not a cleanup.
    "DELETE FROM contact_facts WHERE tenant_id IN (:a, :b)",
    "DELETE FROM connectors WHERE tenant_id IN (:a, :b)",
)


def _clean(conn) -> None:
    for stmt in _CLEAN:
        conn.execute(text(stmt), {"a": TENANT, "b": TENANT_OTHER})


@pytest.fixture(scope="module", autouse=True)
def seed_tenants():
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        for tid, slug in ((TENANT, "ch-t1"), (TENANT_OTHER, "ch-t2")):
            conn.execute(
                text(
                    "INSERT INTO tenants (id, slug, name, status) VALUES "
                    "(:id, :slug, :name, 'active') ON CONFLICT (slug) DO NOTHING"
                ),
                {"id": tid, "slug": slug, "name": slug},
            )
    yield
    with admin.begin() as conn:
        # Slug-driven rather than id-driven, so this also clears residue from an
        # earlier run - including rows whose ids have since changed. That is not
        # hypothetical: the old `...c3` tenant survived an aborted teardown and
        # then collided with `test_identity_admin.py`, and an id-driven cleanup
        # could never have reached it.
        #
        # Children first. `pg_constraint` lists exactly two tables with an FK to
        # `tenants` - `contact_facts` and `conversation_turns` - and deleting the
        # tenant before them rolls the whole transaction back and cleans nothing.
        for table in ("inbox_events", "conversation_turns", "contact_facts", "connectors"):
            conn.execute(
                text(
                    f"DELETE FROM {table} WHERE tenant_id IN "  # noqa: S608 - fixed names
                    "(SELECT id FROM tenants WHERE slug LIKE 'ch-t%')"
                )
            )
        conn.execute(text("DELETE FROM tenants WHERE slug LIKE 'ch-t%'"))
    admin.dispose()


@pytest.fixture(autouse=True)
def clean_rows(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(SECRET_ENV, SECRET_VALUE)
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        _clean(conn)
    yield
    with admin.begin() as conn:
        _clean(conn)
    admin.dispose()


def _client() -> TestClient:
    """The real app, so the exempt-path wiring is under test.

    These routes carry no bearer token, so if the path were missing from the
    middleware's exempt list they would be unreachable in production while a
    router-only test still passed.
    """
    import importlib

    main_mod = importlib.import_module("platform_core.main")
    return TestClient(main_mod.app, raise_server_exceptions=False)


def _insert_channel(
    *,
    provider: str = "email",
    tenant: str = TENANT,
    status: str = "active",
    webhook_secret_ref: str | None = f"env://{SECRET_ENV}",
) -> str:
    cid = str(uuid.uuid4())
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO connectors (id, tenant_id, provider, name, status, "
                "capabilities, configuration, credential_ref, webhook_secret_ref) VALUES "
                "(:id, :tid, :provider, :name, :status, CAST('[]' AS jsonb), "
                "CAST('{}' AS jsonb), NULL, :ref)"
            ),
            {
                "id": cid,
                "tid": tenant,
                "provider": provider,
                "name": f"{provider}-primary",
                "status": status,
                "ref": webhook_secret_ref,
            },
        )
    admin.dispose()
    return cid


def _email_body(
    *, message_id: str = "<m1@acme.test>", references: list[str] | None = None
) -> bytes:
    payload = {
        "message_id": message_id,
        "from": "buyer@example.test",
        "to": "support@acme.test",
        "subject": "Order question",
        "text": "What is the status of order SO-9001?",
        "attachments": [{"content_type": "image/png"}, {"content_type": "image/png"}],
    }
    if references is not None:
        payload["references"] = references
    return json.dumps(payload).encode()


def _deliver_email(
    connector_id: str, body: bytes, *, secret: str = SECRET_VALUE, signed: bool = True
):
    stamp = str(int(time.time()))
    headers = {"Content-Type": "application/json"}
    if signed:
        headers["X-Webhook-Signature"] = f"sha256={sign_payload(secret.encode(), stamp, body)}"
        headers["X-Webhook-Timestamp"] = stamp
    return _client().post(f"/v1/webhooks/channels/{connector_id}", content=body, headers=headers)


def _wechat_signature(timestamp: str, nonce: str, token: str = WECHAT_TOKEN) -> str:
    """Independent implementation of WeChat's scheme, on purpose.

    Reusing the adapter's own helper here would make the test agree with the
    code by construction, and a wrong sort order would pass.
    """
    return hashlib.sha1("".join(sorted((token, timestamp, nonce))).encode()).hexdigest()


def _wechat_body(*, content: str = "我的订单到哪了", msg_id: str = "1000000000000001") -> bytes:
    return (
        "<xml>"
        "<ToUserName><![CDATA[gh_test_account]]></ToUserName>"
        "<FromUserName><![CDATA[openid_abc]]></FromUserName>"
        "<CreateTime>1790000000</CreateTime>"
        "<MsgType><![CDATA[text]]></MsgType>"
        f"<Content><![CDATA[{content}]]></Content>"
        f"<MsgId>{msg_id}</MsgId>"
        "</xml>"
    ).encode()


def _deliver_wechat(
    connector_id: str,
    body: bytes,
    *,
    token: str = WECHAT_TOKEN,
    signed: bool = True,
    timestamp: str = "1790000000",
    nonce: str = "abc123",
):
    params = {"timestamp": timestamp, "nonce": nonce}
    if signed:
        params["signature"] = _wechat_signature(timestamp, nonce, token)
    return _client().post(f"/v1/webhooks/channels/{connector_id}", content=body, params=params)


def _inbox_rows(tenant: str = TENANT) -> list[dict]:
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        rows = (
            conn.execute(
                text(
                    "SELECT delivery_id, event_type, status, minimized_payload "
                    "FROM inbox_events WHERE tenant_id = :t ORDER BY received_at"
                ),
                {"t": tenant},
            )
            .mappings()
            .all()
        )
    admin.dispose()
    return [dict(r) for r in rows]


def _turn_text(turn_id: str) -> str | None:
    """Resolve the stored body the way the worker does."""
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        value = conn.execute(
            text("SELECT resolve_turn_text(CAST(:m AS uuid))"), {"m": turn_id}
        ).scalar_one_or_none()
    admin.dispose()
    return value


# --- email -----------------------------------------------------------------


def test_a_signed_email_is_persisted_and_resolvable() -> None:
    """The whole point: a channel message becomes an answerable question."""
    cid = _insert_channel()
    resp = _deliver_email(cid, _email_body())
    assert resp.status_code == 202, resp.text

    rows = _inbox_rows()
    assert len(rows) == 1
    row = rows[0]
    assert row["event_type"] == "message_created"
    payload = row["minimized_payload"]
    # The two fields the consumer keys off. `incoming` is what makes it a
    # customer message rather than the agent's own reply.
    assert payload["message_type"] == "incoming"
    # No Chatwoot account id: that absence is what tells the orchestrator this
    # platform is the channel, rather than a failed Chatwoot send.
    assert "chatwoot_account_id" not in payload

    # The body is NOT on the inbox row (minimisation policy); it resolves
    # through the turn, exactly as a question typed into /support does.
    assert "content" not in payload
    text_value = _turn_text(payload["message_id"])
    assert text_value is not None
    assert "SO-9001" in text_value


def test_a_channel_sender_is_stored_as_anonymous() -> None:
    """`""`, never absent.

    Absent means `None` downstream, and `None` is the sentinel for "operator
    run, no gate" - so omitting this field would let any email sender read any
    order the tenant can. Asserted explicitly because the failure is silent:
    every other assertion in this file would still pass.
    """
    cid = _insert_channel()
    assert _deliver_email(cid, _email_body()).status_code == 202
    payload = _inbox_rows()[0]["minimized_payload"]
    assert "verified_account" in payload, "omitting it means 'no gate', not 'anonymous'"
    assert payload["verified_account"] == ""


def test_an_unsigned_email_is_refused() -> None:
    cid = _insert_channel()
    assert _deliver_email(cid, _email_body(), signed=False).status_code == 401
    assert _inbox_rows() == []


def test_a_wrongly_signed_email_is_refused() -> None:
    cid = _insert_channel()
    assert _deliver_email(cid, _email_body(), secret="not-the-key").status_code == 401
    assert _inbox_rows() == []


def test_a_reply_lands_on_the_same_conversation_as_the_thread_root() -> None:
    """Threading, and the trap: `references[0]` is the root by RFC 5322.

    A provider that sets `thread_id` to the *parent* would split every thread,
    which is why the standard is preferred. Asserted as a pair: the root and the
    reply must produce one conversation key, and a different thread a different
    one.
    """
    cid = _insert_channel()
    root = "<root@acme.test>"
    assert _deliver_email(cid, _email_body(message_id=root)).status_code == 202
    assert (
        _deliver_email(
            cid,
            _email_body(message_id="<reply1@acme.test>", references=[root, "<other@acme.test>"]),
        ).status_code
        == 202
    )
    assert _deliver_email(cid, _email_body(message_id="<fresh@acme.test>")).status_code == 202

    keys = [r["minimized_payload"]["conversation_id"] for r in _inbox_rows()]
    assert len(keys) == 3
    assert keys[0] == root and keys[1] == root, "the reply must join the root's conversation"
    assert keys[2] != root, "a fresh thread must not join it"


@pytest.mark.zero_tolerance("duplicate_replies")
def test_a_retry_does_not_become_a_second_question() -> None:
    """WeChat retries; a provider retry must collapse onto the original row.

    **This carries a zero-tolerance guarantee, deliberately.** Every other test
    tagged `duplicate_replies` lives on the Chatwoot path
    (`test_e2e_acceptance.py`, `test_orchestrator_lease_race.py`), so removing
    Chatwoot - ADR 0012 Stage 2 - would delete the whole category from the
    release gate. Moving the guarantee onto the surviving channel *before* the
    old path goes is what makes that removal safe rather than a silent loss of
    coverage.

    The guarantee is the same one: a duplicate delivery must not produce a
    second answer. A retry that produced a second inbox row would queue a second
    run, and the customer would be answered twice.
    """
    cid = _insert_channel()
    body = _email_body(message_id="<retry@acme.test>")
    assert _deliver_email(cid, body).status_code == 202
    second = _deliver_email(cid, body)
    assert second.status_code == 200
    assert second.json()["status"] == "duplicate"
    assert len(_inbox_rows()) == 1


def test_an_email_with_no_text_is_acknowledged_but_not_answered() -> None:
    """An HTML-only email must not queue a run with nothing in it."""
    cid = _insert_channel()
    payload = {"message_id": "<html-only@acme.test>", "from": "a@b.test", "to": "c@d.test"}
    resp = _deliver_email(cid, json.dumps(payload).encode())
    assert resp.status_code == 200
    assert _inbox_rows() == []


# --- connector gating ------------------------------------------------------


def test_a_connector_that_is_not_a_channel_is_refused() -> None:
    cid = _insert_channel(provider="jira")
    assert _deliver_email(cid, _email_body()).status_code == 404


def test_an_inactive_channel_is_refused() -> None:
    cid = _insert_channel(status="disabled")
    assert _deliver_email(cid, _email_body()).status_code == 409


def test_a_channel_with_no_secret_is_refused() -> None:
    """Fail closed: an unresolvable secret is not an empty one."""
    cid = _insert_channel(webhook_secret_ref=None)
    assert _deliver_email(cid, _email_body()).status_code == 503


def test_an_unknown_connector_is_refused_without_detail() -> None:
    assert _deliver_email(str(uuid.uuid4()), _email_body()).status_code == 404


# --- wechat ----------------------------------------------------------------


def test_a_signed_wechat_text_is_persisted_and_answered() -> None:
    cid = _insert_channel(provider="wechat", webhook_secret_ref=f"env://{SECRET_ENV}")
    resp = _deliver_wechat(cid, _wechat_body())
    # 200, not 202: WeChat's contract is a valid XML reply inside five seconds,
    # not a status code - the acknowledgement *is* the response.
    assert resp.status_code == 200, resp.text
    assert "application/xml" in resp.headers["content-type"]
    assert "openid_abc" in resp.text and "gh_test_account" in resp.text

    payload = _inbox_rows()[0]["minimized_payload"]
    assert payload["message_type"] == "incoming"
    assert payload["verified_account"] == ""
    # One conversation per customer: the protocol has no thread concept, so the
    # openid is the key.
    assert payload["conversation_id"] == "openid_abc"
    # The body is not on the inbox row (minimisation policy); it resolves
    # through the turn, exactly as a question typed into /support does.
    assert "content" not in payload
    assert "我的订单到哪了" in (_turn_text(payload["message_id"]) or "")


def test_a_wechat_signature_is_verified_with_its_own_scheme() -> None:
    """sha1 over the sorted triple, not an HMAC over the body."""
    cid = _insert_channel(provider="wechat", webhook_secret_ref=f"env://{SECRET_ENV}")
    assert _deliver_wechat(cid, _wechat_body(), token="wrong-token").status_code == 401
    assert _inbox_rows() == []


def test_the_wechat_challenge_is_not_answered_without_a_valid_signature() -> None:
    """The handshake must not run before verification.

    Answering it first would let an unauthenticated caller confirm which server
    URLs belong to this deployment.
    """
    cid = _insert_channel(provider="wechat", webhook_secret_ref=f"env://{SECRET_ENV}")
    good = _wechat_signature("1790000000", "abc123")

    bad = _client().get(
        f"/v1/webhooks/channels/{cid}",
        params={
            "timestamp": "1790000000",
            "nonce": "abc123",
            "signature": "0" * 40,
            "echostr": "echo-me",
        },
    )
    assert bad.status_code == 401
    assert "echo-me" not in bad.text

    ok = _client().get(
        f"/v1/webhooks/channels/{cid}",
        params={
            "timestamp": "1790000000",
            "nonce": "abc123",
            "signature": good,
            "echostr": "echo-me",
        },
    )
    assert ok.status_code == 200
    assert ok.text == "echo-me"


def test_a_wechat_event_is_acknowledged_but_not_answered() -> None:
    """Subscribes and images arrive on the same endpoint and are not questions."""
    cid = _insert_channel(provider="wechat", webhook_secret_ref=f"env://{SECRET_ENV}")
    event = (
        b"<xml>"
        b"<ToUserName><![CDATA[gh_test_account]]></ToUserName>"
        b"<FromUserName><![CDATA[openid_abc]]></FromUserName>"
        b"<CreateTime>1790000000</CreateTime>"
        b"<MsgType><![CDATA[event]]></MsgType>"
        b"<Event><![CDATA[subscribe]]></Event>"
        b"</xml>"
    )
    assert _deliver_wechat(cid, event).status_code == 200
    assert _inbox_rows() == []


def test_wechat_refuses_a_doctype_in_the_body() -> None:
    """Defence in depth: the payload is signed, but entity expansion is never
    legitimate in a chat message."""
    cid = _insert_channel(provider="wechat", webhook_secret_ref=f"env://{SECRET_ENV}")
    hostile = (
        b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "boom">]>'
        b"<xml><FromUserName><![CDATA[openid_abc]]></FromUserName>"
        b"<ToUserName><![CDATA[gh_test_account]]></ToUserName>"
        b"<MsgType><![CDATA[text]]></MsgType><Content><![CDATA[&a;]]></Content>"
        b"<MsgId>1</MsgId></xml>"
    )
    assert _deliver_wechat(cid, hostile).status_code == 200
    assert _inbox_rows() == []
