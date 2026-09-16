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
