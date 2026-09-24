"""Unit tests: the Kubernetes manifests actually declare the HA properties.

Why this is a test rather than a comment
----------------------------------------
Every property asserted here is one a reader would assume from the file and
would not notice going missing. A Deployment with no `PodDisruptionBudget`
still deploys; a container without `readOnlyRootFilesystem` still runs; an
`APP_SECRET_KEY` written as a literal `value:` instead of a `secretKeyRef`
works perfectly until someone reads the manifest. None of those break a build,
and all of them are the difference between "runs" and "can be run in
production".

`kubectl apply --dry-run=client` cannot check any of it either - it needs API
discovery, so it fails on a machine with no cluster (which is where this test
runs). So the manifests are parsed directly and the claims are asserted.
"""

from __future__ import annotations

import pathlib
from typing import Any

import pytest
import yaml

REPO_ROOT = pathlib.Path(__file__).resolve().parents[4]
K8S = REPO_ROOT / "infra" / "kubernetes"

WORKER_QUEUES = {
    "interactive": "interactive",
    "ingestion": "ingestion",
    "retention": "retention",
    "sla": "sla",
}


def _documents() -> list[dict[str, Any]]:
    docs: list[dict[str, Any]] = []
    for path in sorted(K8S.glob("*.yaml")):
        # `kustomization.yaml` is a build input, not a Kubernetes object, and
        # `*.example.yaml` holds placeholders that are deliberately not applied.
        if path.name.endswith(".example.yaml") or path.name == "kustomization.yaml":
            continue
        for doc in yaml.safe_load_all(path.read_text(encoding="utf-8")):
            if doc:
                docs.append(doc)
    return docs


def _by_kind(kind: str) -> list[dict[str, Any]]:
    return [d for d in _documents() if d.get("kind") == kind]


def _named(kind: str, name: str) -> dict[str, Any]:
    matches = [d for d in _by_kind(kind) if d["metadata"]["name"] == name]
    assert matches, f"no {kind} named {name}"
    return matches[0]


def _app_containers(pod_spec: dict[str, Any]) -> list[dict[str, Any]]:
    """Containers of a pod spec (i.e. `template.spec`, not the whole object)."""
    return list(pod_spec["containers"])


def _all_pod_specs() -> list[tuple[str, dict[str, Any]]]:
    out: list[tuple[str, dict[str, Any]]] = []
    for kind in ("Deployment", "Job"):
        for doc in _by_kind(kind):
            out.append((f"{kind}/{doc['metadata']['name']}", doc["spec"]["template"]["spec"]))
    return out


# --- the files parse at all -------------------------------------------------


def test_every_manifest_document_parses_and_is_namespaced() -> None:
    docs = _documents()
    assert docs, "no manifests found"
    for doc in docs:
        assert doc.get("apiVersion") and doc.get("kind")
        assert doc["metadata"].get("name")
        if doc["kind"] != "Namespace":
            assert doc["metadata"].get("namespace") == "b2b-support", doc["kind"]


def test_the_secret_template_is_not_applied() -> None:
    """It holds placeholders. Applying it would create a Secret whose values
    look configured and would fail on the first request instead of at startup."""
    kustomization = (K8S / "kustomization.yaml").read_text(encoding="utf-8")
    assert "11-secret.example.yaml" not in kustomization.replace(
        "# `11-secret.example.yaml` is intentionally absent", ""
    )


# --- pod security -----------------------------------------------------------


def test_every_container_runs_restricted() -> None:
    """The namespace enforces the `restricted` Pod Security Standard, so a
    container without these is rejected at admission - better to find out here
    than during a rollout."""
    for name, spec in _all_pod_specs():
        pod_sc = spec.get("securityContext", {})
        assert pod_sc.get("runAsNonRoot") is True, name
        assert pod_sc.get("seccompProfile", {}).get("type") == "RuntimeDefault", name
        for container in _app_containers(spec):
            sc = container.get("securityContext", {})
            assert sc.get("allowPrivilegeEscalation") is False, (name, container["name"])
            assert sc.get("readOnlyRootFilesystem") is True, (name, container["name"])
            assert sc.get("capabilities", {}).get("drop") == ["ALL"], (name, container["name"])


