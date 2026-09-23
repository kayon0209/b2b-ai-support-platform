# API and Event Contracts

## General conventions

- JSON over HTTPS.
- OpenAPI 3.1 is the API source of truth.
- IDs are UUID strings internally; external IDs remain opaque strings.
- Times are RFC 3339 UTC.
- All commands accept `Idempotency-Key`.
- All responses expose `trace_id`.
- Errors use stable machine-readable codes.
- Breaking changes require a new URL or event version.

## Authentication

- Human API: OIDC access token issued by Keycloak/enterprise IdP.
- Service API: short-lived client credentials with audience restrictions.
- Chatwoot webhook: HMAC signature, timestamp and replay window.
- Connector webhook: provider-specific signature verification.

The API resolves `tenant_id` from authenticated membership, connector configuration or trusted resource mapping. It must not trust a client-provided tenant ID.

## Error envelope

```json
{
  "error": {
    "code": "KNOWLEDGE_EVIDENCE_INSUFFICIENT",
    "message": "The answer could not be verified from authorized knowledge.",
    "retryable": false,
    "details": {}
  },
  "trace_id": "019..."
}
```

Never expose stack traces, prompts, credentials or raw provider responses.

## Signed Chatwoot webhook endpoint

```text
POST /v1/webhooks/chatwoot
Headers:
  X-Signature
  X-Timestamp
  X-Delivery-Id
```

Processing rules:

1. Verify signature and timestamp.
2. Resolve connector and tenant from trusted configuration.
3. Store delivery and payload hash.
4. Return success for already processed delivery IDs.
5. Persist InboxEvent before enqueueing work.
6. Respond within 300 ms; never call an LLM synchronously.

## Canonical event envelope

```json
{
  "event_id": "019...",
  "event_type": "conversation.message.created",
  "event_version": 1,
  "tenant_id": "019...",
  "source": "chatwoot",
  "occurred_at": "2026-09-15T17:30:00Z",
  "resource": {
    "type": "message",
    "external_id": "98765"
  },
  "data": {},
  "trace_id": "019..."
}
```

Consumers provide at-least-once handling and deduplicate by `event_id`.

## Agent run API

```text
POST /v1/conversations/{conversation_ref}/agent-runs
```

Request:

```json
{
  "trigger_message_ref": "chatwoot:98765",
  "mode": "customer_reply",
  "expected_control_version": 12
}
```

Response:

```json
{
  "run_id": "019...",
  "status": "queued",
  "trace_id": "019..."
}
```

## Retrieval API

```text
POST /v1/retrieval/query
```

Request:

```json
{
  "query": "How is priority support defined?",
  "knowledge_space_ids": ["019..."],
  "principal": {
    "actor_id": "019...",
    "enterprise_account_id": "019..."
  },
  "top_k": 8,
  "deadline_ms": 1200
}
```

`principal` is checked against authenticated context; callers cannot grant themselves additional scope.

Response contains authorized candidates only:

```json
{
  "results": [
    {
      "chunk_id": "019...",
      "document_version_id": "019...",
      "title": "Support Policy",
      "section_path": ["Enterprise", "Priority Support"],
      "excerpt": "...",
      "source_uri": "...",
      "ranking": {
        "lexical": 0.7,
        "vector": 0.8,
        "reranker": 0.92
      }
    }
  ],
  "trace_id": "019..."
}
```

Scores are diagnostics and must not be represented to end users as probabilities.

## Case API

```text
POST /v1/cases
POST /v1/cases/{case_id}/commands
GET  /v1/cases
GET  /v1/cases/{case_id}
```

```json
{
  "subject": "EQ 12345: confirm stackup before production",
  "description": "",
  "priority": "p2",
  "category": "eq_confirmation",
  "enterprise_account_id": "019...",
  "conversation_ref_id": "019..."
}
```

`enterprise_account_id` selects the SLA policy through the account's contract
tier. An unknown id and another tenant's id both return `ACCOUNT_NOT_FOUND` —
RLS cannot see the latter, and distinguishing them would make this endpoint a
way to enumerate account ids.

