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

## Case command API

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

## Tool proposal and execution

```text
POST /v1/tool-proposals
POST /v1/tool-proposals/{id}/confirm
POST /v1/tool-proposals/{id}/execute
```

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