def test_the_namespace_enforces_the_restricted_profile() -> None:
    namespace = _by_kind("Namespace")[0]
    labels = namespace["metadata"]["labels"]
    assert labels["pod-security.kubernetes.io/enforce"] == "restricted"


def test_the_service_account_token_is_not_mounted() -> None:
    """Nothing here calls the Kubernetes API, so a mounted token is a
    credential with no purpose."""
    account = _by_kind("ServiceAccount")[0]
    assert account["automountServiceAccountToken"] is False


# --- secrets are not in the manifests ---------------------------------------


# Anything whose name ends in these is a credential, and a literal `value:`
# for one is a secret committed to git.
_SECRET_KEYS = ("_KEY", "_TOKEN", "_SECRET", "_PASSWORD")


def test_no_workload_inlines_a_credential() -> None:
    for name, spec in _all_pod_specs():
        for container in _app_containers(spec):
            for entry in container.get("env", []):
                if not entry["name"].endswith(_SECRET_KEYS):
                    continue
                assert "valueFrom" in entry, (
                    f"{name}/{container['name']} sets {entry['name']} literally"
                )
                assert "secretKeyRef" in entry["valueFrom"], (name, entry["name"])


def test_the_application_connects_as_the_non_bypass_role() -> None:
    """The migration owner is a superuser and bypasses row-level security. If
    the API's URL used it, every isolation guarantee at the application layer
    would be the only one left."""
    secret_doc = next(
        yaml.safe_load_all((K8S / "11-secret.example.yaml").read_text(encoding="utf-8"))
    )
    secret = secret_doc["stringData"]
    assert "platform_app:" in secret["APP_DATABASE_URL"]
    assert secret["APP_DATABASE_URL"] == secret["APP_DATABASE_APP_URL"]
    config = _by_kind("ConfigMap")[0]["data"]
    assert "APP_DATABASE_URL" not in config
    assert "app.tenant_id" not in config  # nothing pins a tenant cluster-wide


def test_every_api_worker_receives_both_app_database_url_settings_from_secret() -> None:
    for name, spec in _all_pod_specs():
        if name.endswith("/platform-migrate"):
            continue
        containers = _app_containers(spec)
        if not containers:
            continue
        env = {entry["name"]: entry for entry in containers[0].get("env", [])}
        for variable in ("APP_DATABASE_URL", "APP_DATABASE_APP_URL"):
            assert env[variable]["valueFrom"]["secretKeyRef"]["name"] == "platform-secrets", (
                name,
                variable,
            )


def test_kubernetes_connection_pools_leave_postgres_admin_headroom() -> None:
    """Three API replicas plus every worker must stay below the DB budget."""
    config = _by_kind("ConfigMap")[0]["data"]
    api = _named("Deployment", "platform-api")
    api_container = _app_containers(api["spec"]["template"]["spec"])[0]
    api_env = {entry["name"]: entry.get("value") for entry in api_container.get("env", [])}
    api_pool_per_pod = int(api_env["APP_DATABASE_POOL_SIZE"]) + int(
        api_env["APP_DATABASE_APP_POOL_SIZE"]
    )
    worker_pool_per_pod = int(config["APP_DATABASE_POOL_SIZE"]) + int(
        config["APP_DATABASE_APP_POOL_SIZE"]
    )
    worker_replicas = sum(
        int(deployment["spec"].get("replicas", 1)) for deployment in _worker_deployments().values()
    )
    max_connections = 100  # PostgreSQL's default; production must substitute its actual cap.
    planned = (
        int(api["spec"]["replicas"]) * api_pool_per_pod + worker_replicas * worker_pool_per_pod
    )
    assert planned <= max_connections - 10, (
        f"planned connection cap {planned} leaves fewer than 10 of {max_connections} "
        "PostgreSQL connections for migrations and administration"
    )


