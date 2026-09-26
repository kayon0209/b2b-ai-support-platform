# ADR 0014: Outbound Channel Delivery

- Status: **Accepted** (2026-09-22) — implemented and verified; ADR 0012
  Stage 2 depended on it
- Date: 2026-09-22
- Completes: [ADR 0013](0013-inbound-channel-adapters.md) Stage B
- Required by: [ADR 0012](0012-remove-chatwoot.md) — its Stage 2 removes Chatwoot,
  which is the only thing that delivers email and WeChat today

## Context

ADR 0013 built the inbound half: a signed email or WeChat delivery becomes a
persisted turn and an answerable run. The outbound half did not exist, and its
absence was silent in the worst way.

`orchestrator._dispatch` decides delivery like this:

```python
if not chatwoot_account_id:
    # ... this surface is the delivery channel. This is not a failed send ...
    return ""
```

`""` means **delivered**. A channel conversation has no Chatwoot account id — so
every email and WeChat answer took that branch, returned success, and was never
sent. The run completed, the answer was stored, `Workbench` showed it, and the
customer received nothing. That is the `worker_cannot_send` failure shape: no
error, no log, no dashboard signal.

## Decision

**A channel message carries the channel it arrived on, and `_dispatch` routes it
to that channel's transport.**

1. **The event says which channel.** `channel_system` is written by the channel
   route (ADR 0013) and read by the consumer. No new field was needed for the
   destination: `contact_id` already holds the customer's address for email and
   the openid for WeChat, and `conversation_id` already holds the thread key.
2. **Routing order is load-bearing.** The channel branch runs **before** the
   `sender is None` guard, because `sender` is the *Chatwoot* client and a
   deployment that delivers over email and WeChat has no Chatwoot token. Running
   the guard first would return "delivered" and swallow every channel answer —
   the same defect, one level down. Mutation-tested: gating the branch behind
   `sender` fails seven tests.
3. **A transport registry, not a branch.** `channels/outbound.py` holds a
   `ChannelSender` keyed by system. Adding a channel must not mean editing the
   run path.
4. **An unconfigured channel is reported, not silently successful.** A channel
   with no credentials is *absent* from the registry, and `_dispatch` returns
   `OUTBOUND_NOT_CONFIGURED`.

### Why `OUTBOUND_NOT_CONFIGURED` does not fail the run

The obvious reading is "no transport means the delivery failed, so mark the run
FAILED". That is wrong here, and the reason is mechanical: **the agent turn is
only written for a COMPLETED run**. Failing would discard the answer *and* the
platform's only record of it, so a receive-only deployment could not even show
what it had produced — trading a silent non-delivery for a silent data loss.

So it joins `SHADOW_MODE` as a *withheld* delivery: the run completes, the reason
is recorded on the run, and a warning is logged. The failure taxonomy is:

| Outcome | Reason | Run |
|---|---|---|
| Sent | `""` | COMPLETED |
| No transport for this channel | `OUTBOUND_NOT_CONFIGURED` | COMPLETED (withheld, logged) |
| Transport known, no address | `OUTBOUND_TARGET_MISSING` | FAILED |
| Transport raised | `OUTBOUND_FAILED` | FAILED |
| Transport cannot tell if it sent | `OUTBOUND_AMBIGUOUS` | FAILED (retryable) |

### Email closes the threading loop

The outbound message sets `In-Reply-To` and `References` to the **thread root**,
which is the same value `channels.email` reads to derive `conversation_key` on
the way in. So a customer's reply lands on the same conversation. Using the
*parent* message instead would resolve to a different key and split the thread on
every exchange — the trap ADR 0013 documents on the inbound side, and it has to
be avoided on the outbound side too.

### WeChat uses the customer-service API, not the passive reply

The passive XML reply must go back within five seconds (ADR 0013); a run takes
8–68s here. The customer-service message API has no such deadline, so it is the
delivery path for the real answer, and the passive reply stays what it is: an
acknowledgement.

## Consequences

- **`require_sender` had to change.** It raised unless a Chatwoot token was
  present, which would have made ADR 0012's removal impossible: a channel-only
  deployment has no Chatwoot token and is not misconfigured. It now refuses only
  when **no** transport exists at all, which is what the guard was always for.
- **Idempotency is unchanged**: `command_id` is `run:<id>` on both paths, so a
  retry of one run cannot deliver twice. No new mechanism was invented.
- **No retry queue and no delivery receipt.** The run's own retry is the retry,
  and a receipt needs a provider that reports one. Both are real gaps and neither
  is half-built here.
- **What is NOT verified.** This environment has no SMTP server and no WeChat
  credentials, so **no live send has been observed**. What is verified is the
  decision logic — routing, the guard order, the failure taxonomy, the command
  id, and which channels get registered — with a fake transport, plus mutation
  tests on the two guards that matter. The transports themselves
  (`EmailSmtpTransport`, `WeChatTransport`) are thin and unexercised against a
  real provider; that is stated rather than implied, and a live smoke test is the
  first thing to write when credentials exist.
- **Per-tenant sender identity is not modelled.** `email_from_address` is
  deployment configuration. A multi-tenant deployment that wants
  `support@customer.example` per tenant needs a per-tenant sender, which is the
  first thing to add if the pilot grows past one tenant.
