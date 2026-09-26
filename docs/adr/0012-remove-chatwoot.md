# ADR 0012: The Platform Hosts Its Own Customer Channel; Chatwoot Leaves The Stack

- Status: **Done** (2026-09-22). All four stages have run: the surviving
  channel works, nothing routes through Chatwoot, the adapter is deleted, and
  `AGENTS.md` rules 1 and 2 were rewritten in the same change that made them
  true. § Stage 2 inventory is kept as the record of what was measured before
  the cut.
- Date: 2026-09-22
- Supersedes: ADR 0001 (the "Chatwoot is the kernel" commitment), ADR 0010 (its
  "the customer surface is Chatwoot" half)
- Amends: `AGENTS.md` rules 1 and 2 — **only when this is accepted and the
  removal lands**, not before
- Related: [ADR 0011](0011-customer-chat-is-a-visitor-session.md)

## Context

`AGENTS.md` rule 1 makes Chatwoot the customer-service kernel and forbids
changing it without an ADR; rule 2 forbids custom services from touching its
database. ADR 0010 read those rules as "the customer surface is Chatwoot, and
`/chat` is internal". ADR 0011 amended that the same day, because the pilot's
value is the **data card**, which Chatwoot cannot render, and built `/support`
as a visitor session — a second channel with its own record.

The product owner has now decided the direction plainly: **do not use Chatwoot;
the platform hosts the conversation.** This ADR is that decision's home, because
rule 1 requires one and because the removal has consequences that must be
written down rather than discovered.

What Chatwoot does today, and nothing else does:

1. **Inbound.** Every customer message that is not sent from `/support` arrives
   as a signed Chatwoot webhook (`support_bridge/router.py` →
   `integrations/webhook_router.py` → `inbox`), which is the only way a
   conversation obtains a `conversation_ref` from outside this repository.
2. **Outbound.** The agent's answer is delivered by
   `orchestrator._dispatch` through a `ChatwootClient`-shaped `sender`
   (`orchestrator.py:145`, `:2441`).
3. **Identity and mapping.** A Chatwoot account id is resolved to a tenant
   (`support_bridge/mapping.py`) — server-side, never client-supplied.
4. **Channels the platform does not implement.** Email, WeChat and anything else
   Chatwoot ingests. ADR 0011 says so explicitly: *"no inbound email/WeChat
   integration for the visitor surface (**Chatwoot keeps that job**)"*.

## Decision

**The platform's own surfaces become the product: `/support` for customers and
`Workbench` for agents. Chatwoot is removed from the stack — the service, the
webhook path, the REST client and the compose profile.**

Two commitments from ADR 0010 survive unchanged and are not reopened here:

- `/chat` remains an internal verification surface and keeps its operator token.
- `/v1/customer/*` keeps requiring `CASE_READ` / `CASE_UPDATE`.

## The channels decision — ANSWERED 2026-09-22: **Option B**

> **Resolved: build the adapters.** The product owner chose to re-implement
> email and WeChat in this repository before Chatwoot is removed. The design is
> [ADR 0013](0013-inbound-channel-adapters.md). Stage 2 below therefore waits on
> that work rather than on a decision — and it must not start until Stage A
> (both inbound adapters, verified) and Stage B (the outbound leg, ADR 0014) are
> done, or the channels are lost silently.

**What happens to the channels only Chatwoot ingests (email, WeChat, social)?**

Removing Chatwoot removes them, because nothing in this repository implements
them. There is no third option where they keep working: their inbound path *is*
the Chatwoot webhook. So:

- **Option A — one channel is enough.** `/support` is the pilot's only customer
  channel. The removal is a deletion, and this ADR records the loss as accepted.
- **Option B — the channels must be replaced.** Each one needs its own inbound
  adapter writing into `inbox` with its own signature verification. That is a
  feature, not a refactor, and it should get its own ADR; this ADR then only
  removes Chatwoot *after* at least the pilot-critical channel exists.

