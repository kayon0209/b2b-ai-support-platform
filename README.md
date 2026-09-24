# B2B Enterprise AI Customer Support

A production-oriented implementation of an enterprise AI customer support
platform: a **FastAPI AI control plane** that owns tenancy, cases, knowledge,
agent runs, tool execution, citations, evaluation and audit, and that **hosts its
own customer channels** (`/support` for customers, `Workbench` for agents,
an ops console for supervisors).

Chatwoot was the original customer-service kernel. It was removed — see
[ADR 0012](docs/adr/0012-remove-chatwoot.md), which supersedes ADR 0001.

## Decision

- Own the customer channel. A channel is a `connectors` row plus an adapter in
  `platform_core/channels/`; adding one is not a change to the run path.
- Build knowledge, AI orchestration, case/SLA, enterprise identity, tool execution, audit, and evaluation as independently owned modules.
- Integrate through REST APIs, signed webhooks, and versioned events. Never read
  or write another system's database.
- Start as a modular monolith plus workers; split services only after measured bottlenecks.

## Document map

| Document | Purpose |
|---|---|
| [AGENTS.md](AGENTS.md) | Repository-level instructions for coding agents |
| [docs/agent.md](docs/agent.md) | Runtime AI agent behavior and safety contract |
| [docs/architecture.md](docs/architecture.md) | System architecture, boundaries, data flow, and scaling |
| [docs/development-plan.md](docs/development-plan.md) | Phased implementation roadmap and acceptance gates |
| [docs/domain-model.md](docs/domain-model.md) | Canonical entities, ownership, tenancy, and state machines |
| [docs/api-contracts.md](docs/api-contracts.md) | API, event, webhook, and idempotency contracts |
| [docs/security.md](docs/security.md) | Tenant isolation, authorization, privacy, audit, and threat model |
| [docs/testing-and-evaluation.md](docs/testing-and-evaluation.md) | Test pyramid, LLM evaluation, load and failure testing |
| [docs/deployment-and-operations.md](docs/deployment-and-operations.md) | Environments, deployment, observability, backup, and runbooks |
| [docs/integrations.md](docs/integrations.md) | CRM, IM, SSO, issue-tracker, and enterprise connector design |
| [docs/product-gap-analysis.md](docs/product-gap-analysis.md) | Target experience (three surfaces + one loop) vs. what is built |
| [docs/workbench-redesign-spec.md](docs/workbench-redesign-spec.md) | Current conversation-first workbench requirements and visual blueprint |
| [docs/workbench-redesign-acceptance.md](docs/workbench-redesign-acceptance.md) | UI, UX, security and production acceptance gates |
| [docs/workbench-redesign-implementation.md](docs/workbench-redesign-implementation.md) | Implementation map, operating conditions and remaining work |
| [docs/adr/](docs/adr/) | Architecture decision records, newest first |

## MVP scope

### Included

- Web Chat and email
- The platform's own customer window, agent workbench and ops console
- Tenant-aware knowledge ingestion and RAG
- Evidence citations and abstention
- Human handoff with control ownership
- Enterprise Case/Ticket and SLA
- OIDC SSO, RBAC, ABAC, PostgreSQL RLS
- Read-only CRM and issue-tracker integration
- Auditing, tracing, evaluation, dashboards

### Excluded until after pilot

- General multi-agent framework
- Browser/UI automation
- Voice agents
- Marketplace/plugin runtime
- More than two customer channels
- Fully autonomous high-risk writes
- Milvus/OpenSearch unless benchmarks justify them

## Target delivery

- Demonstrable MVP: 5–8 weeks
- Enterprise pilot: 12–16 weeks
- Production-oriented release: 20–28 weeks

All dates assume one primary developer using AI coding assistance and a tightly controlled scope.
