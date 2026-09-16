# B2B Enterprise AI Customer Support

A production-oriented implementation plan for an enterprise AI customer support system built with **Chatwoot as the customer-service kernel** and an independent **FastAPI AI control plane**.

## Decision

- Keep Chatwoot close to upstream and treat it as a bounded external subsystem.
- Build knowledge, AI orchestration, case/SLA, enterprise identity, tool execution, audit, and evaluation as independently owned modules.
- Integrate through REST APIs, signed webhooks, and versioned events. Never read or write Chatwoot tables directly.
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
| [docs/adr/0001-chatwoot-as-support-kernel.md](docs/adr/0001-chatwoot-as-support-kernel.md) | Architecture decision record for the selected foundation |

## MVP scope

### Included

- Web Chat and email
- Chatwoot inbox and human-agent workspace
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