The migration plan below is written so that Stage 0 and Stage 1 are identical
under both options. **Stage 2 differs, and must not start until this is
answered.**

## What goes, what stays — `support_bridge` is not "the Chatwoot module"

This is the trap. The package is named after the integration, so deleting it
looks like deleting the integration — but it also holds primitives the
platform's *own* channel is built on. Deleting it wholesale takes `/support`
down with Chatwoot.

**Goes (Chatwoot adapter):**

| Item | Why |
|---|---|
| `support_bridge/chatwoot_client.py` | The REST client. Nothing to talk to. |
| `support_bridge/router.py` | The Chatwoot webhook endpoint. |
| `support_bridge/mapping.py` | Resolves a *Chatwoot account id* to a tenant. No Chatwoot payloads, no account ids. |
| `support_bridge/minimize.py` → `minimize_chatwoot_payload` | Redacts Chatwoot payloads. |
| `orchestrator._dispatch`'s `sender` branch | The outbound leg. |
| `infra/compose`'s `chatwoot` profile (web, sidekiq, 2 pg, 2 redis), k8s manifests, `0002_bridge_mappings`, `0032_chatwoot_tenant_resolver` | Its infrastructure and its tables. |

**Stays (platform-owned — verify each before deleting anything):**

| Item | Kept because |
|---|---|
| `support_bridge/conversation_ref.py` | `conversation_ref_for(tenant, external)` is how `/support` and `router.py` derive a conversation. **Load-bearing for the surviving channel.** |
| `support_bridge/visitor_token.py` | ADR 0011's credential. |
| `support_bridge/inbox.py`, `models.py` | The `InboxEvent` table and the outbox. Channel-agnostic; only its *producers* change. |
| `support_bridge/minimize.py` → `payload_hash` | Imported by `chat_service` and `customer_router`. |
| `support_bridge/webhook_security.py` (`sign_payload`, `verify_webhook`, `WebhookVerificationError`) | **Reused by the *connector* webhooks** — `integrations/webhook_router.py` imports all three — which are a different feature: CRM/ERP callbacks, not customer channels. Deleting this module breaks them. |
| `support_bridge/continuity.py`, `channel_format.py`, `csat.py`, `inbound_media.py` | Channel-agnostic: continuity across devices, format adaptation, satisfaction, media-as-attachment. Each mentions Chatwoot 0–1 times. |

**Rule for the removal: delete a file only after `grep -rn` shows every importer
is also going.** `webhook_security` and `minimize` each have two consumers, and
only one of each pair is Chatwoot's.

## Consequences

