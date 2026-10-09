# API and Event Contracts

## General conventions

- JSON over HTTPS.
- OpenAPI 3.1 is the API source of truth.
- IDs are UUID strings internally; external IDs remain opaque strings.
- Stored times are UTC; existing list/timeline APIs return epoch seconds, while event envelopes use RFC 3339.
- All commands accept `Idempotency-Key`.
- All responses expose `trace_id`.
- Errors use stable machine-readable codes.
- Breaking changes require a new URL or event version.

## Authentication

- Human API: OIDC access token issued by Keycloak/enterprise IdP.
- Service API: short-lived client credentials with audience restrictions.
- Channel and connector webhooks: provider signature, timestamp and replay window.
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

Two codes are worth naming here because they separate conditions that used to
share one answer:

- `DATABASE_SATURATED` (503, `retryable: true`) - the connection pool is
  exhausted. The endpoint is fine and the service is busy; the pool drains on
  its own and the same request succeeds shortly after.
- `INTERNAL_ERROR` (500, `retryable: true`) - a genuine fault. Retrying
  reproduces it, and the `trace_id` is what support needs.

Both are retryable, because refusing the second would turn a bug into an
outage for clients that honour the flag. They are distinct codes so that a load
balancer, a backoff policy and an operator can tell a capacity problem from a
bug - a page of identical 500s cannot.

The Tool Gateway returns HTTP 409 `TASK_DEPENDENCY_BLOCKED` when a write
proposal is linked to a task whose prerequisite cannot be proven at execution
time. A confirmed proposal is rechecked against a fresh, verified read before
the executor runs; a false condition withdraws the proposal and records the
dependent task as skipped. An unavailable or unverified read leaves the write
unexecuted and the task blocked.

Task-linked proposals also capture the conversation lease version when the
proposal is prepared. `confirm` and a new `execute` re-check that the lease is
still active, human-owned, and at that version; otherwise the Gateway returns
HTTP 409 `TASK_LEASE_STALE` before recording a new execution. A pending task
proposal created before lease-version capture was deployed fails closed and
must be prepared again.

## Signed channel/connector webhooks

