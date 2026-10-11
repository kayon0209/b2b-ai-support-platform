# TODO

Open work, ordered by the risk it removes. Each item names the evidence that
put it here, because a backlog entry without one is a wish.

## P0 — blocks a real deployment

- [!] **Prometheus Operator is not part of this repository.** `70-alerts.yaml`
      and `71-servicemonitor.yaml` are Operator CRDs; applying them to a
      cluster without the Operator and its CRDs fails with `no matches for
      kind`. That is the intended behaviour — a cluster with the Operator but
      no rules looks exactly like a healthy deployment. Install the Operator,
      set `OTEL_EXPORTER_OTLP_ENDPOINT` in the ConfigMap for tracing to leave
      the process, and confirm the alerts reach a real receiver.
- [~] **The pool benchmark needs connection headroom.** **Code side done; the connection budget is a deployment setting.** The test now asks the server for its budget (`max_connections` minus what is already connected) *before* opening anything, and skips with an explanation when there is not room for a 50-connection pool plus a 15-connection margin. Verified both ways: with headroom it runs and passes, and with the budget forced low it reports `needs about 65 free connections ... and the server has 8` rather than failing on `too many clients already`.
      Catching the failure instead would not work: it cannot tell a full host from broken code, and reporting the second as the first is how a sizing test goes permanently red and is eventually deleted. The margin is deliberate — a benchmark that takes the last connection makes every *other* test fail.

- [~] **Set `APP_RATE_LIMIT_TRUSTED_PROXIES` in the deployment.** **Code side done; a helper now answers the question a deployment actually has.** `python scripts/report_trusted_proxies.py --namespace <ns>` prints the API pod addresses, the ingress controller's load balancer if it can read one, and the command that *verifies* the setting took effect (`redis-cli --scan --pattern 'ratelimit:api:addr:*' | wc -l` — one key means it did not). It changes nothing. With no reachable cluster it exits `2` and says so rather than guessing a value, because empty is the safe default and a wrong CIDR is either the availability bug or the security one.
      Remaining: run it against the real cluster and put the value in the ConfigMap.

- [x] **The frontend has no deployment path.** **Done (T3.2).** `infra/compose/api.Dockerfile` is a multi-stage build, `platform_core.spa` serves `/assets` with an SPA fallback, and `GET /support` answers 200 from the API image alone. The image now also passes `scripts/verify_image_build.py` and all seven artifact/runtime probes.

- [x] **No alerting, and traces are not wired.** **Code done (T3.3/TODO closeout).** API and worker Prometheus Services/ServiceMonitors are internal; worker queue age/outcome/process metrics and API/worker availability alerts exist. `OTEL_EXPORTER_OTLP_ENDPOINT` is wired with an in-process ring-buffer fallback. Installing the Operator and verifying a live receiver remain external cluster tasks tracked above.

- [x] **API image build and contents.** `python scripts/verify_image_build.py`
      built the image and passed all seven probes: PostgreSQL client binaries,
      operational scripts, frontend `dist/`, no runtime Node toolchain, and
      importable API source. This is a local image build; it was not pushed or
      deployed.

## P1 — correctness, resilience, isolation

- [x] **Object storage has no lifecycle and no scheduled backup.** **Done (T4.2).** Scheduled `pg_dump` plus S3 upload, a quarterly restore drill, retention that deletes object bytes, and orphan reconciliation.

- [x] **Uploads are not scanned.** **Done (T4.2).** Magic-byte type validation, a scan state machine, and an enforcement that an unscanned upload cannot enter retrieval.

