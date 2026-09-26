# ADR 0011: The Customer Chat Surface Is A Visitor Session

- Status: Accepted
- Date: 2026-09-21
- Amends: [ADR 0010](0010-platform-chat-is-internal.md)

## Context

ADR 0010 declined to host a customer-facing chat: `/chat` required `CASE_READ`
(an operator permission) plus an operator bearer token, so a customer opening it
received 401 on both the timeline and the send, and the decision was to keep the
customer surface in Chatwoot. Its own closing line named the condition under
which that would change: *a customer-facing chat "changes who can spend the
tenant's model budget", so "if one is ever built, it starts with an ADR of its
own."*

It has now been built, for a reason the audit could not see: the pilot's
customer is an engineer at 2am asking whether a batch can be expedited, and the
platform's value in that exchange is the **data card** — order status, shipment
nodes, the evidence behind an answer. Chatwoot is the kernel for inbound
conversations (rule 1), but it has no way to render a card the platform
assembled from an order system, and the pilot's demonstration depends on the
customer seeing one. So a customer-openable window exists in this repository
after all, and the question ADR 0010 deferred has to be answered rather than
deferred again.

## Decision

**The platform hosts a customer chat as a *visitor session*: a credential bound
to exactly one conversation, issued without membership, whose spend is capped by
the same gates an operator's run goes through.**

Concretely, as implemented:

- **The credential names one `(tenant, conversation)` pair and nothing else**
  (`support_bridge/visitor_token.py`). `vs_<payload>.<signature>`, HMAC-SHA256
  over the encoded payload with the deployment secret, 12-hour expiry. It
  carries **no role** — that is the mechanism, not an omission. Every operator
  policy gate denies a role-less context, so this token cannot reach an
  operator endpoint even if a route were mis-wired; the visitor's authorization
  is the *binding*, and the handler compares the request against the binding
  instead of trusting a path value.
- **Issuance needs no identity**: `POST /v1/support/sessions` takes a tenant
  slug and an opaque visitor handle (localStorage), and answers unknown and
  suspended tenants with the same message so the endpoint cannot enumerate
  tenants. Re-supplying the handle resumes the same conversation.
- **The send path is one call** that persists the turn and queues the run
  (`/v1/support/messages`), because a customer who completes two calls and loses
  the second leaves a question stored but never answered. It requires an
  `Idempotency-Key`, like every write.
- **Spend is capped where ADR 0010 wanted it capped.** A visitor run goes
  through `chat_service.queue_agent_run` — the same monthly quota gate, the same
  queue-depth backpressure, the same 429s — so "anyone may post" does not mean
  "anyone may spend without bound". Refusal still persists the turn, so a 429 is
  not data loss.
- **Delivery is the platform's own surface when the conversation is not backed
  by Chatwoot.** A visitor event carries no Chatwoot account id, and
  `orchestrator._dispatch` treats that as "this surface is the channel", not as
  a failed send — treating it as a failure is what made every such answer
  vanish while the run reported FAILED.

## Why this does not reopen ADR 0010's three arguments

- **Rule 1 (Chatwoot is the kernel).** Not amended in the way that matters:
  Chatwoot remains the customer-service kernel and the source of
  `conversation_ref` for every conversation that arrives through it. The visitor
  surface is a *second, separate channel for the pilot*, and its record is the
  platform's own (`conversation_turns`) rather than a rival copy of Chatwoot's.
  What it does cost — and this is the honest price — is that a pilot-stage
  support conversation does not appear in Chatwoot, so an operator watching only
  Chatwoot does not see it. The workbench is where those conversations live.
- **The identity handle already exists.** True for conversations that arrive
  through Chatwoot. A visitor conversation starts here, so there is no Chatwoot
  handle to reuse; the token *is* the handle, and it is deliberately the
  narrowest one that works — one conversation, no role, 12 hours.
- **"Anyone may post" means "anyone may spend".** Still true, and unanswered by
  identity — the answer is the two gates plus the fact that the blast radius of
  a leaked credential is one conversation for twelve hours, not a tenant.

## Consequences

- The customer experience is no longer "not implemented here". `/support` is a
  product surface and is labelled as one; `/chat` remains the internal
  verification surface and keeps its operator token.
- **A support conversation and a Chatwoot conversation are two different
  records.** Nothing links them today. If a pilot customer is expected to move
  between the widget and the portal, that link is a design decision of its own.
- A deployment that exposes `/support` publicly is accepting anonymous model
  spend bounded only by the quota and queue-depth gates. That is a deployment
  choice, and it should be made deliberately per environment rather than copied.
- The token is not revocable before expiry (12h, one conversation). Revocation
  would need a server-side session table, which is the first thing to add if a
  support conversation ever carries more than read-and-append.
- What is **not** here, on purpose: no inbound email/WeChat integration for the
  visitor surface (Chatwoot keeps that job), no attachment upload, no
  operator-visitor presence. Each of those is a reason to reopen this ADR.

## References

- [ADR 0010](0010-platform-chat-is-internal.md) — the decision this amends, and
  the three arguments above.
- `support_bridge/visitor_token.py` — the credential and the reasoning behind
  its shape, including why it carries the raw external id.
- `agent_runtime/support_router.py` — issuance, the binding check, and the
  single-call send path.
- `agent_runtime/chat_service.py` — the gates every queued run goes through.
- `FINDINGS-2026-09-21-CARD-AND-RLS.md` — the isolation defects found on the
  worker path that this surface's runs depend on.