```text
POST /v1/webhooks/channels/{connector_id}
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
  "source": "web",
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
  "trigger_message_ref": "channel-message-reference",
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

Operator-requested retries create a new run and preserve the failed record:

```text
POST /v1/conversations/agent-runs/{run_id}/rerun
```

This command has no request body and requires `CASE_UPDATE`, a human operator
context, and `Idempotency-Key`. The key is stored only as a tenant-scoped hash.
Repeating the same request returns the same replacement run, including after it
has started or finished, and reports that run's current status. A different
request while a replacement is queued/running is refused; a new key is also
refused after a rerun has completed or handed the conversation to a human. The
command and its result are audited in the same transaction.

Response:

```json
{
  "run_id": "019...",
  "rerun_of": "019...",
  "status": "queued",
  "idempotent_replay": false,
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

The current operator entry point is the **conversation-first** workbench:

```text
GET  /v1/workbench/conversations?tab=queue|mine|waiting&q=&limit=&offset=
GET  /v1/workbench/conversations/{conversation_ref}
GET  /v1/workbench/conversations/{conversation_ref}/timeline?before={turn_id}
POST /v1/workbench/conversations/{conversation_ref}/actions
```

The action body contains `operation` (`claim|release|transfer|close`),
`expected_version` and an optional `target_ref`. It requires `CASE_UPDATE`
and `Idempotency-Key`. The actor is derived from the authenticated context;
version or ownership conflicts return HTTP 409. Queue items may have `case: null`.
The customer-visible reply still uses `POST /v1/conversations/{ref}/replies`,
which is idempotent for the same `(tenant, ref, key)`.

The workbench may also send `copilot_job_id` when a human reply was composed
from an AI draft. The server resolves source turn references from that
tenant-bound job and rechecks the current actor, lease version, and timeline
revision before it writes the customer-visible turn. Clients cannot submit or
forge `source_refs`; stale jobs return 409. The returned timeline only exposes
these provenance pointers to the authenticated workbench, not to the customer
support surface.

The older case-specific context endpoint remains for bookmarked cases:

```text
GET /v1/cases/{case_id}/workbench
```

Everything an agent needs to take over without asking the customer to repeat
themselves: the case, the conversation, the AI's last proposal with its
sources, related cases, the account's tier, and every contact bound to the
account (one company may reach us through several channel contacts).

```json
{
  "case": {},
  "conversation_ref": "019...",
  "account_tier": "enterprise",
  "account_contacts": [
    {"external_contact_id": "email-contact", "channel": "email"},
    {"external_contact_id": "wechat-contact", "channel": "wechat"}
  ],
  "conversation": [],
  "ai_suggestion": {"text": "...", "sources": []},
  "related_cases": {"items": [], "basis": "subject_terms_or_category"}
}
```

Read-only and assembled from what already exists, so it cannot drift from the
underlying rows. `related_cases.basis` is spelled out rather than left
implied - the query filters on category and recency, and an agent who believes
it is a relevance ranking will trust it more than it deserves.

## Contact memory erasure

```text
POST /v1/identity/contact-memory/erase
Headers: Idempotency-Key
Permission: TENANT_ADMIN
```

```json
{"external_contact_id": "provider-local-id", "channel": "chatwoot"}
```

The channel is required so equal provider-local IDs in different channels are
not treated as one person. The command deletes that tenant/channel contact's
durable `ContactFact` rows and redacted turns from linked conversations, and
records row counts in the append-only audit log. Conversation summaries are
computed per run and are not stored; support-path query embeddings bypass the
process cache, and retrieval result sets are never cached. Verified task state,
audit history, provider address mappings, and the external channel's raw
transcript remain outside this memory-erasure command.

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
GET  /v1/quality/metrics?window_seconds=86400
GET  /v1/quality/routes?window_seconds=86400
GET  /v1/quality/outcomes?window_seconds=2592000&confirmation_window_seconds=604800
POST /v1/quality/reviews/batches
GET  /v1/quality/reviews/batches/{batch_id}
POST /v1/quality/reviews/batches/{batch_id}/decisions
POST /v1/quality/reviews/batches/{batch_id}/finalize
POST /v1/quality/categories/state
```

Returns the dashboard numbers, plus the leak analysis:

- `handoff_reason_counts` - how many runs reached a person, and why.
- `automation_candidates` - those reasons ranked by volume, each with
  `automatable`. `true` means the gap is in the corpus and a document closes it;
  `false` means a control decided, and the honest response is capacity planning
  rather than automation. Each carries `sample_questions` taken from the gap
  queue, so the list is a work queue and not a histogram with opinions.
- `pending_corrections` - agent corrections awaiting review (see above).
- `GET /v1/quality/outcomes` - confirmation answers and mature no-response
  events after the customer-facing resolution question was actually displayed.
  Silence is never a positive confirmation. `same_issue_recontact_rate` stays
  null until a tenant-scoped same-contact/same-case follow-up can be verified.

`POST /categories/state` is the only write here, and it is the one that records
that something was *decided* about a category - which is why it demands an
`Idempotency-Key`. It is authorized under `AUDIT_READ` because reading the
dashboard and recording a decision on it are the same operator's job, but the
state machine it advances is deliberate: a first decision may not skip the work
(`observed` before `automated`), and two illegal transitions are refused with
`CATEGORY_STATE_INVALID` rather than normalised. A client that timed out and
retried would move that machine twice, so the retry needs the key.

The customer surface records exposure and explicit answers separately:

```text
POST /v1/support/resolution-feedback/requested
POST /v1/support/resolution-feedback   { "confirmed": true | false }
```

Both commands require the conversation's visitor token and an
`Idempotency-Key`; the server derives tenant, conversation, and an unambiguous
linked Case. Events contain only an enum, timestamps, and hashed request keys,
are append-only under tenant RLS, and are audited. A question is eligible only
after the conversation is closed and a human has replied.

Human review batches require `case.review` and idempotency keys on create,
decision, and finalize. Create accepts `window_seconds`, `size`, and an
optional tenant-owned `target_prompt_version_id`; when supplied, the sample
population contains only AgentRuns for that immutable prompt version. The
batch records code/policy/prompt version scope, per-stratum population, and the
sampler version. Decisions are one-per-selected-run, store only agree/override
plus a bounded reason code, and are append-only. Finalization is available
only after all selected items are reviewed; it writes an immutable hashed
snapshot with the weighted result and exact version scope. Prompt promotion
resolves this evidence server-side for the exact candidate version. Snapshot
schema v2 also binds bounded override-reason counts so release checks can
distinguish safety overrides from general quality disagreements.

## Prompt promotion release gate

```text
POST /v1/prompts/{version_id}/promote
```

Promotion requires `prompt.release`, an `Idempotency-Key`, a caller-supplied
CI evaluation summary (`eval_run_id`, category `scores`, and `regressions`),
and recent server-stored human-review evidence for the exact candidate
version. The human-review artifact is never accepted from the request body.
The release service reloads it under tenant RLS and rechecks its canonical
hash, target version, completeness, freshness, sample coverage, override
reasons, and thresholds before archiving the incumbent or activating the
candidate.

The initial code defaults require evidence finalized within 30 days; at least
30 reviewed cases, or a census when the measured population is smaller; at
least five reviewed cases per populated stratum, or a census of that stratum
when it contains fewer than five cases; zero `unsafe_action`,
`unsupported_claim`, or `citation_gap` overrides; and a population-weighted
override rate no greater than 10%. A failed condition returns HTTP 422 with a
stable `HUMAN_REVIEW_*` code and writes a `prompt.promotion_blocked` audit
event. Successful promotion audit data records the evidence ID/hash, target
version, sample counts, weighted rate, and threshold values. Emergency
rollback retains its existing path and does not require fresh evidence.

Unpublished prompt versions can enter enabled A/B experiments to collect
candidate-version runs before promotion. The server verifies each prompt
reference belongs to the tenant and caps the combined allocation of
unpublished versions at 10% across enabled experiments. Promotion remains the
only path to mark a version as serving.

The evaluation summary remains an operator/CI-supplied declaration; this
endpoint does not authenticate an external CI artifact. Signed CI provenance,
independent human holdout, and calibration on approved production traffic
remain production admission work.

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
POST /v1/tool-proposals/{id}/reconcile
POST /v1/tool-proposals/{id}/compensate
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

`confirm`, `execute`, and provider-outcome reconciliation recheck the actor's
current active tenant membership from PostgreSQL. The confirmation and execute
paths lock that membership row; a concurrent role change/removal either commits
first and is denied, or waits until the durable execution intent commits. If
the repeatable-read snapshot conflicts with a concurrent membership change, the
API returns HTTP 409 `AUTHORIZATION_STATE_CHANGED` and requires a fresh request.
Proposal and confirmation expiry are checked again after live preconditions,
immediately before the execution intent is committed; an approval expired at
that boundary cannot dispatch a provider call. Once the intent is committed,
the external request is an authorized in-flight operation and cannot be
recalled by a later role change.

An execution intent is committed before the provider call. If a process exits
after the provider may have accepted a write but before its receipt is stored,
the execution remains unresolved. A duplicate execute returns HTTP 409
TOOL_EXECUTION_IN_PROGRESS while the bounded provider window is active, then
TOOL_EXECUTION_OUTCOME_UNKNOWN; neither response dispatches another write.

An authorized current human owner can use reconcile after checking the provider
record. The request requires an Idempotency-Key and a compact evidence_reference
(provider record or lookup reference; no pasted document or free-form note).
The decision is applied, not_applied, or unresolved. Applied records a human
provider lookup as verified; not_applied closes the attempt as failed and
requires a new proposal and confirmation for any later write; unresolved keeps
the execution unknown and allows a later lookup under a new idempotency key.
Every decision is append-only and audited. Reconciliation never invokes the
executor. Provider-specific automatic lookup and provider-side idempotency
remain connector capabilities that must be qualified separately.

Compensation is business-specific and opt-in. Today the only qualified
compensator is for a verified `case.create`: an authorized human can request
`case.create.close_unmodified`, which closes (never deletes) the case only if
it remains `NEW` at version 1. The request requires `case.close`, a controlled
reason code, and an Idempotency-Key. A changed or missing case produces an
append-only failed-compensation record, audit event, and bounded Prometheus
counter/alert; the human must inspect the case before choosing another action.
Provider issue creation, CRM writes, notifications, and release confirmations
have no generic undo path. They remain at their explicit provider/no-return
point and use reconciliation or the owning system's human workflow.

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

## Outbound channel command

The internal command includes:

```json
{
  "command_id": "019...",
  "tenant_id": "019...",
  "conversation_ref": "019...",
  "expected_control_version": 12,
  "message": {
    "content": "...",
    "visibility": "customer",
    "citations": []
  }
}
```

Before dispatch, the worker performs a compare-and-set control check. The channel adapter supplies the provider conversation key, and `command_id` is the idempotency key for delivery.

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
