# ADR 0010: The Customer Surface Is Chatwoot; `/chat` Is Internal

- Status: Accepted
- Date: 2026-09-21

## Context

`/chat` is a chat panel served from the same bundle as the operator console. It
was added so the write path could be exercised without Chatwoot: before it, the
only way to start a run was a signed webhook, so a run started from our own
screen had nothing to read and completed without answering.

The 2026-09-21 audit found that the surface is not usable by a customer. Its
endpoints require `CASE_READ` — an operator permission — and a bearer token, so
a customer opening `/chat` receives 401 on both the timeline and the send. The
UI made this worse by swallowing the failure, so the page looked like an empty
conversation rather than a rejected one.

That raised a real question: should the platform host a customer-facing chat at
all, and if so how would a customer be identified?

## Decision

**No. The customer surface is Chatwoot, and `/chat` is an internal
verification surface.**

The platform keeps the operator-token requirement on `/v1/customer/*`. It does
not grow a customer identity scheme, a session token, or a public send path.

## Why

- **AGENTS.md rule 1** makes Chatwoot the customer-service kernel. Hosting a
  second customer channel duplicates its job and splits the record of a
  conversation across two systems.
- **The platform's customer identity handle already exists and comes from
  Chatwoot**: `conversation_ref`, derived from the signed webhook. A customer
  identity scheme invented here would be a second, weaker answer to a question
  that is already answered — and the weaker one would be the one exposed.
- **An unauthenticated send path is not a neutral convenience.** Each accepted
  message queues an agent run, which costs a model call. "Anyone may post"
  means "anyone may spend", and the platform's own rate limiter is sized for a
  tenant's operators, not for the open internet.
- The surface is genuinely useful as it stands: it is how the write path,
  receipt publishing, handoff notes and the data card are verified end to end.

## Consequences

- `/chat` is labelled in the UI as an internal verification surface, so it
  cannot be mistaken for the product's customer experience. The 401 now says
  what it is instead of rendering as an empty conversation.
- **The real customer experience is not implemented here and is not a gap in
  this repository.** A customer-facing chat would be a Chatwoot widget, or a
  signed per-conversation link issued by Chatwoot. Neither is needed for the
  pilot; if one is ever built, it starts with an ADR of its own, because it
  changes who can spend the tenant's model budget.
- A deployment must not expose `/chat` publicly. The operator-token requirement
  enforces this technically; the label enforces it socially.