`conversation_ref_id` records where the case came from, on
`CaseConversation`. It is optional because a case can be raised from a phone
call or an email, and it matters beyond provenance: it is what
`inbox_consumer.case_conversation_ref` joins on to find the conversation of an
**escalated** case, which is what priority claiming filters on. A case created
without it is not reachable that way.

## Case workbench

```text
GET /v1/cases/{case_id}/workbench
```

Everything an agent needs to take over without asking the customer to repeat
themselves: the case, the conversation, the AI's last proposal with its
sources, related cases, the account's tier, and every contact bound to the
account (one company reaches us through several channels, and each is a
different Chatwoot contact).

```json
{
  "case": {},
  "account_tier": "enterprise",
  "account_contacts": [
    {"external_contact_id": "email-contact", "channel": "email"},
    {"external_contact_id": "wechat-contact", "channel": "wechat"}
  ],
  "conversation": {"items": []},
  "ai_suggestion": {"text": null, "citations": [], "abstain_reason": null},
  "related_cases": {"items": [], "basis": "same_category"}
}
```

Read-only and assembled from what already exists, so it cannot drift from the
underlying rows. `related_cases.basis` is spelled out rather than left
implied - the query filters on category and recency, and an agent who believes
it is a relevance ranking will trust it more than it deserves.

## Answer corrections

```text
POST /v1/corrections
GET  /v1/corrections?status=pending
POST /v1/corrections/{id}/review
```

An agent records that an answer was wrong, and what it should have been; a
reviewer approves or dismisses it.

```json
{
  "agent_run_id": "019...",
  "question": "标准交期是几天？",
  "correct_answer": "标准交期 7 天，加急 3 天，以报价单为准。",
  "note": "AI 说了 5 天，实际是 7 天。"
}
```

Recording needs `case.update` - the people who see bad answers are agents, and
gating it behind an admin action would leave corrections in a chat message
where they are lost. Reviewing needs `knowledge.publish`, because approving
says "this may become what the platform tells customers".

Nothing here learns automatically. Approving an `AGENT_CORRECTION` opens a gap
and drafts the corrected answer - a draft, not knowledge: approving checked
that the answer is right, not that it reads well as documentation. Publishing
stays its own reviewed act.

## Quality metrics

```text
GET /v1/quality/metrics?window_seconds=86400
GET /v1/quality/routes?window_seconds=86400
```

Returns the dashboard numbers, plus the leak analysis:

- `handoff_reason_counts` - how many runs reached a person, and why.
- `automation_candidates` - those reasons ranked by volume, each with
  `automatable`. `true` means the gap is in the corpus and a document closes it;
  `false` means a control decided, and the honest response is capacity planning
  rather than automation. Each carries `sample_questions` taken from the gap
  queue, so the list is a work queue and not a histogram with opinions.
- `pending_corrections` - agent corrections awaiting review (see above).

## Case evidence attachments

```text
POST /v1/cases/{case_id}/attachments   multipart: file, uploaded_by?
GET  /v1/cases/{case_id}/attachments   list, each with a pre-signed download URL
```

```json
{
  "attachment": {
    "attachment_id": "019...",
    "filename": "board-rev-c.jpg",
    "content_type": "image/jpeg",
    "size_bytes": 184320,
    "uploaded_by": "eng-42",
    "created_at": 1789812000,
    "url": null
  }
}
```

The bytes live in the object store; the row holds the reference, the display
name, the accepted content type and the size. **Upload is multipart and read is
pre-signed**, which is not a stylistic split: routing the bytes through the API
is what lets the content type and the 25 MiB cap be enforced *before* anything
is stored, whereas a pre-signed PUT lets a client write arbitrary bytes and only
then have the API discover they are not allowed, after the object exists.
Reading is the opposite problem - a pre-signed GET gives a reviewer a
short-lived URL without handing out a credential.

