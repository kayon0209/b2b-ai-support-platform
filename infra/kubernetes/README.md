# Production deployment (Kubernetes)

This directory defines the platform deployment topology, migration Job, API/Worker workloads, network policies, monitoring resources and scheduled backup/restore tasks. PostgreSQL, Redis, object storage, enterprise identity and model/business providers are external deployment dependencies. See [deployment guide](../../docs/deployment-and-operations.md) and [production conditions](../../docs/production-readiness.md).

Plain manifests plus a Kustomization - no Helm. A Helm chart would add
templating, a values schema and a release lifecycle for six Deployments that
differ by one environment variable each, and it would make `kubectl get` show
objects nobody wrote. `kubectl apply -k` and `kubectl set image` cover what is
needed here.

## Apply order

Ordering is enforced by this procedure, not by the manifests: `kubectl apply`
has no notion of "wait until the Job finished", and Kustomize's `dependsOn`
does not survive into the objects.

```bash
# 1. Create the real Secret from your secret manager. Do NOT apply
#    11-secret.example.yaml: it holds placeholders, and a Secret whose values
#    look configured fails on the first request rather than at startup.
kubectl apply -f 00-namespace.yaml

# 2. Schema before code. The Job has backoffLimit 0: a failed migration stops
#    the rollout rather than being retried into a half-applied schema.
kubectl apply -f 10-configmap.yaml -f 20-migrate-job.yaml
kubectl -n b2b-support wait --for=condition=complete job/platform-migrate --timeout=600s

# 3. Everything else, after Operator CRDs and all dependencies are available.
kubectl apply -k .

# 4. Rollouts are explicit. `latest` is not a tag you should ever see here.
kubectl -n b2b-support set image \
  deployment/platform-api platform=registry.example.com/b2b-support/platform:v1.2.3
```

Re-running the migration Job after it exists fails on an immutable field; it
carries `ttlSecondsAfterFinished: 86400`, so wait a day or delete it first:

```bash
kubectl -n b2b-support delete job/platform-migrate --ignore-not-found
```

## Validation scope

Repository tests assert manifest properties, and historical local records include Kustomize rendering. Rendering and unit assertions do not replace API-server admission, installed CRDs, enforced CNI policy, actual scheduling or rollout checks. No real-cluster deployment result is introduced by this documentation update.

Before rollout, replace example host/image/storage/identity values, create the real Secrets, install Prometheus Operator CRDs and supply reachable data services. Validate resources with the target API server and exercise the selected business scope. The default Ingress has no `/api` prefix rewrite; the frontend/API-base choice must follow the deployment guide.

## Decisions worth reading before editing

**The namespace enforces `restricted`.** A pod that does not satisfy it is
rejected at admission, so a manifest copied from an older project fails
immediately instead of running with more privilege than intended.

**`automountServiceAccountToken: false`.** No container here calls the
Kubernetes API. A mounted token would be a credential with no purpose, readable
by anything that got code execution in the pod.

**The API and workers connect as `platform_app`, the migration Job as the
owner.** The owner role is a superuser with `rolbypassrls`; if the API used it,
row-level security would stop being a defence and become documentation. The
workload DSNs live in `platform-secrets`; the migration-only owner DSN is kept
in a separate Secret so request debugging does not hand over schema authority.

**Ingress is default-deny, and egress excludes `169.254.0.0/16`.** The egress
rule allows 443 anywhere (the providers' address ranges change and pinning them
would break on their schedule), and the metadata-service exclusion is what stops
that rule from being "everything".

**Metrics require explicit public-route restrictions.** API and Worker targets use separate internal scrape Services. Worker metrics on port 8001 are admitted from the monitoring namespace. However the API metrics endpoint shares port 8000 with customer/API traffic, `APP_METRICS_ENABLED` is enabled in the sample ConfigMap, and the sample Ingress forwards the `/` prefix. Neither a separate Service nor an L3/L4 NetworkPolicy blocks `/metrics` by URL. The deployment gateway must deny public `/metrics`, `/docs`, `/redoc` and `/openapi.json` access while preserving internal scrapes; confirm this with external probes.

**No CPU limit on the API, a memory limit on everything.** A CFS quota makes a
bursty request handler slower under load while it holds a connection; memory is
what actually needs bounding.

**The ingestion worker is one replica with no HPA.** It idles in a poll loop
between batches, so a CPU-based HPA would scale a backlog *down*. Scaling it
needs queue depth, which is a custom metric and a separate piece of work.

## Cluster configuration requirements

- **Target cluster configuration.** Confirm ingress class, TLS, service discovery, CNI NetworkPolicy enforcement, availability budgets and actual resource limits. Repository-local tests do not prove these environment properties.
- **Postgres and Redis are referenced, not deployed.** Both are StatefulSet
  problems with backup, failover and upgrade questions of their own, and
  `docs/deployment-and-operations.md` already says the restores are what matter.
  `scripts/backup_restore_drill.py` is the executable half of that.
- **Prometheus Operator and receivers.** `70-alerts.yaml` and `71-servicemonitor.yaml` are already included in Kustomization. Install their CRDs before apply, confirm API/Worker targets are scraped, and verify alert delivery. Configure an OTLP collector for exported tracing.

Outbound mail deployments must also allow the configured SMTP port. The sample dependency egress rule permits external 443, not arbitrary SMTP ports; review it against the selected transport rather than assuming mail connectivity.
