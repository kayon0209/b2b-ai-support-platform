# Development Plan

## Delivery assumptions

- One primary developer with AI coding assistance.
- Web Chat and email only through enterprise pilot.
- Chatwoot is deployed, not rewritten.
- The first pilot has a limited set of enterprise documents and 10–20 priority intents.
- High-risk write tools remain human-approved.

## Phase 0 — Foundation and due diligence

**Duration:** 1–2 weeks

### Outcomes

- Reproducible Chatwoot deployment.
- Legal and licensing decision.
- Frozen MVP scope and architecture decisions.
- Initial evaluation dataset and performance baseline.

### Tasks

- Review Chatwoot and all copied dependency licenses.
- Deploy Chatwoot locally with separate PostgreSQL and Redis.
- Exercise Web Widget, Inbox, agent reply, handoff and webhooks.
- Establish repository layout, CI, linting, secret scanning and dependency scanning.
- Define tenant, conversation, case, knowledge and tool vocabularies.
- Capture 50–100 representative support questions with expected evidence and outcomes.
- Benchmark 50–100 concurrent conversations.
- Write ADRs for Chatwoot boundary, modular monolith, identity and retrieval.

### Exit gate

- No unresolved license blocker.
- Clean rebuild from an empty machine.
- Chatwoot upgrade and rollback rehearsed once.
- P0 use cases and non-goals approved.

## Phase 1 — AI answer and handoff loop

**Duration:** 3–5 weeks

### Epics

1. Support Bridge and signed webhook ingestion.
2. Tenant and external resource mapping.
3. Document ingestion, parsing, versioning and indexing.
4. Tenant-filtered retrieval and citations.
5. Agent runtime with abstention.
6. AI/human control lease and handoff.
7. Tracing, metrics and audit baseline.

### Acceptance criteria

- Duplicate webhook delivery never creates duplicate customer replies.
- Every enterprise factual answer contains valid citations.
- Missing evidence causes abstention or handoff.
- Human takeover prevents AI output even during an in-flight generation.
- All runs identify prompt, model, retrieval and code versions.
- First-token P95 is below 2.5 seconds in the agreed pilot environment.

## Phase 2 — Enterprise identity, authorization, Case and SLA

**Duration:** 3–4 weeks

### Epics

- Tenant, EnterpriseAccount, Department and Membership.
- Keycloak OIDC authentication.
- RBAC roles and ABAC policy conditions.
- PostgreSQL RLS for all tenant-owned tables.
- KnowledgeSpace and DocumentVersion ACLs.
- Case/Ticket lifecycle separate from Conversation.
- SLA policy, clocks, pause, escalation, resolution and reopen.
- Immutable audit events and admin search.

### Acceptance criteria

- Cross-tenant read/write tests fail closed.
- Search and vector retrieval cannot return unauthorized chunks.
- Every Case has an owner, priority and SLA state.
- Admin and service-account actions are audited.
- Tenant resolution is server-side and cannot be overridden by request payload.

## Phase 3 — Enterprise integrations and tools

**Duration:** 3–4 weeks

### Priority

1. CRM read integration.
2. Jira or Linear ticket integration.
3. Enterprise IM notification or support channel.
4. Email identity/thread enhancements.
5. One controlled write workflow.

### Epics

- Connector SDK and lifecycle.
- Encrypted credential references and rotation.
- ExternalResourceRef and SyncCursor.
- Webhook verification and replay protection.
- Tool registry and JSON Schema validation.
- Authorization, confirmation and postcondition verification.
- Circuit breakers, bounded retries and dead-letter recovery.

### Acceptance criteria

- Connector outage does not block unrelated support functions.
- OAuth reauthorization is visible and actionable.
- Duplicate commands cannot duplicate external writes.
- Every write can be traced to actor, conversation, Case, approval and result.

## Phase 4 — Quality, dashboards and production hardening

**Duration:** 3–5 weeks

### Epics

- Evaluation runner and release gates.
- Quality dashboard: supported resolution, wrong resolution, abstention, handoff, citation coverage.
- Knowledge gap queue and reviewed knowledge-draft workflow.
- Prompt/model/configuration version release process.
- PII redaction and retention controls.
- Backpressure, priority queues and rate limits.
- Backup/restore, migration and failure drills.
- Feature flags, canary release and rollback.

### Release gates

- Cross-tenant leakage: 0 in the security suite.
- Unauthorized high-risk action: 0.
- Duplicate customer reply: 0 in duplicate/failure tests.
- Citation coverage for knowledge answers: ≥ 95%.
- Read-tool success excluding third-party outage: ≥ 99%.
- Prompt/model candidate does not regress any P0 evaluation category.

## Phase 5 — Productization

**Duration:** 4–8 weeks

- Tenant self-service administration.
- SAML and SCIM.
- Custom domains and branding.
- Knowledge approval workflow.
- Usage quotas and billing events.
- Additional IM channels.
- Retention and compliance controls.
- High-availability production templates.

## Milestone plan

| Milestone | Target | Demonstration |
|---|---:|---|
| M0 | Week 2 | Chatwoot + repository + baseline |
| M1 | Week 6 | Cited AI answer and reliable handoff |
| M2 | Week 10 | Tenant isolation, OIDC, Case and SLA |
| M3 | Week 14 | CRM/Jira tools with audit and confirmation |
| M4 | Week 18 | Evaluation gates and production drills |
| M5 | Week 24+ | Enterprise productization |

## Work prioritization

### P0

- Tenant isolation and authorization
- Human/AI ownership
- Evidence and abstention
- Tool authorization and idempotency
- Sensitive-data minimization
- Audit, migration and rollback
- Message reliability

### P1

- Case/SLA sophistication
- CRM and issue-tracker integrations
- AI quality dashboards
- Knowledge gap management
- Agent Copilot

### P2

- More channels
- Visual workflow builder
- Voice
- General plugin marketplace
- Autonomous learning
- General multi-agent orchestration

## First 20 implementation tickets

1. Bootstrap monorepo, CI and local Compose.
2. Create tenant and membership migrations with RLS.
3. Implement server-side tenant context middleware.
4. Add Chatwoot account/conversation/contact mappings.
5. Implement signed Chatwoot webhook endpoint.
6. Persist InboxEvent and deduplicate event IDs.
7. Add transactional job enqueue pattern.
8. Implement Chatwoot message API client with idempotency.
9. Implement ConversationControlLease.
10. Create KnowledgeSource, Document and DocumentVersion.
11. Upload originals to MinIO with tenant-prefixed keys.
12. Implement parsing job and explicit ingestion states.
13. Implement structure-aware chunking with section metadata.
14. Implement pgvector and FTS candidate retrieval.
15. Enforce knowledge ACL filters before retrieval.
16. Create AgentRun, Citation and PromptVersion.
17. Implement knowledge answer path and citation validator.
18. Implement abstention and human-handoff path.
19. Add OpenTelemetry traces and redacted JSON logs.
20. Add duplicate-webhook, cross-tenant and handoff-race E2E tests.
