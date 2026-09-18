"""End-to-end: a duplicate webhook delivery must not produce a second reply.

This is Phase 1's first acceptance criterion, verbatim:

    "Duplicate webhook delivery never creates duplicate customer replies."

It has contract tests with synthetic payloads, but nothing had ever asserted it
against a live Chatwoot, where "duplicate reply" is a customer-visible event
rather than a row count.

What is real here and what is not:

- **real**: the signed webhook and its verification, tenant resolution from the
  account mapping, the InboxEvent write and its dedupe, the worker, the
  orchestrator, and `ChatwootClient` posting the reply;
- **synthetic**: the delivery itself. The payload is built here rather than by
  Chatwoot, because the test has to know the `X-Chatwoot-Delivery` id in order
  to redeliver it. `tests/e2e/e2e_chatwoot_loop.py` covers the real delivery
  path; this one covers what happens when the same delivery arrives twice.

Two things have to be arranged for the test to be able to observe anything:

- **the message must really exist in Chatwoot.** `minimize_chatwoot_payload`
  strips `content` on purpose ("the message body itself stays in Chatwoot and
  is fetched later via its API"), so the worker resolves the question with
  `fetch_message(message_id)`. A synthetic message id yields no question and
  therefore no reply - which is what the first version of this test did, and it
  looked like the platform failing to answer.
- **Chatwoot must not deliver the message itself.** Posting a message through
  Chatwoot's API raises a `message_created` webhook of its own, which would be
  a *different* delivery and legitimately produce a second reply. The account
  webhook is therefore detached for the duration and restored in `finally`, so
  the only deliveries are the two the test makes.

Run:

    ./.venv/Scripts/python.exe tests/e2e/e2e_chatwoot_duplicate_delivery.py
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import sys
import time
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
for _p in (
    "apps/api/src",
    "apps/worker/src",
    "packages/contracts/src",
    "packages/policy/src",
    "packages/observability/src",
):
    sys.path.insert(0, str(REPO_ROOT / _p))

import httpx  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

PLATFORM_URL = os.environ.get(
    "APP_ADMIN_DATABASE_URL",
    "postgresql+psycopg://platform:platform@localhost:5435/platform",
)
CHATWOOT_BASE = os.environ.get("APP_CHATWOOT_BASE_URL", "http://localhost:3000")
CHATWOOT_TOKEN = os.environ.get("APP_CHATWOOT_API_TOKEN", "")
# The same secret Chatwoot's webhook row carries; both sides must agree.
WEBHOOK_SECRET = os.environ.get("APP_CHATWOOT_WEBHOOK_SECRET", "local-dev-webhook-secret")
API_BASE = os.environ.get("APP_API_BASE_URL", "http://localhost:8000")

INBOX_ID = int(os.environ.get("CHATWOOT_E2E_INBOX_ID", "1"))
ACCOUNT_ID = os.environ.get("CHATWOOT_E2E_ACCOUNT_ID", "1")
POLL_SECONDS = 90


def _headers() -> dict[str, str]:
    return {"api_access_token": CHATWOOT_TOKEN, "Content-Type": "application/json"}


def _chatwoot(client: httpx.Client, method: str, path: str, **kwargs: object) -> httpx.Response:
    resp = client.request(method, f"{CHATWOOT_BASE}/api/v1{path}", headers=_headers(), **kwargs)  # type: ignore[arg-type]
    if resp.status_code >= 300:
        raise RuntimeError(f"{method} {path} -> {resp.status_code}: {resp.text[:300]}")
    return resp


def _sign(timestamp: str, body: bytes) -> str:
    mac = hmac.new(WEBHOOK_SECRET.encode(), digestmod=hashlib.sha256)
    mac.update(timestamp.encode())
    mac.update(b".")
    mac.update(body)
    return mac.hexdigest()


def _deliver(body: bytes, *, delivery_id: str) -> httpx.Response:
    timestamp = str(int(time.time()))
    return httpx.post(
        f"{API_BASE}/v1/webhooks/chatwoot",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Chatwoot-Signature": f"sha256={_sign(timestamp, body)}",
            "X-Chatwoot-Timestamp": timestamp,
            "X-Chatwoot-Delivery": delivery_id,
        },
        timeout=20.0,
    )


async def _seed(slug: str) -> uuid.UUID:
    engine = create_async_engine(PLATFORM_URL)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            tenant_id = (
                await session.execute(
                    text(
                        "INSERT INTO tenants (id, slug, name, status) "
                        "VALUES (gen_random_uuid(), :slug, :slug, 'active') RETURNING id"
                    ),
                    {"slug": slug},
                )
            ).scalar()
            await session.execute(
                text(
                    "INSERT INTO external_resource_refs "
                    "(id, tenant_id, system, resource_type, external_id, external_url) "
                    "VALUES (gen_random_uuid(), :t, 'chatwoot', 'account', :acct, :url)"
                ),
                {"t": tenant_id, "acct": ACCOUNT_ID, "url": CHATWOOT_BASE},
            )
            await session.commit()
        return tenant_id
    finally:
        await engine.dispose()


async def _inbox_events(slug: str) -> list[tuple[str, str]]:
    engine = create_async_engine(PLATFORM_URL)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT e.event_type, e.delivery_id FROM inbox_events e "
                        "JOIN tenants t ON t.id = e.tenant_id WHERE t.slug = :slug ORDER BY e.id"
                    ),
                    {"slug": slug},
                )
            ).all()
        return [(str(r[0]), str(r[1])) for r in rows]
    finally:
        await engine.dispose()


async def _cleanup(slug: str) -> None:
    engine = create_async_engine(PLATFORM_URL)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            tenant_id = (
                await session.execute(
                    text("SELECT id FROM tenants WHERE slug = :slug"), {"slug": slug}
                )
            ).scalar()
            if tenant_id is None:
                return
            for stmt in (
                "DELETE FROM inbox_events WHERE tenant_id = :t",
                "DELETE FROM external_resource_refs WHERE tenant_id = :t",
                "DELETE FROM agent_runs WHERE tenant_id = :t",
                "DELETE FROM audit_events WHERE tenant_id = :t",
                "DELETE FROM tenants WHERE id = :t",
            ):
                await session.execute(text(stmt), {"t": tenant_id})
            await session.commit()
    finally:
        await engine.dispose()


def _outbound(client: httpx.Client, conversation_id: int) -> list[dict]:
    """Messages from the agent side (`message_type == 1`)."""
    messages = _chatwoot(
        client, "GET", f"/accounts/{ACCOUNT_ID}/conversations/{conversation_id}/messages"
    ).json()["payload"]
    return [m for m in messages if m.get("message_type") == 1 and m.get("content")]


def main() -> int:
    if not CHATWOOT_TOKEN:
        print("FATAL: APP_CHATWOOT_API_TOKEN is not set.", file=sys.stderr)
        return 2

    slug = f"e2e-dup-{uuid.uuid4().hex[:8]}"
    contact_id: int | None = None
    webhook: dict | None = None
    detached = False

    with httpx.Client(timeout=20.0) as client:
        try:
            health = httpx.get(f"{API_BASE}/healthz", timeout=5.0)
            if health.status_code != 200:
                print(
                    f"FATAL: API not healthy at {API_BASE}: {health.status_code}",
                    file=sys.stderr,
                )
                return 2
            _chatwoot(client, "GET", "/profile")

            asyncio.run(_seed(slug), loop_factory=asyncio.SelectorEventLoop)
            print(f"tenant slug: {slug} -> chatwoot account {ACCOUNT_ID}")

            suffix = uuid.uuid4().hex[:8]
            contact = _chatwoot(
                client,
                "POST",
                f"/accounts/{ACCOUNT_ID}/contacts",
                json={"name": f"E2E Dup {suffix}", "email": f"e2e-dup-{suffix}@example.test"},
            ).json()
            contact_id = contact["payload"]["contact"]["id"]
            conversation = _chatwoot(
                client,
                "POST",
                f"/accounts/{ACCOUNT_ID}/conversations",
                json={
                    "source_id": f"e2e-dup-{suffix}",
                    "inbox_id": INBOX_ID,
                    "contact_id": contact_id,
                },
            ).json()
            conversation_id = conversation["id"]
            print(f"contact={contact_id} conversation={conversation_id}")

            # --- detach Chatwoot's own delivery for the duration --------
            existing = _chatwoot(client, "GET", f"/accounts/{ACCOUNT_ID}/webhooks").json()
            webhook = existing["payload"]["webhooks"][0]
            _chatwoot(client, "DELETE", f"/accounts/{ACCOUNT_ID}/webhooks/{webhook['id']}")
            detached = True
            print(f"detached chatwoot webhook {webhook['id']} for the duration")

            # --- a real message, so the worker can fetch its body -------
            question = "How long do I have to request a refund on an annual plan?"
            posted = _chatwoot(
                client,
                "POST",
                f"/accounts/{ACCOUNT_ID}/conversations/{conversation_id}/messages",
                json={"content": question, "message_type": "incoming"},
            ).json()
            message_id = posted["id"]
            print(f"posted real chatwoot message id={message_id}")

            # --- the delivery, and the same delivery again ----------------
            delivery_id = str(uuid.uuid4())
            payload = {
                "event": "message_created",
                "id": message_id,
                "content": question,
                "message_type": "incoming",
                "account": {"id": int(ACCOUNT_ID)},
                "conversation": {"id": conversation_id, "inbox_id": INBOX_ID},
                "sender": {"id": contact_id, "type": "contact"},
            }
            body = json.dumps(payload, separators=(",", ":")).encode()

            # The contract distinguishes the two: 202 Accepted for a delivery
            # being queued for asynchronous processing, 200 OK for one that was
            # already handled. A redelivery is a success, not an error, or
            # Chatwoot would retry it forever.
            first = _deliver(body, delivery_id=delivery_id)
            print(f"first delivery  -> {first.status_code} {first.text[:120]}")
            if first.status_code != 202 or '"received"' not in first.text:
                print(
                    f"FAIL: a new delivery must be accepted with 202/received, got "
                    f"{first.status_code}.",
                    file=sys.stderr,
                )
                return 1

            # Identical bytes, identical delivery id: a redelivery.
            second = _deliver(body, delivery_id=delivery_id)
            print(f"second delivery -> {second.status_code} {second.text[:120]}")
            if second.status_code != 200 or '"duplicate"' not in second.text:
                print(
                    "FAIL: a redelivery must be reported as 200/duplicate; anything "
                    "else means the dedupe key is not doing its job.",
                    file=sys.stderr,
                )
                return 1

            # --- the customer must see exactly one reply ------------------
            deadline = time.time() + POLL_SECONDS
            replies: list[dict] = []
            while time.time() < deadline:
                replies = _outbound(client, conversation_id)
                if replies:
                    # Give the platform a chance to produce a second one before
                    # concluding there is only one.
                    time.sleep(8)
                    replies = _outbound(client, conversation_id)
                    break
                time.sleep(3)

            events = asyncio.run(_inbox_events(slug), loop_factory=asyncio.SelectorEventLoop)
            print(f"inbox events: {events}")
            print(f"outbound replies: {len(replies)}")

            if not events:
                print(
                    "FAIL: no InboxEvent; check that chatwoot-sidekiq is running.", file=sys.stderr
                )
                return 1
            if len(events) != 1:
                print(
                    f"FAIL: a redelivery wrote {len(events)} InboxEvents; the delivery "
                    "id must dedupe to exactly one.",
                    file=sys.stderr,
                )
                return 1
            if len(replies) != 1:
                print(
                    f"FAIL: the customer received {len(replies)} replies for one "
                    "message. This is the P0 acceptance criterion.",
                    file=sys.stderr,
                )
                return 1

            print(f"\nreply: {replies[0]['content'][:160]!r}")
            print("\nE2E OK: two identical deliveries -> one InboxEvent -> one customer reply")
            return 0
        finally:
            if detached and webhook is not None:
                try:
                    _chatwoot(
                        client,
                        "POST",
                        f"/accounts/{ACCOUNT_ID}/webhooks",
                        json={
                            "url": webhook["url"],
                            "subscriptions": webhook["subscriptions"],
                        },
                    )
                    print("restored the chatwoot webhook")
                except Exception as exc:  # noqa: BLE001 - reported loudly below
                    print(
                        f"WARNING: could not restore the chatwoot webhook: {exc}. "
                        "Re-add it at {url} before running the other e2e.".format(
                            url=webhook["url"]
                        ),
                        file=sys.stderr,
                    )
            if contact_id is not None:
                try:
                    _chatwoot(client, "DELETE", f"/accounts/{ACCOUNT_ID}/contacts/{contact_id}")
                    print(f"deleted chatwoot contact {contact_id}")
                except Exception as exc:  # noqa: BLE001 - cleanup is best effort
                    print(f"warning: could not delete contact {contact_id}: {exc}", file=sys.stderr)
            asyncio.run(_cleanup(slug), loop_factory=asyncio.SelectorEventLoop)
            print(f"cleaned up tenant {slug}")


if __name__ == "__main__":
    raise SystemExit(main())