def test_bootstrap_tokens_are_not_enabled_anywhere() -> None:
    """`APP_ALLOW_BOOTSTRAP_TOKENS` accepts an unsigned token, and its absence
    is the safe state - so it must not appear as a setting anywhere.

    Asserted against the parsed objects rather than the file text: the
    ConfigMap's own comment explains why the key is missing, and a substring
    check would fail on the explanation.
    """
    for doc in _documents():
        if doc["kind"] == "ConfigMap":
            assert "APP_ALLOW_BOOTSTRAP_TOKENS" not in doc.get("data", {})
            continue
        spec = doc.get("spec", {}).get("template", {}).get("spec")
        if not spec:
            continue
        for container in spec.get("containers", []):
            for entry in container.get("env", []):
                assert entry["name"] != "APP_ALLOW_BOOTSTRAP_TOKENS", container["name"]


# --- availability -----------------------------------------------------------


def test_the_api_scales_and_survives_a_node_drain() -> None:
    deployment = _named("Deployment", "platform-api")
    assert deployment["spec"]["replicas"] >= 2
    assert deployment["spec"]["strategy"]["rollingUpdate"]["maxUnavailable"] == 0

    budget = _named("PodDisruptionBudget", "platform-api")
    assert budget["spec"]["minAvailable"] >= 1


def test_the_api_spreads_across_zones() -> None:
    deployment = _named("Deployment", "platform-api")
    constraints = deployment["spec"]["template"]["spec"]["topologySpreadConstraints"]
    assert any(c["topologyKey"] == "topology.kubernetes.io/zone" for c in constraints)


def test_the_api_has_all_three_probes() -> None:
    """A liveness probe pointing at a dependency is how a slow database gets a
    healthy API killed and replaced by another one that is equally slow."""
    container = _app_containers(_named("Deployment", "platform-api")["spec"]["template"]["spec"])[0]
    for probe in ("startupProbe", "readinessProbe", "livenessProbe"):
        assert probe in container, probe
        assert container[probe]["httpGet"]["path"] == "/healthz"


def test_the_api_has_a_grace_period_longer_than_a_model_call() -> None:
    spec = _named("Deployment", "platform-api")["spec"]["template"]["spec"]
    assert spec["terminationGracePeriodSeconds"] >= 30


def test_every_workload_declares_resource_requests_and_limits() -> None:
    """A pod with no request is scheduled as if it needed nothing and is the
    first to be evicted under pressure."""
    for name, spec in _all_pod_specs():
        for container in _app_containers(spec):
            resources = container.get("resources", {})
            assert resources.get("requests"), (name, container["name"])
            assert resources.get("limits"), (name, container["name"])
            assert "memory" in resources["requests"], (name, container["name"])


def test_the_ingestion_worker_is_not_scaled_by_cpu() -> None:
    """It idles in a poll loop between batches, so a CPU-based HPA would scale
    a backlog *down* to one pod. One replica, scaled on queue depth instead."""
    deployment = _named("Deployment", "platform-worker-ingestion")
    assert deployment["spec"]["replicas"] == 1
    names = {d["metadata"]["name"] for d in _by_kind("HorizontalPodAutoscaler")}
    assert "platform-worker-ingestion" not in names


# --- the worker roles are all deployed --------------------------------------


def _worker_deployments() -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for doc in _by_kind("Deployment"):
        name = doc["metadata"]["name"]
        if not name.startswith("platform-worker-"):
            continue
        out[name.removeprefix("platform-worker-")] = doc
    return out


def test_every_worker_role_has_a_deployment() -> None:
    """A role the runner supports but nothing deploys is dead code, and the
    outbox relay is the one that makes business events reach anybody at all."""
    deployed = _worker_deployments()
    assert set(deployed) == {*WORKER_QUEUES, "outbox"}