`url` is **null on the upload response** and signed only on read. A link minted
at upload time would outlive the request that asked for it, and one long enough
to survive a review is one long enough to leak.

Attaching requires `CASE_UPDATE` and an `Idempotency-Key` (a retried upload is a
second copy of the evidence); listing requires `CASE_READ`. A case in another
tenant answers `CASE_NOT_FOUND` - RLS makes it invisible, and the same answer
covers "does not exist", so this cannot be used to enumerate case ids.

The accepted types are this endpoint's own list, not the knowledge corpus's:
knowledge documents are parsed and indexed, so images are useless there, while a
complaint's evidence is frequently a board photograph or a fabrication archive.
Refusals are `EMPTY_ATTACHMENT`, `ATTACHMENT_TOO_LARGE` and
`UNSUPPORTED_ATTACHMENT_TYPE`.

## Case commands

```text
POST /v1/cases/{case_id}/commands
```

```json
{
  "command": "change_priority",
  "expected_version": 7,
  "parameters": {"priority": "p1"},
  "reason": "Production service unavailable"
}
```

Optimistic concurrency prevents lost updates. Invalid state transitions return `CASE_TRANSITION_NOT_ALLOWED`.

## Tool catalog

```text
GET /v1/tools
```

```json
{
  "items": [
    {
      "name": "case.eq_confirm",
      "version": 1,
      "risk": "confirmed_write",
      "requires_confirmation": true,
      "input_schema": {"type": "object", "properties": {"case_ref": {"type": "string"}}, "required": ["case_ref"]},
      "tenant_scoped": false
    }
  ],
  "total": 1
}
```

The tools this tenant may propose against, read from `tool_definitions` — the
tenant's own rows plus the shared ones, highest version first, deduplicated by
name so the entry returned is the one the propose path would resolve.

A tenant whose catalog was never registered sees an empty list rather than the
code catalog, which is intended: a tenant may edit or disable a definition, and
repairing that silently is worse than an empty form. Seed with
`tool_gateway.registry.ensure_tool_definitions`, which only adds missing rows.

A tool the caller cannot propose is **omitted**, not listed-and-refused: a choice
the API always rejects is not a choice. Two things disqualify one — the
`prohibited` class, and a risk class whose action this principal does not hold.
The second matters more than it looks, because the write grants are narrow:
`support_agent` holds `CASE_READ`, `CASE_CREATE`, `CASE_UPDATE`, `KNOWLEDGE_READ`
and `TOOL_READ`, and **no write action at all**, so a support agent's catalog is
read tools only. Approving is separate from proposing, and a support agent does
hold `CASE_UPDATE` — which is what lets them approve a `confirmed_write`
proposal the agent raised without being able to raise one.

## Tool proposal and execution

```text
POST /v1/tool-proposals
POST /v1/tool-proposals/{id}/confirm
POST /v1/tool-proposals/{id}/execute
GET  /v1/tool-proposals
GET  /v1/tool-proposals/{id}
```

`GET /v1/tool-proposals` lists the tenant's proposals, newest first, with
`limit`/`offset` and an optional `status` filter, and requires `case.read` —
listing exposes the arguments of writes in flight, which is case content.
Each item carries both `status` (the stored value) and `effective_status`: a
proposal still `authorized` past its expiry is reported as `expired`, because
`confirm` and `execute` both refuse it, and a console that showed it as pending
would offer an approval that cannot be given.

The list is what makes the agent's write path usable. The agent can propose a
`confirmed_write` and then stop — it holds `tool.write.confirmed` so it can
propose, and not `case.update`, so it cannot approve — and a human discovers the
proposal here rather than being told its id out of band.

A proposal freezes:

- tool name and version;
- sanitized arguments;
- action hash;
- actor and tenant;
- permission decision;
- required confirmation scope;
- expiry time.

Execution requires the same action hash. Any material argument change invalidates confirmation.

## Tenant branding

```text
GET /v1/tenant/branding
PUT /v1/tenant/branding
```

