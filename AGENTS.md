# Repository Instructions for Coding Agents

## Mission

Build a trustworthy B2B enterprise AI customer support platform. Optimize for tenant isolation, evidence-grounded answers, controlled tool execution, human handoff, auditability, rollback, and operational clarity.

## Non-negotiable architecture rules

1. Chatwoot is the customer-service kernel. Do not fork or rewrite its core unless an ADR explicitly approves the change.
2. Never access the Chatwoot database from custom services. Use documented REST APIs, signed webhooks, and versioned events.
3. The custom platform owns Tenant, EnterpriseAccount, Case, SLA, KnowledgeSpace, DocumentVersion, AgentRun, ToolExecution, Citation, Evaluation, and AuditEvent.
4. Every persisted business row must carry `tenant_id` unless it is explicitly global reference data.
5. PostgreSQL RLS must protect tenant-owned tables. Application filtering is additional defense, not a replacement.
6. Open-ended answers use retrieval and citations. Business mutations use deterministic flows and the Tool Gateway.
7. An LLM may propose a write action but cannot bypass authorization, confirmation, idempotency, or postcondition verification.
8. Human ownership overrides AI ownership immediately. The AI must re-check the control lease immediately before sending a customer-visible message.
9. Do not introduce a new infrastructure component without a benchmark, operational need, and ADR.
10. Do not log raw prompts, credentials, full documents, message attachments, or unredacted customer payloads.

## Default stack

- Python 3.12+, FastAPI, Pydantic, SQLAlchemy 2, Alembic
- PostgreSQL 16 with pgvector and RLS
- Redis and Celery for custom asynchronous work; separate Redis from Chatwoot
- MinIO/S3 for immutable document originals and attachments
- React, Vite, TypeScript for the enterprise admin UI
- Keycloak for OIDC; SAML and SCIM after pilot
- OpenTelemetry, Prometheus, Grafana, Loki, Sentry, Langfuse-compatible traces
- Docker Compose locally; Kubernetes only for production environments that need it

## Repository shape

```text
apps/
  api/                  # FastAPI modular monolith
  worker/               # ingestion, embedding, evaluation, connector jobs
  admin-web/            # React/Vite enterprise admin
packages/
  contracts/            # OpenAPI/event schemas and generated clients
  policy/               # shared authorization vocabulary, not business shortcuts
  observability/        # trace and logging helpers
infra/
  compose/
  kubernetes/
  migrations/
docs/
tests/
  contract/
  integration/
  e2e/
  evals/
```

## Module boundaries

- `identity`: tenants, memberships, roles, policies, external identities
- `support_bridge`: Chatwoot webhooks, API client, resource mappings
- `cases`: ticket lifecycle, assignment, escalation, SLA clocks
- `knowledge`: source, document, version, parsing, chunking, indexing, ACL
- `retrieval`: tenant filters, hybrid retrieval, reranking, citations
- `agent_runtime`: routing, context, generation, abstention, response validation
- `tool_gateway`: tool registry, authorization, confirmation, execution, verification
- `integrations`: CRM, IM, SSO, issue trackers, sync cursors
- `audit`: append-only security and business audit events
- `evaluation`: datasets, runs, metrics, release gates

Modules may call one another through declared application interfaces. Do not import another module's ORM models directly.

## Coding rules

- Use explicit types and Pydantic schemas at every external boundary.
- Prefer small deterministic functions around LLM calls.
- Store timestamps in UTC and render in tenant/user time zones.
- Use UUIDv7 or equivalent sortable UUIDs for internal identifiers.
- Monetary values use integer minor units plus ISO currency.
- Every inbound webhook and write command requires an idempotency key.
- Persist inbound events before enqueueing work.
- Use transactional outbox for business events.
- External calls require timeouts, bounded retries, circuit breakers, and structured error mapping.
- Schema changes must be backward-compatible for at least one deployment window.
- Long migrations must use expand–migrate–contract.

## Security checklist for every change

- Is `tenant_id` derived server-side?
- Does authorization check both actor and resource?
- Can the change expose another tenant through search, cache, logs, files, or metrics?
- Is sensitive data redacted before model and log boundaries?
- Does a write action require confirmation?
- Is the action idempotent and auditable?
- Is there a rollback or compensating action?

## Testing required before completion

- Unit tests for deterministic domain logic
- Integration tests with PostgreSQL RLS enabled
- Contract tests for Chatwoot and connector payloads
- Cross-tenant negative tests
- Idempotency and duplicate-webhook tests
- Human/AI race-condition tests
- Evaluation cases for answer correctness, citation support, abstention, and unsafe action refusal
- Migration test against a production-like data snapshot

## Definition of done

A feature is not complete until it has:

- API/schema contract
- authorization policy
- tenant isolation tests
- audit events
- metrics and traces
- failure and retry behavior
- rollback/feature flag where applicable
- user-facing acceptance test
- updated documentation

## Prohibited shortcuts

- Direct database joins across Chatwoot and custom databases
- Passing client-supplied `tenant_id` through without server-side resolution
- Treating vector similarity as calibrated confidence
- Marking an issue resolved because the model produced an answer
- Automatically learning from unreviewed conversations
- Executing refunds, deletions, permission changes, or irreversible actions without confirmation
- Sharing one Redis instance between Chatwoot and the custom platform
- Introducing Kafka, Milvus, OpenSearch, Temporal, or Kubernetes only for architectural appearance