def test_each_worker_selects_its_own_queue() -> None:
    for role, deployment in _worker_deployments().items():
        container = _app_containers(deployment["spec"]["template"]["spec"])[0]
        command = container["command"]
        env = {e["name"]: e.get("value") for e in container.get("env", [])}
        if role == "outbox":
            # The flag wins over the environment, so the outbox pod must not
            # also declare a queue and leave a reader guessing which applies.
            assert "--outbox-only" in command
            assert "APP_WORKER_QUEUE" not in env
        else:
            assert env["APP_WORKER_QUEUE"] == WORKER_QUEUES[role], role


def test_every_worker_runs_the_shared_worker_entrypoint() -> None:
    """Same entrypoint, different role - which is what keeps API and worker from
    being built from different code."""
    for role, deployment in _worker_deployments().items():
        container = _app_containers(deployment["spec"]["template"]["spec"])[0]
        assert container["command"][:3] == ["python", "-m", "worker.runner"], role
        assert container["image"] == "registry.example.com/b2b-support/platform:REPLACE_ME_TAG"


@pytest.mark.parametrize("role", ["interactive", "ingestion", "outbox", "retention", "sla"])
def test_each_worker_has_a_grace_period_at_least_its_poll_interval(role: str) -> None:
    """SIGTERM must land between cycles, not inside one: the runner finishes the
    cycle in progress and stops."""
    spec = _worker_deployments()[role]["spec"]["template"]["spec"]
    assert spec["terminationGracePeriodSeconds"] >= 60


# --- migrations -------------------------------------------------------------


def test_migrations_run_once_before_the_workloads() -> None:
    """Several API replicas running `alembic upgrade head` concurrently is a
    race the tooling does not arbitrate, so it is a Job with no retries."""
    job = _by_kind("Job")[0]
    assert job["spec"]["backoffLimit"] == 0
    container = _app_containers(job["spec"]["template"]["spec"])[0]
    assert "upgrade" in container["args"] and "head" in container["args"]
    # The owner role, which `platform_app` is not: creating tables and policies
    # needs more than the application role has.
    env = {e["name"]: e for e in container["env"]}
    assert env["APP_DATABASE_URL"]["valueFrom"]["secretKeyRef"]["name"] == (
        "platform-migration-owner"
    )


def test_the_migration_job_uses_the_application_image() -> None:
    """A migration applied from a different build than the code that expects it
    is how a schema and its reader drift."""
    job = _by_kind("Job")[0]
    container = _app_containers(job["spec"]["template"]["spec"])[0]
    api = _app_containers(_named("Deployment", "platform-api")["spec"]["template"]["spec"])[0]
    assert container["image"] == api["image"]


# --- network ----------------------------------------------------------------


def test_ingress_is_denied_by_default() -> None:
    """`podSelector: {}` covers a workload added later that forgets its own
    policy, so "I forgot" fails closed."""
    denies = [
        d
        for d in _by_kind("NetworkPolicy")
        if d["metadata"]["name"] == "default-deny" and d["spec"]["podSelector"] == {}
    ]
    assert denies, "no namespace-wide default deny"
    assert set(denies[0]["spec"]["policyTypes"]) == {"Ingress", "Egress"}


def test_the_metadata_service_is_not_reachable() -> None:
    """A compromised pod reaching 169.254.169.254 steals the node's
    credentials. Excluding it explicitly also stops the egress rule reading as
    'everything is allowed'."""
    egress = [
        d for d in _by_kind("NetworkPolicy") if d["metadata"]["name"] == "egress-dependencies"
    ]
    assert egress
    excepts = [
        rule["to"][0]["ipBlock"]["except"]
        for rule in egress[0]["spec"]["egress"]
        if "ipBlock" in rule["to"][0]
    ]
    assert any("169.254.0.0/16" in block for block in excepts)


def test_metrics_are_not_routed_through_the_ingress() -> None:
    """Exposing operational detail to the internet for no benefit; it is
    scraped from inside the cluster through its own Service."""
    ingress = _by_kind("Ingress")[0]
    paths = [p["path"] for rule in ingress["spec"]["rules"] for p in rule["http"]["paths"]]
    assert paths == ["/"]
    names = {s["metadata"]["name"] for s in _by_kind("Service")}
    assert "platform-api-metrics" in names