- [x] **Cross-tenant sweep coverage.** 52 tables carry `tenant_id` and the
      sweep now names all 52 — measured against `information_schema`, not
      maintained by hand. The 24 added here are billing, customer records,
      connectors and cursors, feature flags, the knowledge authoring surface,
      identity, SSO/SCIM and visitor-token revocation.
      All 24 already had RLS policies and phase one's
      `0055_rls_empty_binding_guard` had already hardened them, so this batch
      added verification rather than protection — a smaller and different claim
      than the original gap implied.
      `test_the_table_list_matches_the_database` now fails when a table gains a
      `tenant_id` column, so the list cannot drift again silently. The sweep
      itself was checked by breaking `billing_entries` to `USING (true)`: it
      fails with `tenant B must see zero rows` and passes again once restored.

## P2 — experience and scale

- [x] Seat-side deep links: conversation selection and Workbench tab/search/sort
      were already URL-backed; conversation and Workbench offsets now round-trip
      through bounded, page-aligned `offset` parameters. Changing a filter and
      resetting its page occurs in one URL update. URL utility tests cover
      invalid, oversized and aligned offsets.
- [x] `pages/Agents.tsx` route and navigation. `main.tsx` registers
      `/admin/agents`, `Layout.tsx` links it, and the page reads the tenant
      roster and per-agent quality report.
- [x] Admin data pages and Workbench list/detail loading states use the shared
      accessible skeleton rows component; async action buttons retain their
      inline busy state.
- [x] Workbench dialogs use the shared `Dialog` component, which sets
      `aria-modal`, moves and traps focus, closes on Escape, and restores focus.
- [x] `AgentRun.version` and compare-and-set terminal claiming are implemented
      by migration `0060_agent_run_version` and `agent_runtime/terminal.py`.
- [~] Per-tenant API token buckets and separate Workbench/visitor/webhook
      budgets exist. A tenant-specific queue admission quota or fair scheduler
      below the global queue depth is still absent.
- [x] Worker metrics endpoint: each worker process serves its own Prometheus
      registry on a private listener. Poll-cycle duration/outcome, last success,
      queue-age and event metrics use bounded labels; Kubernetes exposes a
      separate internal metrics Service/ServiceMonitor and monitoring-only
      NetworkPolicy. A worker scrape-down alert is wired. Cluster scrape
      verification remains an external deployment step.
- [x] Failed AgentRun rerun: tenant-authorized human API and Conversation Replay
      confirmation now create a new queued run, preserve the failed record,
      audit the request, and persist only a SHA-256 idempotency-key hash. A late
      retry with the same key returns the same run even after it finishes;
      migration `0080_agent_run_rerun_idempotency` and isolated PostgreSQL
      integration coverage verify it.
- [x] Redis outage behavior is injected and verified to fall back to a bounded
      per-process limiter. Database outage injection now covers an
      authenticated Case read returning the sanitized error envelope,
      `/readyz` returning 503, and worker poll-loop backoff/recovery metrics.
- [~] Runs carry prompt/retrieval/model versions and citations; evaluation
      artifacts carry `derived_from`, and knowledge-gap drafts link to a
      conversation. A general parent-artifact graph across every derived
      business artifact is not implemented.
- [~] Compliance export currently covers `audit` and `cases` only. Extending it
      to document/attachment bytes and customer transcripts needs an explicit
      legal/product export scope, data minimization and object-delivery contract;
      metadata-only output would falsely claim a complete subject export.
- [x] Health semantics now distinguish liveness (`/healthz`) from database
      readiness (`/readyz`), with a sanitized 503 on dependency loss. Kubernetes
      readiness uses `/readyz`; docs explicitly state Redis is optional because
      rate limiting has a bounded fallback and connector checks are tenant APIs.
- [x] `apps/api/tests/contract/test_public_api_contract.py` is a marked,
      collected contract suite for served OpenAPI identity, unique operation
      IDs and core public routes. The broader documented-route/idempotency
      assertions remain in the unit suite.
- [~] Load evidence remains local/single-host: browser acceptance, 100-
      concurrent insert and worker-drain probes exist; the 100-concurrent
      webhook p95 target passed in the fresh isolated drill. No k6/Locust
      multi-host production-like suite or real-cluster capacity result exists.
