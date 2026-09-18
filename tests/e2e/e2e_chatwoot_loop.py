"""End-to-end through the real Chatwoot kernel, both directions.

Exercises the Phase 1 loop against a live Chatwoot, which nothing did before:
the test suite covers the webhook contract and the outbound client separately,
but nothing proved the two halves meet.

    customer message (Chatwoot)
      -> Chatwoot webhook, signed with the inbox secret
      -> POST http://ai-api:8000/v1/webhooks/chatwoot   (the real endpoint)
      -> InboxEvent persisted, tenant resolved from the account mapping
      -> interactive worker: orchestrator -> abstention or cited answer
      -> ChatwootClient.send_message
      -> a message visible in the same conversation

Requires the compose stack (ai-api, ai-worker-interactive, chatwoot-web) and a
filled `.env`. Run:

    ./.venv/Scripts/python.exe tests/e2e/e2e_chatwoot_loop.py

The tenant it creates is throwaway and is removed afterwards, including its
account mapping; the Chatwoot contact is deleted too, which removes the
conversation with it.
"""

from __future__ import annotations

import asyncio
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
API_BASE = os.environ.get("APP_API_BASE_URL", "http://localhost:8000")

# The API Inbox the Chatwoot fixture created, and the account it belongs to.
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


async def _seed_tenant(slug: str) -> uuid.UUID:
    """Create a tenant and map it to the Chatwoot account.

    The mapping is what makes the webhook actionable: tenant resolution reads
    trusted configuration, never a tenant id from the payload.
    """
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


async def _inbox_events(slug: str) -> list[tuple[str, str, str]]:
    engine = create_async_engine(PLATFORM_URL)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT e.event_type, e.status, e.delivery_id "
                        "FROM inbox_events e JOIN tenants t ON t.id = e.tenant_id "
                        "WHERE t.slug = :slug ORDER BY e.id"
                    ),
                    {"slug": slug},
                )
            ).all()
        return [(str(r[0]), str(r[1]), str(r[2])) for r in rows]
    finally:
        await engine.dispose()


def main() -> int:
    if not CHATWOOT_TOKEN:
        print("FATAL: APP_CHATWOOT_API_TOKEN is not set.", file=sys.stderr)
        return 2

    slug = f"e2e-chatwoot-{uuid.uuid4().hex[:8]}"
    contact_id: int | None = None
    conversation_id: int | None = None

    with httpx.Client(timeout=20.0) as client:
        try:
            # --- 0. the stack must be reachable ---------------------------
            health = httpx.get(f"{API_BASE}/healthz", timeout=5.0)
            if health.status_code != 200:
                print(
                    f"FATAL: API not healthy at {API_BASE}: {health.status_code}",
                    file=sys.stderr,
                )
                return 2
            _chatwoot(client, "GET", "/profile")
            print(f"chatwoot reachable; api reachable at {API_BASE}")

            # --- 1. seed the mapping the webhook resolves through ---------
            asyncio.run(_seed_tenant(slug), loop_factory=asyncio.SelectorEventLoop)
            print(f"tenant slug: {slug} -> chatwoot account {ACCOUNT_ID}")

            # --- 2. a real contact and conversation in the API inbox ------
            suffix = uuid.uuid4().hex[:8]
            contact = _chatwoot(
                client,
                "POST",
                f"/accounts/{ACCOUNT_ID}/contacts",
                json={"name": f"E2E Customer {suffix}", "email": f"e2e-{suffix}@example.test"},
            ).json()
            contact_id = contact["payload"]["contact"]["id"]
            # The API channel takes `source_id` from the caller: it is how the
            # external system names this end user. Chatwoot does not mint one,
            # and the contact carries no `contact_inboxes` entry until the
            # conversation links it.
            contact_source_id = f"e2e-{suffix}"

            conversation = _chatwoot(
                client,
                "POST",
                f"/accounts/{ACCOUNT_ID}/conversations",
                json={
                    "source_id": contact_source_id,
                    "inbox_id": INBOX_ID,
                    "contact_id": contact_id,
                },
            ).json()
            conversation_id = conversation["id"]
            print(f"contact={contact_id} conversation={conversation_id}")

            # --- 3. the customer speaks -----------------------------------
            question = "How long do I have to request a refund on an annual plan?"
            _chatwoot(
                client,
                "POST",
                f"/accounts/{ACCOUNT_ID}/conversations/{conversation_id}/messages",
                json={"content": question, "message_type": "incoming"},
            )
            print(f"posted incoming: {question!r}")

            # --- 4. the loop has to close ---------------------------------
            # An outbound message from anyone other than the customer is the
            # proof: the platform answered, and it reached Chatwoot.
            deadline = time.time() + POLL_SECONDS
            outbound: dict | None = None
            while time.time() < deadline:
                messages = _chatwoot(
                    client,
                    "GET",
                    f"/accounts/{ACCOUNT_ID}/conversations/{conversation_id}/messages",
                ).json()["payload"]
                for message in messages:
                    # `message_type == 1` is outgoing, i.e. from the agent side.
                    # An earlier version of this check accepted 0 as well and
                    # "passed" by matching the customer's own message - a false
                    # pass that hid the fact that nothing had been delivered.
                    if message.get("message_type") == 1 and message.get("content"):
                        outbound = message
                        break
                if outbound:
                    break
                time.sleep(3)

            events = asyncio.run(_inbox_events(slug), loop_factory=asyncio.SelectorEventLoop)
            print(f"inbox events: {events}")

            # An outbound message with no InboxEvent would mean we answered
            # something we never received - impossible in this design, so treat
            # it as a failure rather than trusting the message alone.
            if not events:
                print(
                    "\nFAIL: no InboxEvent was persisted, so the webhook never reached "
                    "the API. Check that chatwoot-sidekiq is running: Chatwoot delivers "
                    "webhooks through a Sidekiq job, and with it down nothing is sent.",
                    file=sys.stderr,
                )
                return 1

            if outbound is None:
                print(
                    f"\nFAIL: no outbound message within {POLL_SECONDS}s.\n"
                    "The webhook reached us only if an inbox event exists; if it does, "
                    "check the ai-worker-interactive logs.",
                    file=sys.stderr,
                )
                return 1

            print(f"\noutbound message: {outbound['content'][:200]!r}")
            print(
                "\nE2E OK: customer message -> signed webhook -> InboxEvent -> "
                "orchestrator -> Chatwoot reply, visible in the conversation"
            )
            return 0
        finally:
            # Chatwoot first: deleting the contact removes its conversations,
            # and it is the part that cannot be recovered from our database.
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
