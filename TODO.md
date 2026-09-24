# TODO

Open work, ordered by the risk it removes. Each item names the evidence that
put it here, because a backlog entry without one is a wish.

## P0 — blocks a real deployment

- [ ] **Prometheus Operator is not part of this repository.** `70-alerts.yaml`
      and `71-servicemonitor.yaml` are Operator CRDs; applying them to a
      cluster without the Operator and its CRDs fails with `no matches for
      kind`. That is the intended behaviour — a cluster with the Operator but
      no rules looks exactly like a healthy deployment. Install the Operator,
      set `OTEL_EXPORTER_OTLP_ENDPOINT` in the ConfigMap for tracing to leave
      the process, and confirm the alerts reach a real receiver.
- [ ] **The pool benchmark needs connection headroom.**
      `test_larger_pool_is_not_faster_under_concurrency` deliberately opens 50
      connections at once. It cannot pass on a host where a long-running
      Compose stack already holds most of `max_connections` — measured here:
      62 of 100 in use, so the warm-up cannot complete. Its failure path also
      used to leak every connection it had opened, which left the database
      unusable for the rest of the session; that is fixed, but the test still
      needs a database with room, or a skip when the budget is obviously too
      small rather than a failure.
- [ ] **Set `APP_RATE_LIMIT_TRUSTED_PROXIES` in the deployment.** The code
      side is fixed — a trusted-proxy list plus a per-visitor bucket, verified
      by A/B load test (200 concurrent visitors: 14.4% rejected with one global
      bucket, 0% rejected and 340 split buckets with the proxy trusted) — but
      the ConfigMap ships the value empty, because the correct CIDR belongs to
      the cluster. A deployment that leaves it empty behind an ingress keeps the
      original defect. Fill it from the actual Ingress/load-balancer network
      and re-run the probe; `docs/deployment-and-operations.md` now says so.
- [ ] **The frontend has no deployment path.** The API mounts no static
      assets, neither the K8s manifests nor the Compose file carry a frontend
      workload, and `GET /support` answers 401. The product cannot ship its own
      interface. Planned approach: multi-stage Docker build and a FastAPI SPA
      mount, because that adds no new infrastructure component (AGENTS.md §9).
- [ ] **No alerting, and traces are not wired.** `/metrics` exposes 169
      series; the repository contains no `PrometheusRule`, `ServiceMonitor`,
      Grafana dashboard or Alertmanager config, and `requirements.txt` does not
      list `opentelemetry-*` — so `trace_id` reaches logs but not across
      processes. Minimum viable: alerts on queue age, abstention rate, tool
      failure rate and 429 ratio; one overview dashboard.

## P1 — correctness, resilience, isolation

- [ ] **Visitor tokens cannot be revoked.** No `jti`, no revocation table, no
      endpoint; a 12-hour TTL is the only limit, and re-opening a session with
      the same `visitor_id` returns the same token. A conversation cannot be
      closed in a way that actually locks the holder out.
- [ ] **Object storage has no lifecycle and no scheduled backup.** Retention
      purges rows and leaves the bytes; nothing creates the bucket, configures
      versioning or expires prefixes; `backup_restore_drill.py` is manual and
      no CronJob runs it.
- [ ] **Uploads are not scanned.** Content-type allowlist and size cap only —
      no magic-byte check, no malware scan — while `docs/security.md` requires
      one. Retrieval must stay closed to un-scanned documents.
- [ ] **Cross-tenant sweep coverage.** 51 tables carry `tenant_id`;
      `test_cross_tenant_negative.py` now names 28. Still unswept:
      `answer_corrections`, `billing_entries`, `case_escalations`,
      `connectors`, `contact_facts`, `dead_letter_items`, `departments`,
      `enterprise_accounts`, `enterprise_account_contacts`,
      `external_identities`, `feature_flags`, `feature_flag_targets`,
      `knowledge_aliases`, `knowledge_drafts`, `knowledge_gaps`,
      `membership_invitations`, `prompt_versions`, `saml_connections`,
      `saml_consumed_assertions`, `scim_tokens`, `sync_cursors`,
      `tenant_domains`. Add a test that fails when a new tenant-owned table is
      not named, so the gap cannot reopen silently.

## P2 — experience and scale

- [ ] Seat-side deep links: `Conversations.tsx` keeps the replay selection in
      component state and `Workbench.tsx` keeps queue tab, search and offset in
      memory, so a refresh loses the operator's place. `Cases.tsx` already
      writes them to the URL — apply the same pattern.
- [ ] `pages/Agents.tsx` is implemented and routed nowhere: no import in
      `main.tsx`, no sidebar entry, so nobody can see team load.
- [ ] No skeleton states anywhere; every load is a text placeholder, against
      8-second silent polling.
- [ ] `Workbench.tsx` modals declare `aria-modal` without Escape handling, a
      focus trap, initial focus or focus restoration.
- [ ] `AgentRun` has no version column, so terminal states are assigned
      directly and a late writer wins.
- [ ] No per-tenant rate limit (only the monthly quota and a global depth of
      500), so one tenant can fill the queue.
- [ ] Workers expose no metrics endpoint, so queue age and event outcomes —
      the signals that matter most — cannot be scraped.
- [ ] No dead-letter path for agent runs, and no manual replay entry.
- [ ] No Redis or database outage injection test, although the code degrades
      (rate limiting falls back to per-process buckets).
- [ ] No artifact lineage. `derived_from` / `lineage` / `parent_artifact` do
      not exist, so a run cannot be traced to the evidence and prompt versions
      that produced it, and knowledge-gap drafts cannot rejoin their
      conversation.
- [ ] Compliance export covers `audit` and `cases` only — documents,
      attachments, conversations, citations and runs are absent.
- [ ] `healthz` reports no dependency health and is not what
      `docs/deployment-and-operations.md` describes.
- [ ] `apps/api/tests/contract/` contains only `__init__.py`, and no test uses
      the `contract` marker the configuration declares.
- [ ] `docs/testing-and-evaluation.md` claims browser journeys and
      production-like load testing; both are manual scripts. `observability_metrics.py`
      cites a P1 alert that no document defines.
- [ ] No k6/locust. The only load evidence is a 100-concurrent insert test and
      `scripts/concurrency_probe.py`, both single-process.
