# Deployment and Operations

## Environments

- `local`: Docker Compose, synthetic data.
- `test`: ephemeral CI environment with RLS and contract fixtures.
- `staging`: production-like topology and sanitized datasets.
- `production`: tenant workloads, controlled releases and backups.

Production credentials and customer data never enter local or CI environments.

## Local Compose services

```text
chatwoot-web
chatwoot-sidekiq
chatwoot-postgres
chatwoot-redis
ai-api
ai-worker-interactive
ai-worker-ingestion
ai-postgres
ai-redis
minio
keycloak
otel-collector
prometheus
grafana
loki
```

Chatwoot and the custom platform use separate PostgreSQL databases and Redis instances.

## Configuration

- Environment variables contain non-secret configuration.
- Secrets are loaded from a secret manager or orchestrator secret store.
- Configuration is validated at process startup.
- Unknown or missing security-critical configuration causes startup failure.
- Every deployment records a configuration hash.

## Release process

1. Merge after CI and evaluation gates.
2. Build immutable images with SBOM and provenance.
3. Deploy backward-compatible schema expansion.
4. Deploy application behind feature flags.
5. Run smoke and tenant-isolation checks.
6. Enable for internal tenant, then pilot tenant canary.
7. Monitor errors, latency, quality and cost.
8. Roll back application if needed.
9. Contract schema only after all old versions are gone.

## Database migrations

Use expand–migrate–contract:

- add nullable/backward-compatible fields;
- backfill in bounded batches;
- switch reads/writes under a feature flag;
- verify counts and checksums;
- remove obsolete structures in a later release.

Take a verified backup before high-risk migrations. Measure lock time in staging with production-like data.

## Observability

### Traces

Propagate one trace ID through:

```text
Chatwoot delivery
→ webhook ingestion
→ job
→ retrieval
→ model
→ tool
→ outbound Chatwoot command
```

### Metrics

- webhook rate, rejection and duplicate rate
- queue depth, age and retries by queue
- control lease conflict rate
- retrieval latency and candidate counts
- LLM latency, errors, tokens and cost
- tool proposal/execution/verification results
- Chatwoot API latency and errors
- citation coverage and abstention rate
- RLS/authorization denial counts without sensitive labels

### Alerts

P0:

- suspected cross-tenant access
- unauthorized write attempt that bypassed expected control
- audit pipeline failure
- backup failure
- sustained duplicate reply signal

P1:

- interactive queue age above threshold
- first-token P95 breach
- connector authentication failure
- indexing failures or stuck states
- Chatwoot API/WebSocket degradation

## Backup and recovery

- PostgreSQL: daily full plus continuous WAL/PITR where available.
- Object storage: versioning and lifecycle policy.
- Keycloak: database and realm configuration backup.
- Connector secrets: managed by the secret system's recovery process.
- Evaluation datasets and prompt versions: versioned and backed up.

Quarterly restore drills must demonstrate:

- target RPO and RTO;
- tenant data integrity;
- external mapping consistency;
- audit continuity;
- ability to resume jobs without duplicate actions.

## Queue design

Separate queues:

- `interactive`: customer-visible AI work.
- `tools`: external actions.
- `ingestion`: parsing/chunking/embedding.
- `sync`: connector synchronization.
- `evaluation`: offline evaluation.

Interactive and tool queues have reserved capacity. Retries use exponential backoff with jitter and bounded attempts. Exhausted work becomes a visible dead-letter item.

## Capacity and scaling

Scale independently:

- Chatwoot web by HTTP/WebSocket load.
- Chatwoot Sidekiq by native queue depth.
- AI API by request concurrency.
- Interactive workers by queue age and model concurrency limits.
- Ingestion workers by backlog and CPU/memory.

Do not autoscale solely on CPU; queue age and external rate limits are primary signals.

## Runbooks

### LLM provider unavailable

- open circuit after threshold;
- route to approved fallback if evaluation permits;
- otherwise hand off to human;
- never answer from memory without evidence.

### Retrieval unavailable

- stop enterprise-fact answers;
- preserve question and context;
- notify/hand off;
- alert on sustained failure.

### Chatwoot API unavailable

- retain outbound command;
- retry with command ID;
- check for existing external message before retry after ambiguity.

### Redis interruption

- fail safe on AI control acquisition;
- prevent uncoordinated AI replies;
- allow Chatwoot human support to continue;
- rebuild non-authoritative caches after recovery.

### Connector credential expired

- mark connector `NEEDS_REAUTH`;
- stop retries that cannot succeed;
- notify tenant administrators;
- do not silently use stale sensitive data.

## Production readiness checklist

- backup and restore drill passed
- migration rehearsal passed
- tenant isolation test passed
- failure injection passed
- rate limits configured
- secrets rotated and scoped
- PII logging review passed
- dashboards and alerts active
- on-call ownership and escalation documented
- feature flags and rollback verified