```json
{
  "display_name": "Acme Support",
  "logo_url": "https://cdn.example.com/logo.png",
  "primary_color": "#1a2b3c",
  "support_email": "help@acme.example"
}
```

Both routes act on the caller's server-resolved tenant; there is no route that
takes a tenant id from the client. Reading needs only an authenticated member,
because branding is public-facing. Writing needs `tenant.admin` and an
`Idempotency-Key`, and is audited. `PUT` replaces the whole object: an omitted
field is cleared. `primary_color` must be a hex colour and `logo_url` must be
http(s), because the admin UI renders both.

## Tenant usage and quota

```text
GET  /v1/tenant/usage
PUT  /v1/tenant/quota
GET  /v1/tenant/billing
POST /v1/tenant/billing/adjustments
```

```json
{
  "usage": {
    "period_start": 1780000000,
    "period_end": 1782592000,
    "runs_used": 42,
    "prompt_tokens": 128000,
    "completion_tokens": 9000,
    "quota": 100,
    "remaining": 58,
    "over_quota": false
  }
}
```

Usage is per calendar month (UTC). `quota: null` means unlimited. Queuing an
agent run (`POST /v1/conversations/{ref}/agent-runs`) returns
`429 QUOTA_EXCEEDED` once the quota is exhausted, so a caller can tell
"declined for capacity" from "no supporting evidence" — both otherwise look
like "no answer". Setting the quota requires `tenant.admin` and an
`Idempotency-Key`, and is audited.

Completing a run emits the outbound event `usage.recorded`
(`aggregate_type: agent_run`) with the run id, route, status and token
counts, written in the same transaction as the run's final state.

### Billing ledger

`GET /v1/tenant/billing` returns the month's ledger rollup, which is what an
invoice is computed from. It is fed by the same `usage.recorded` event as the
usage snapshot, but keyed on the event id so an outbox redelivery collapses
instead of double-billing. Requires `audit.read`: commercial totals are not
part of the support role.

```json
{
  "billing": {
    "period_start": 1780000000,
    "period_end": 1782592000,
    "entries": 43,
    "usage_entries": 42,
    "adjustment_entries": 1,
    "prompt_tokens": 127800,
    "completion_tokens": 9000,
    "total_tokens": 136800
  }
}
```

The ledger is **append-only** — the application role holds `SELECT` and
`INSERT` and no `UPDATE` or `DELETE` — so a correction is a new row, never an
edit, and shows up as an `adjustment`:

```text
POST /v1/tenant/billing/adjustments
Idempotency-Key: <uuid>

{ "run_id": "...", "prompt_tokens_delta": -200,
  "completion_tokens_delta": 0, "reason": "duplicate run" }
```

A negative delta credits, a positive one charges. Adjustments subtract in the
rollup, and the total floors at zero so an over-applied correction is a
visible data problem rather than negative consumption. Requires
`billing.adjust`, held by `tenant_owner` only: crediting an account is a
financial statement about a customer, not an administrative convenience.

The `Idempotency-Key` is what makes a retry safe. A redelivery returns
`{"duplicate": true}` and changes nothing — no second row and no second audit
event — so a client that retried after a timeout cannot credit an account
twice. An empty adjustment (both deltas zero) is refused with
`400 ADJUSTMENT_EMPTY`.

## Outbound Chatwoot command

The internal command includes:

```json
{
  "command_id": "019...",
  "tenant_id": "019...",
  "conversation_external_id": "12345",
  "expected_control_version": 12,
  "message": {
    "content": "...",
    "private": false,
    "citations": []
  }
}
```

Before API dispatch, the worker performs a compare-and-set control check. `command_id` is the idempotency key and is stored with the returned external message ID.

## Connector interface

All connectors implement:

```text
health_check()
authorize()
refresh_credentials()
fetch(resource, cursor?)
search(query, filters)
execute(command, idempotency_key)
verify_postcondition(execution)
handle_webhook(headers, body)
```

Connector-specific payloads stay inside the adapter. Domain modules consume canonical models.
