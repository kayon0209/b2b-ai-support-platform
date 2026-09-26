# ADR 0013: Inbound Channel Adapters — Email and WeChat

- Status: **Accepted** (2026-09-22) — implemented and verified; ADR 0012
  Stage 2 depended on it
- Date: 2026-09-22
- Required by: [ADR 0012](0012-remove-chatwoot.md) — the removal was gated on
  this, because Chatwoot is the only thing that ingests email and WeChat today
- Related: ADR 0011 (the visitor session), ADR 0006 (realtime data via tools)

## Context

ADR 0012 chose to remove Chatwoot from the stack. Its § Open decision asked what
happens to the channels only Chatwoot ingests, and the answer is **Option B:
build them here.** That makes this ADR a prerequisite, not a follow-up —
deleting Chatwoot without it would leave `/support` as the only customer channel
and silently drop email and WeChat.

So the question this ADR answers is narrow and concrete: **what must a new
inbound channel produce so that the existing pipeline answers it?**

## What the pipeline already requires (measured, not assumed)

The inbound path is `verify → resolve tenant → persist InboxEvent → worker
claims → orchestrator answers`. Reading the consumer
(`apps/worker/src/worker/inbox_consumer.py`) gives the exact contract:

| Requirement | Where | Value an adapter must produce |
|---|---|---|
| Only this event type is acted on | `ACTIONABLE_EVENT_TYPES = {"message_created"}` (`:53`) | `event_type = "message_created"` |
| Only customer messages are answered | `CUSTOMER_MESSAGE_TYPES = {"incoming"}` (`:70`), `is_customer_message` (`:337`) | `message_type = "incoming"` |
| The conversation ref | `_conversation_ref` (`:236`) | `conversation_id` = a **stable** channel conversation key |
| The question text | `resolve_question` (`:294`) | see below |
| Turn provenance | `:474` `turn_source = "chatwoot" if chatwoot_account_id else "platform"` | `chatwoot_account_id` **absent** |
| Delivery | `orchestrator._dispatch` (`:2441`) | no `chatwoot_account_id` ⇒ "this surface is the channel" |

Two of these are already written for us to use:

- **`resolve_question` states the extension point in its own docstring:** *"A
  payload that already carries content (tests, future connectors) is used
  directly."* So an adapter *may* put `content` in the payload.
- **`_local_turn_text`** (`:257`) resolves a body from a **platform-persisted
  turn id**, via the `resolve_turn_text` SECURITY DEFINER function. This is
  exactly how a question typed into `/support` is answered — it has no Chatwoot
  message to fetch either.

## Decision

**One route, one adapter per channel, and an adapter's job is translation — not
persistence.**

```
POST /v1/webhooks/channels/{connector_id}
GET  /v1/webhooks/channels/{connector_id}    # WeChat server-URL verification
```

**A channel is a `connectors` row** (`provider = "email" | "wechat"`), and that
choice is the design, not a shortcut. `connectors` already carries everything an
inbound channel needs, and `resolve_connector_for_webhook` is *already* a
`SECURITY DEFINER` resolver written to break the exact bootstrap cycle this would
otherwise hit. Reusing it means **no new migration and no new secret store**.

The alternative — binding channels in `external_resource_refs`, as this ADR first
proposed — was measured and rejected: that table is **FORCE RLS** with policy
`tenant_id = current_setting('app.tenant_id')`, so an unbound app-role read
returns **zero rows** and every delivery would resolve to no tenant. It has no
secret column either, so it would also have needed a migration for both the
resolver and the credential. `connectors` has both, and the resolver is proven.

The route is separate from `/v1/webhooks/connectors/{id}` rather than folded into
it, because the response contract differs: a connector delivery answers JSON,
while WeChat requires an XML reply inside 5 seconds (§ Consequences).

1. **Resolve the connector and its secret from trusted configuration.**
   `resolve_connector_for_webhook(connector_id)` returns `(tenant_id, provider,
   webhook_secret_ref, status)`; `provider` selects the adapter and `status`
   must be `active`. A provider-supplied tenant field is never read — the same
   rule the connector webhook states.