- **A conversation started on `/support` was already invisible in Chatwoot**
  (ADR 0011's stated price). After this, *every* conversation is, because
  Chatwoot is gone. `Workbench` is where an operator watches them.
- **`/support` must be finished before Chatwoot is deleted.** It is the only
  surviving customer channel, and its headline feature (the data card) was
  unreachable until 2026-09-22, because the page never called
  `POST /v1/support/verify` and the read path therefore answered
  `IDENTITY_REQUIRED` before any connector call. **That is fixed and verified**
  (`scripts/support_card_smoke.cjs`, 6/6 green). Deleting Chatwoot first would
  have left the product with no working customer channel at all.
- **`/support` becomes anonymous-spend exposure.** ADR 0011 already noted a
  public `/support` accepts anonymous model spend bounded only by the monthly
  quota and queue depth. With Chatwoot gone there is no second channel to fall
  back on, so the per-environment decision ADR 0011 asked for becomes mandatory
  rather than advisable.
- `EXPECTED_MIGRATIONS` and the two `TENANT_TABLES` lists must move together when
  migrations are dropped — `migration_count` reads the **disk**, so a local green
  run is not evidence. Count `git ls-tree`.
- Roughly **40 test files** mention Chatwoot. Most are about behaviour that
  survives (RLS isolation, handoff, dispatch) and should be *re-pointed* at the
  visitor session, not deleted; the Chatwoot-specific ones
  (`tests/e2e/e2e_chatwoot_loop.py`, `e2e_chatwoot_duplicate_delivery.py`,
  `unit/support_bridge/test_chatwoot_client.py`, `test_webhook_security.py`,
  `test_minimize_attachments.py`) go with the code they test.

## Migration plan

Each stage ends in a state that is *verified*, not merely committed to.

- **Stage 0 — the surviving channel works.** Wire the identity step into
  `/support`; make it reachable from the console. ✅ **Done 2026-09-22** —
  `support_card_smoke.cjs` 6/6, `admin_render_check.cjs` 6/6 `errors=0`.
- **Stage 1 — stop depending on Chatwoot for the demo path.** Nothing new is
  routed through it; the pilot runs entirely on `/support` + `Workbench`. This
  is where the product becomes demonstrable without Chatwoot running. Guard: the
  customer loop must pass with the `chatwoot` profile **stopped**.
  ✅ **Verified 2026-09-22.** With all four Chatwoot containers stopped (the AI
  side is unaffected — no `depends_on` either way), `support_card_smoke.cjs`
  passed 6/6 (`the card, the data behind it and the answer agree`, 68s). The
  decisive number is that `worker_cannot_send` appeared **zero** times in the
  worker log: a visitor conversation does not attempt to reach Chatwoot at all,
  because `orchestrator._dispatch` already treats "no Chatwoot account id" as
  "this surface is the channel" (ADR 0011).
- **Stage 2 — remove the adapter.** Only after § Open decision. Delete the
  "Goes" table, split `support_bridge`, re-point the surviving tests, drop the
  migrations with the count updated, remove the compose profile and k8s
  manifests.
- **Stage 3 — update `AGENTS.md`.** Rules 1 and 2 are rewritten in the same
  commit that makes them true. Leaving them stale would be the same defect this
  repository keeps finding: a document that describes a system that no longer
  exists.

## Stage 2 inventory (measured 2026-09-22)

The scary number is ~90 files; the real one is far smaller. `grep -il chatwoot` counts **mentions**, and
most are comments and docstrings. The actual coupling, measured by imports and route usage:

**Production**

| Surface | Files |
|---|---|
| Route registration | `main.py` (import + `include_router`) |
| The adapter | `support_bridge/router.py`, `chatwoot_client.py`, `mapping.py` (Chatwoot helpers), `inbound_media.py` |
| Worker wiring | `worker/wiring.py` (sender/reader), `worker/inbox_consumer.py` (the fetch path) |
| Dispatch | `orchestrator.py` — the Chatwoot branch of `_dispatch`, and `_send_handoff_note` |
| Config | `config.py` (`chatwoot_*`) |
| Middleware | `identity/middleware.py` — `/v1/webhooks/chatwoot` in `EXEMPT_PATHS` |
| Infra | `infra/compose` (6 services + the `chatwoot` profile), `infra/kubernetes` |
| Migrations | `0002_bridge_mappings`, `0032_chatwoot_tenant_resolver` |

**Tests** — three POST to the route, three import the client: `test_webhook_ingest.py` (6 tests, all
Chatwoot, zero connector coverage), `test_e2e_acceptance.py`, `unit/identity/test_tenant_context.py`
(one exemption assertion), `unit/support_bridge/test_chatwoot_client.py`, `test_chatwoot_contact.py`,
`test_minimize_attachments.py`, `unit/worker/test_wiring.py`.

### The prerequisite: `duplicate_replies` lives *entirely* on the Chatwoot path

`@pytest.mark.zero_tolerance` is what the release gate reads. Grouping the marks by file:

| Category | Files | Chatwoot coupling |
|---|---|---|
| `cross_tenant_violations` (15) | leak_surfaces, cross_tenant_negative, rls_isolation | low (0–2 mentions) |
| `unauthorized_writes` (4) | `unit/tool_gateway/test_gateway.py` | none |
| **`duplicate_replies` (3)** | **`test_e2e_acceptance.py` (14), `test_orchestrator_lease_race.py` (15)** | **total** |

So deleting the Chatwoot path as-is would take `duplicate_replies` from 3 tests to **zero** and
**remove the category from the release gate** — a silent loss of a zero-tolerance guarantee, and the
opposite of what this change is for.

**Already done:** `test_channel_webhooks.py::test_a_retry_does_not_become_a_second_question` now carries
`zero_tolerance("duplicate_replies")`, so the guarantee exists on the surviving channel *before* the old
path goes. The other two travel with `test_e2e_acceptance.py` and `test_orchestrator_lease_race.py`,
which must be **re-pointed at the channel or `/support` path, never simply deleted**.

**Order within Stage 2:** re-point the zero-tolerance tests, confirm the gate still reports
`duplicate_replies`, and only then delete the adapter. Doing it the other way round is how a security
gate gets quietly weakened by a "cleanup".

## What Stage 2 changed, measured

- **`duplicate_replies` kept its four carriers.** The prerequisite was met
  first: `test_channel_webhooks.py` already carried the marker, and
  `test_e2e_acceptance.py` / `test_orchestrator_lease_race.py` were re-pointed at
  the channel and the platform surface rather than deleted. The gate still
  reports `duplicate_replies: 4 passing test(s)`.
- **Two things the removal exposed, both fixed here.**
  1. `_finish_abstain` and `_finish_clarify` dispatched without a channel, so on
     email or WeChat the *notice* — "a human is coming" — fell through to the
     platform-surface branch, returned `""` (which means **delivered**), and
     reached nobody. The channel now travels with the notice.
  2. The routing team (7.3) and the handoff context lived only in the Chatwoot
     private note. Deleting the note would have made them values nothing reads,
     so they are recorded on the handoff's audit event `metadata` — the record
     an operator or reviewer actually opens. (`after=` is hashed and unreadable;
     `metadata` is the documented exception for an event's own parameters.)
- **`external_resource_refs` now has no production reader or writer.** It was
  the Chatwoot account→tenant mapping. The table and its model stay (dropping a
  table is a contract migration, not a cleanup), but this is the repository's
  recurring defect shape and is recorded here rather than left to be discovered:
  either onboarding writes to it again, or a later migration drops it.

## What Stage 2 changed, measured

- **`duplicate_replies` kept its four carriers.** The prerequisite was met
  first: `test_channel_webhooks.py` already carried the marker, and
  `test_e2e_acceptance.py` / `test_orchestrator_lease_race.py` were re-pointed at
  the channel and the platform surface rather than deleted. The gate still
  reports `duplicate_replies: 4 passing test(s)`.
- **Two things the removal exposed, both fixed here.**
  1. `_finish_abstain` and `_finish_clarify` dispatched without a channel, so on
     email or WeChat the *notice* — "a human is coming" — fell through to the
     platform-surface branch, returned `""` (which means **delivered**), and
     reached nobody. The channel now travels with the notice.
  2. The routing team (7.3) and the handoff context lived only in the Chatwoot
     private note. Deleting the note would have made them values nothing reads,
     so they are recorded on the handoff's audit event `metadata` — the record
     an operator or reviewer actually opens. (`after=` is hashed and unreadable;
     `metadata` is the documented exception for an event's own parameters.)
- **`external_resource_refs` now has no production reader or writer.** It was
  the Chatwoot account→tenant mapping. The table and its model stay (dropping a
  table is a contract migration, not a cleanup), but this is the repository's
  recurring defect shape and is recorded here rather than left to be discovered:
  either onboarding writes to it again, or a later migration drops it.

## What this does not do

- It does not add an email/WeChat adapter (Option B above would).
- It does not remove `/chat`, or relax the operator token on `/v1/customer/*`.
- It does not touch `identity`, `cases`, `knowledge`, `retrieval`,
  `tool_gateway`, `evaluation` or `audit` — none of them import Chatwoot.
