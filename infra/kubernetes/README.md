# Production deployment (Kubernetes)

`docs/development-plan.md` Phase 5 lists "High-availability production
templates". `infra/` held only `compose/`, which is a single-host development
stack: one API process, one of each worker, no disruption budget, no policy, no
way to roll a change without downtime. This directory is the production shape.

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

# 3. Everything else.
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

## What is verified, and what is not

Honest statement of the checks that were actually run, because a manifest
directory that looks validated and is not is worse than one that admits it:

| Check | Status |
|---|---|
| `kubectl kustomize infra/kubernetes` builds, 20 objects | **run** |
| Every HA property is asserted by `apps/api/tests/unit/test_kubernetes_manifests.py` (29 tests) | **run** |
| `kubectl apply --dry-run=client` | **not run** - it needs API discovery, so it cannot work without a cluster |
| Any of it applied to a real cluster | **not run** |

The unit test is the substitute for server-side validation, and it covers the
things a rollout would otherwise discover: a container missing a
`securityContext` under an enforced `restricted` profile, a credential written
as a literal value, a worker whose grace period is shorter than its poll
interval, an HPA on a poll loop.

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

**`/metrics` is not routed.** It is scraped in-cluster through a separate
Service. The endpoint carries no tenant-identifying labels, but publishing
operational detail to the internet has no upside.

**No CPU limit on the API, a memory limit on everything.** A CFS quota makes a
bursty request handler slower under load while it holds a connection; memory is
what actually needs bounding.

**The ingestion worker is one replica with no HPA.** It idles in a poll loop
between batches, so a CPU-based HPA would scale a backlog *down*. Scaling it
needs queue depth, which is a custom metric and a separate piece of work.

## What is still missing

- **A cluster to run it on.** These manifests have never been applied. Several
  cluster-specific facts - the ingress class, the storage class for Postgres,
  whether the CNI enforces NetworkPolicy at all - are configuration decisions,
  not omissions.
- **Postgres and Redis are referenced, not deployed.** Both are StatefulSet
  problems with backup, failover and upgrade questions of their own, and
  `docs/deployment-and-operations.md` already says the restores are what matter.
  `scripts/backup_restore_drill.py` is the executable half of that.
- **A ServiceMonitor rather than `prometheus.io/*` annotations** if the cluster
  runs the Prometheus Operator. The annotations are what the metrics Service
  carries today because they work with a plain scrape config.