2. **Verify with the channel's own scheme.** Email reuses the existing
   `verify_webhook` HMAC (the connector path's, not a second implementation).
   WeChat cannot: it signs with `sha1` over the sorted `token`/`timestamp`/`nonce`
   triple, so it gets its own verifier. **A channel that cannot verify must not
   ingest.**
3. **Persist the customer turn, then the event.** The adapter writes the body
   through the existing `chat_service.append_customer_turn` (which redacts
   before storage and serialises duplicates with an advisory lock), then persists
   the `InboxEvent` carrying `message_id = str(turn.id)`.

**Step 3 is the design.** It makes an email or WeChat message *indistinguishable
from a question typed into `/support`* to everything downstream: the consumer
finds the text through the platform path it already has, `turn_source` is
`platform`, and `_dispatch` treats this platform as the channel. No consumer
code changes, and no second copy of the "where is the body" rule.

**`resolve_tenant_from_external` is NOT usable on this path.** It is the same
query as the definer function but runs as the app role, so unbound it silently
finds nothing. It stays for onboarding and tests, where a tenant is bound; the
request path must go through the function. A silent zero-row read is the failure
mode to avoid, not to invite.

### Why the body is not put in `inbox_events`

`resolve_question` would accept `content` in the payload, and it is the shorter
path. It is refused here for a stated reason: `docs/security.md` is explicit that
raw customer content does not persist unminimised, and `minimize.py`'s docstring
makes the same commitment. Storing the body in the inbox row would quietly repeal
both, and the row is metadata for routing — not a store of what the customer
sent. Routing it through `conversation_turns` keeps the content in the one place
retention (`RetentionPolicy.conversation_turn_days`) already governs.

## Consequences

- **Email and WeChat become first-class channels with no new consumer code.** The
  adapters are ~translation and verification only.
- **The outbound leg is NOT in this ADR, and must be built separately.** This
  covers messages *in*. `_dispatch` currently means "the platform surface is the
  channel", so an answer to an email question would be persisted and visible in
  `Workbench` but never emailed. That is the documented "persist first, wire the
  consumer later" posture (the connector webhook does the same), and it is
  reversible because the event is on record. **It is still a gap for a real
  customer, and it should be ADR 0014.**
- **WeChat's 5-second reply rule conflicts with this platform's run model.** The
  official account protocol expects a synchronous XML reply to the POST, or it
  retries (three times) and then shows the user nothing. But an agent run is
  queued and answers asynchronously — measured on this deployment at 8–68s. So
  WeChat must be answered with the passive "已收到" reply and the real answer sent
  as a **customer-service message** via the outbound leg. **This is a
  consequence of the async model, not a bug to fix in the adapter.**
- WeChat retries are deduplicated by the existing `uq_inbox_delivery` index only
  if `delivery_id` is stable across retries. WeChat's `MsgId` is, and it is what
  the adapter must use — a random delivery id would turn one question into three.
- A channel binding is a **`connectors` row** (`provider = "email"` with
  `webhook_secret_ref` set, or `provider = "wechat"`). Onboarding writes it;
  nothing infers it from traffic, and the inbound URL is
  `/v1/webhooks/channels/{connector_id}`.

## Staging

- **Stage A — the core + both inbound adapters.** ✅ **Done 2026-09-22.**
  `platform_core/channels/` (`base.py` contract, `email.py`, `wechat.py`,
  `router.py`); `persist_inbox_event` takes a `minimized_payload` and
  `build_envelope` takes a `source`; the connector webhook's resolver and secret
  reader moved to `integrations/inbound.py` so both routes share one copy.
  Guard: `apps/api/tests/integration/test_channel_webhooks.py`, 16 tests, all
  passing — a signed email and a signed WeChat text each persist a turn and an
  answerable event, unsigned/wrongly-signed deliveries of either kind are
  refused, a retry collapses onto one row, and the WeChat challenge is not
  answered without a valid signature.

  Two real defects were found by writing those tests, and both would have
  shipped:

  1. **Header lookup was case-sensitive.** The adapter read
     `request.headers.get("X-Webhook-Signature")` off a plain `dict`, but header
     names are case-insensitive and httpx (and Starlette's TestClient) lowercase
     them. It passed in a hand-built probe and 401'd against every real client.
     Fixed with `channels.base.header()`, and mutation-tested: reverting it fails
     five tests.
  2. **A duplicate delivery answered `202`.** The response used the caller's
     status code for both branches, so a provider retry was told "new work
     accepted". Now `200` for a duplicate, matching the connector webhook.

  The `verified_account: ""` line is mutation-tested too: removing it fails
  `test_a_channel_sender_is_stored_as_anonymous`.
- **Stage B — the outbound return leg.** ✅ **Done 2026-09-22** —
  [ADR 0014](0014-outbound-channel-delivery.md). `_dispatch` routes a channel
  message to its transport; an unconfigured channel is reported as
  `OUTBOUND_NOT_CONFIGURED` rather than returning the `""` that means
  "delivered". Note that until this landed, the inbound adapters produced
  answers the customer never received, and the run recorded success.
- **Stage C — remove Chatwoot.** ADR 0012's Stage 2, unblocked once Stage A and B
  are verified.

## What this does not do

- It does not add attachments *upload* for the new channels. The payload carries
  `attachment_types` only, which is what the minimisation policy allows and what
  the run needs to know ("evidence was already supplied").
- It does not make `/support` and email share one conversation. A customer who
  writes an email and then opens the web window is two conversations, exactly as
  ADR 0011 already accepted for Chatwoot.
- It does not touch the operator surfaces.
