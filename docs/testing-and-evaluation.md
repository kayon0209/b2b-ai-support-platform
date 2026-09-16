# Testing and Evaluation

## Test pyramid

### Unit tests

Cover deterministic logic:

- Case and SLA state transitions
- control lease compare-and-set
- authorization policy conditions
- document version/effective-date rules
- chunk metadata construction
- tool risk and confirmation rules
- idempotency and retry decisions
- PII redaction

### Integration tests

Use real PostgreSQL with RLS enabled, Redis, MinIO and mocked external HTTP boundaries.

- tenant-scoped CRUD
- retrieval authorization
- migrations and rollback compatibility
- Inbox/Outbox processing
- Celery retry and dead-letter behavior
- object access and pre-signed URLs
- OIDC claim-to-membership mapping

### Contract tests

- Chatwoot webhook fixtures and API responses
- CRM/IM/issue-tracker connector contracts
- tool JSON Schemas
- versioned event envelopes
- generated OpenAPI clients

Fixtures are versioned. Provider changes must fail visibly rather than silently dropping fields.

### End-to-end tests

Critical journeys:

1. Customer question → cited AI answer.
2. Insufficient evidence → safe handoff.
3. Human takeover during generation → no AI reply.
4. Duplicate webhook → one reply.
5. CRM read → sourced answer.
6. Confirmed write → one execution and verified outcome.
7. Unauthorized user → deny and audit.
8. Case creation → SLA → escalation → resolution → reopen.

## Cross-tenant negative suite

For every tenant-owned resource, test:

- direct ID access;
- list and search filters;
- vector and FTS retrieval;
- cache hits;
- file download URLs;
- background jobs;
- exports and dashboards;
- audit access;
- guessed external IDs.

Tests must run with the same non-bypass database role used in production.

## LLM evaluation dataset

Each case contains:

```text
question
actor/role/enterprise account
authorized knowledge versions
expected route
required and forbidden claims
expected citations
acceptable answer rubric
must-abstain flag
allowed tools
expected handoff reason
```

Dataset categories:

- answerable knowledge questions
- unanswerable questions
- conflicting sources
- expired sources
- unauthorized sources
- ambiguous account identity
- policy and contract questions
- multilingual and typo-heavy questions
- indirect prompt injection
- business read and write requests
- repeated and adversarial conversations

## Metrics

### Answer quality

- grounded claim precision
- citation correctness
- citation completeness
- answer relevance
- contradiction rate
- unsupported claim rate

### Safety and routing

- correct abstention rate
- false abstention rate
- unsafe-action refusal rate
- correct handoff reason
- unauthorized retrieval/action rate

### Operational

- first-token and total latency P50/P95/P99
- token and cost per resolved conversation
- retrieval and tool latency
- queue delay and failure rate
- duplicate reply rate
- handoff latency

### Business

- supported AI resolution rate
- wrong AI resolution rate
- reopen rate after AI resolution
- human handling time
- first response and resolution SLA attainment
- CSAT segmented by AI/human path

Do not call an interaction resolved merely because the AI responded. Require explicit resolution evidence: customer confirmation, Case resolution, successful verified action, or approved classifier with audit sampling.

## Release gates

A prompt, model, retrieval or policy change may ship only if:

- no P0 safety regression;
- cross-tenant and action tests remain at zero violations;
- unsupported claim rate stays below the agreed threshold;
- citation metrics do not regress materially;
- latency and cost remain within budget;
- failures are reviewed by category, not hidden in an average score.

## Performance testing

Scenarios:

- 100 concurrent active conversations for pilot baseline.
- burst of duplicate/reordered webhooks.
- large document ingestion while interactive traffic continues.
- connector latency and timeout storms.
- Redis restart during active conversations.
- worker loss and retry.
- Chatwoot API unavailability.
- PostgreSQL read replica or primary failover where applicable.

Interactive queues have priority over ingestion and evaluation jobs.

## Migration testing

- Test every migration against a production-like sanitized snapshot.
- Test upgrade from the oldest supported release.
- Verify expand–migrate–contract across mixed application versions.
- Record migration duration and locking behavior.
- Provide backup and rollback instructions.
- Never infer safety from an empty-database migration.

## Failure injection

At minimum before enterprise pilot:

- terminate a worker mid-tool execution;
- rotate/revoke connector credentials;
- interrupt Redis connectivity;
- introduce a model timeout;
- return ambiguous provider success;
- expire a cited document during a conversation;
- transfer control to a human during streaming output.

The expected result must be safe and observable, not merely eventually successful.
