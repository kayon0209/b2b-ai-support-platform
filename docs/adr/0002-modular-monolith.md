# ADR 0002: Modular Monolith with an Async Worker

- Status: Accepted
- Date: 2026-09-17

## Context

The platform spans identity, support bridge, cases, knowledge, retrieval, agent
runtime, tool gateway, integrations, audit and evaluation. These are genuinely
different concerns with different failure modes and different rates of change.

The question is whether to deploy them as one process, several services, or a
mesh of event-driven services, and how asynchronous work is owned.

Constraints that shaped the decision:

- One primary developer with AI assistance (see `docs/development-plan.md`).
- A pilot with 10–20 priority intents and a limited document set.
- `AGENTS.md` forbids "introducing a new infrastructure component without a
  benchmark, operational need, and ADR".
- Tenant isolation, RLS and audit are load-bearing and must not be re-implemented
  per service — a duplicated RLS policy is a duplicated place to get it wrong.

## Decision

Build a **modular monolith**: one FastAPI application (`apps/api`) composed of
modules that may only call each other through declared application interfaces,
plus **one worker image** (`apps/worker`) that runs asynchronous roles selected
at deployment time by `APP_WORKER_QUEUE` (`interactive`, `ingestion`, `outbox`).

Modules live under `apps/api/src/platform_core/<module>/` and are:

`identity`, `support_bridge`, `cases`, `knowledge`, `retrieval`,
`agent_runtime`, `tool_gateway`, `integrations`, `audit`, `evaluation`, `llm`.

Enforced boundaries:

- **No module imports another module's ORM models directly.** Cross-module reads
  go through that module's service functions. This is what keeps the modules
  separable later, and it is checkable in review.
- **One database, one schema, one RLS policy set.** Every tenant-owned table
  carries `tenant_id` and is protected by the same `USING (tenant_id::text =
  current_setting('app.tenant_id', true))` predicate, installed with
  `FORCE ROW LEVEL SECURITY`.
- **Asynchronous work is claimed from PostgreSQL tables**, not from a broker
  queue. `inbox_events`, `outbox_events` and `document_versions` are queues, and
  workers claim with `FOR UPDATE SKIP LOCKED`.
- **Chatwoot is not part of this process.** It is a separate deployment reached
  only through its REST API and signed webhooks (ADR 0001).

## Why a monolith and not services

- **Tenant isolation is a cross-cutting invariant.** With separate services, the
  RLS policy, the tenant-resolution path and the audit writer each get
  reimplemented per service, and the security property becomes "as strong as the
  weakest service". In one process there is exactly one implementation, and
  `test_cross_tenant_negative.py` exercises it once.
- **Deterministic work is most of the work.** Ingestion, chunking, SLA clocks
  and the outbox relay are ordinary transactional code. Splitting them out buys
  no isolation that a module boundary does not already buy.
- **The transaction boundary is real.** Persisting an inbound webhook and
  enqueueing its work must be atomic (see ADR-adjacent note in
  `support_bridge`). Separate services turn a local transaction into a
  distributed one and force an outbox plus a relay plus reconciliation — three
  new failure modes to buy modularity we already have.
- **Deployment cost is not zero.** Every additional service costs a Dockerfile,
  a health check, a migration owner, a config surface and a place for secrets to
  drift. At this team size that cost is paid in the currency we have least of.

## Why a worker image rather than in-process background tasks

Running work inside the API process was rejected:

- A long ingestion batch would compete with request handling for the event loop
  and for database connections.
- Scaling the API would silently multiply worker concurrency, changing the
  ingestion rate as a side effect of scaling for traffic.
- A crash-restart would take in-flight work down with the request path.

The worker image shares the same `platform_core` code and the same migrations,
so there is no second model of the domain — only a second entry point.

Roles are selected by environment rather than by building three images, so the
deployment topology (how many of which role) is a runtime decision, not a build
artifact. Roles are `interactive` (inbox → agent run), `ingestion`
(parse/chunk/embed/index) and `outbox` (business-event relay).

## Why PostgreSQL as the queue

- The work is already transactional with the data it touches. An inbound event
  and its queue row commit together, so there is no window in which one exists
  without the other.
- `FOR UPDATE SKIP LOCKED` gives correct multi-worker claiming without a broker.
- Rate is bounded and known: the bulk ingestion queue is explicitly the lowest
  priority consumer, and `AGENTS.md` already commits to bounded retries.

The cost is paid in throughput ceiling and in poll load. Both are acceptable at
pilot scale and both are measured rather than assumed — the claim query is
covered by a partial index restricted to claimable states
(`ix_docversion_claimable`), so the scan is O(claimable), not O(rows).

`APP_REDIS_URL` is used by exactly one thing: **rate-limit counters**
(`rate_limit.py`). That distinction is the whole point and worth stating.

Redis is present for cache-shaped and ephemeral state, never as the durable
queue. A rate-limit bucket is the archetypal example of what it is for: a token
count that is worthless after a restart, tolerates loss, and must be shared
across replicas so the limit is a property of the tenant's entitlement rather
than of how many pods happen to be running. Losing those counters costs one
window of protection and nothing else.

That is the opposite of the queue case above. Putting *work* in Redis would
reintroduce exactly the atomicity problem this ADR rejects: a message could be
delivered without the database change that was supposed to accompany it. So the
rule is not "Redis is banned" but "Redis never holds state whose loss changes
the outcome of a request" — and a durable queue would require its own ADR.

When Redis is unreachable, the limiter falls back to per-process buckets rather
than failing open (see `rate_limit.py` for why failing open is the wrong
direction).

## Consequences

### Positive

- One implementation of tenant isolation, audit and policy — the security
  property is testable once and holds everywhere.
- Local transactions across module boundaries; the outbox is used for *events*,
  not to paper over a missing transaction.
- One migration chain, one schema, one deployable pair (api + worker).
- Module boundaries are still explicit, so a later extraction is a refactor, not
  a rewrite.

### Negative

- All modules scale together; a hot retrieval path cannot be scaled alone.
- A defect in a shared dependency is a defect everywhere at once.
- The packaging discipline (`packages/contracts`, `packages/policy`) must be
  maintained by review, since nothing at runtime prevents a bad import.

### Neutral

- The worker image is a second deployment to operate, but it reuses the API's
  code, schema and configuration.

## Constraints

- No new infrastructure component (broker, workflow engine, search cluster)
  without a benchmark, an operational need, and an ADR.
- No direct cross-module ORM imports.
- No second Redis shared with Chatwoot.
- Every queue table is claimed with `FOR UPDATE SKIP LOCKED` under a bounded
  batch, never an unbounded scan.
- Workers must be idempotent: a claim can be reclaimed after a crash, so a
  re-run has to converge rather than duplicate.

## Revisit criteria

Revisit if any of the following becomes measurable:

- A single module's resource profile dominates and forces over-provisioning of
  the rest (the classic driver for extraction).
- The worker and API need materially different scaling signals for a sustained
  period.
- Polling load on PostgreSQL becomes a meaningful share of database CPU at pilot
  scale despite the partial indexes.
- A team boundary appears such that independent deploy cadence for one module
  would meaningfully reduce coordination.
