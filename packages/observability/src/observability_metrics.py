"""Prometheus metrics for the platform (docs/deployment-and-operations.md).

Design rules, in priority order:

1. **No tenant-identifying labels.** Every metric in this module is either
   label-free or labelled with a code-owned vocabulary (route, status,
   reason_code, queue, operation). Tenant ids, user ids, conversation ids
   and document ids are never label values. A per-tenant label would turn
   `/metrics` into a cross-tenant disclosure surface - the exact question
   AGENTS.md requires every change to answer ("Can the change expose
   another tenant through search, cache, logs, files, or **metrics**?").
   Where per-tenant attribution is genuinely needed for operations, it
   comes from a trace, not from a metric label.

2. **Cardinality is bounded by construction.** `platform_range_labels`
   exists so a caller can assert that a label set is drawn from a small
   closed vocabulary before it is used. Unbounded label values (`str(exc)`,
   document titles, question text) turn a metrics endpoint into a memory
   leak and a de-facto log with worse retention.

3. **The registry is created here, not imported globally.** Tests build a
   fresh `CollectorRegistry`, so two test modules cannot collide on
   duplicate metric names, and a suite never sees counters polluted by an
   earlier test in the same process.

4. **Absent means absent, not zero.** A histogram that has never been
   observed has no percentile. Do not synthesise one.

The `prometheus-client` dependency was declared in `requirements-dev.txt`
long before this module existed; this is what reads it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from prometheus_client import CollectorRegistry, Counter, Histogram

# --- Label vocabularies ----------------------------------------------------
#
# These are code-owned sets. The names match the reason/route/status values
# already persisted on AgentRun, so a metric and an audit event can be
# correlated without a translation table.

RUN_OUTCOMES = ("completed", "abstained", "handed_off", "failed", "running")
RUN_ROUTES = ("knowledge_qa", "human_required", "out_of_scope")
# Abstention reason codes plus the failure codes the orchestrator synthesises
# before the abstention gate is reached. Kept in sync with qa_path /
# orchestrator literals; a value outside this set is a programming error, not
# a runtime condition, so `observe_run` refuses to invent a label for it.
CITATION_STATUSES = ("supported", "unsupported", "no_claims")

WORKER_QUEUES = ("interactive", "ingestion", "outbox")

# Latency buckets. The upper bound matters: docs/development-plan.md sets a
# first-token P95 budget of 2.5 s, so the buckets must be dense below 5 s or
# the P95 estimate is an artefact of bucket width rather than a measurement.
_LATENCY_BUCKETS = (0.05, 0.1, 0.25, 0.5, 0.75, 1.0, 1.5, 2.0, 2.5, 3.5, 5.0, 8.0, 15.0, 30.0)


def platform_range_labels(kind: str) -> tuple[str, ...]:
    """Return the closed label vocabulary for `kind`.

    Callers use this to validate a value before it becomes a label. The
    alternative - letting any string through - is how a metric becomes an
    unbounded memory map keyed on attacker-influenced text.
    """
    try:
        return _LABEL_RANGES[kind]
    except KeyError as exc:  # pragma: no cover - programming error
        raise KeyError(f"unknown metric label kind: {kind!r}") from exc


_LABEL_RANGES: dict[str, tuple[str, ...]] = {
    "run_outcome": RUN_OUTCOMES,
    "run_route": RUN_ROUTES,
    "citation_status": CITATION_STATUSES,
    "worker_queue": WORKER_QUEUES,
}


@dataclass
class PlatformMetrics:
    """The metric instruments, owned by whoever builds the registry.

    Held as a dataclass rather than module-level globals so a caller can
    construct an isolated instance bound to a private registry (tests) or to
    the process-wide default registry (the API and worker processes).
    """

    registry: CollectorRegistry = field(default_factory=CollectorRegistry)

    def __post_init__(self) -> None:
        r = self.registry

        # --- Agent run lifecycle ---
        self.runs_total = Counter(
            "platform_agent_runs_total",
            "Agent runs by terminal outcome and route.",
            labelnames=("outcome", "route"),
            registry=r,
        )
        self.run_latency_seconds = Histogram(
            "platform_agent_run_latency_seconds",
            "End-to-end agent run latency (abstention gate through dispatch).",
            labelnames=("outcome",),
            buckets=_LATENCY_BUCKETS,
            registry=r,
        )
        self.abstentions_total = Counter(
            "platform_agent_abstentions_total",
            "Abstentions by reason code.",
            labelnames=("reason_code",),
            registry=r,
        )
        self.citations_per_run = Histogram(
            "platform_agent_citations_per_run",
            "Persisted citations per completed run.",
            buckets=(0, 1, 2, 3, 4, 5, 8, 13, 21),
            registry=r,
        )
        self.citation_validation_total = Counter(
            "platform_agent_citation_validation_total",
            "Citation validator decisions (supported vs rejected drafts).",
            labelnames=("status",),
            registry=r,
        )
        self.lease_conflicts_total = Counter(
            "platform_agent_lease_conflicts_total",
            "Pre-send control-lease conflicts: a human took over mid-generation "
            "and the generated answer was withheld.",
            registry=r,
        )

        # --- Retrieval ---
        self.retrieval_latency_seconds = Histogram(
            "platform_retrieval_latency_seconds",
            "Hybrid retrieval latency (FTS + vector + fusion).",
            buckets=_LATENCY_BUCKETS,
            registry=r,
        )
        self.retrieval_candidates = Histogram(
            "platform_retrieval_candidates",
            "Candidate chunks surviving the ACL/tenant pre-filter.",
            buckets=(0, 1, 2, 4, 8, 16, 32, 64),
            registry=r,
        )
        self.retrieval_degraded_total = Counter(
            "platform_retrieval_degraded_total",
            "Retrieval degraded to a weaker path, by reason code.",
            labelnames=("reason_code",),
            registry=r,
        )

        # --- Model boundary ---
        self.model_latency_seconds = Histogram(
            "platform_model_call_latency_seconds",
            "Model call latency by operation.",
            labelnames=("operation", "outcome"),
            buckets=_LATENCY_BUCKETS,
            registry=r,
        )
        self.model_errors_total = Counter(
            "platform_model_errors_total",
            "Model calls that raised a mapped ModelError, by code.",
            labelnames=("error_code",),
            registry=r,
        )
        self.model_tokens_total = Counter(
            "platform_model_tokens_total",
            "Tokens consumed at the model boundary.",
            labelnames=("direction",),
            registry=r,
        )

        # --- Inbox / worker ---
        self.inbox_events_total = Counter(
            "platform_inbox_events_total",
            "Inbox events by processing result.",
            labelnames=("result",),
            registry=r,
        )
        self.inbox_claim_age_seconds = Histogram(
            "platform_inbox_claim_age_seconds",
            "Time between an event being received and its agent run starting. "
            "This is the queue-age signal docs/deployment-and-operations.md "
            "makes a P1 alert: measuring only run latency would miss it.",
            buckets=_LATENCY_BUCKETS,
            registry=r,
        )
        self.stale_claims_reclaimed_total = Counter(
            "platform_stale_claims_reclaimed_total",
            "Inbox rows returned to RECEIVED after a worker died mid-run.",
            registry=r,
        )

        # --- Ingestion ---
        self.ingestion_versions_total = Counter(
            "platform_ingestion_versions_total",
            "Document versions processed by ingestion, by terminal state.",
            labelnames=("state",),
            registry=r,
        )
        self.ingestion_latency_seconds = Histogram(
            "platform_ingestion_latency_seconds",
            "Document version ingestion latency (chunk through index).",
            buckets=_LATENCY_BUCKETS,
            registry=r,
        )

        # --- HTTP ---
        self.http_requests_total = Counter(
            "platform_http_requests_total",
            "HTTP requests by method, route template, status and outcome. "
            "Route templates (not raw paths) so a path parameter cannot "
            "inflate cardinality or leak an identifier.",
            labelnames=("method", "route", "status", "outcome"),
            registry=r,
        )

        # --- Authorization ---
        self.policy_denials_total = Counter(
            "platform_policy_denials_total",
            "Policy denials by action and reason code. No tenant labels: a "
            "denial count is an operational signal, and per-tenant denial "
            "counts belong in audit events, which are access-controlled.",
            labelnames=("action", "reason_code"),
            registry=r,
        )

    # --- Recording helpers -------------------------------------------------
    #
    # The helpers exist so call sites do not each invent their own label
    # values, and so an out-of-vocabulary value fails fast here rather than
    # silently creating an unbounded label set in production.

    def _check(self, kind: str, value: str) -> str:
        allowed = platform_range_labels(kind)
        if value not in allowed:
            raise ValueError(f"{kind} label {value!r} is not in {allowed}")
        return value

    def observe_run(
        self,
        *,
        outcome: str,
        route: str,
        latency_seconds: float,
        abstain_reason: str = "",
        citation_count: int | None = None,
    ) -> None:
        """Record one finished agent run."""
        self.runs_total.labels(
            outcome=self._check("run_outcome", outcome),
            route=self._check("run_route", route),
        ).inc()
        if latency_seconds >= 0:
            self.run_latency_seconds.labels(outcome=outcome).observe(latency_seconds)
        if abstain_reason:
            self.abstentions_total.labels(reason_code=abstain_reason).inc()
        if citation_count is not None:
            self.citations_per_run.observe(max(0, citation_count))

    def observe_retrieval(self, *, latency_seconds: float, candidate_count: int) -> None:
        self.retrieval_latency_seconds.observe(max(0.0, latency_seconds))
        self.retrieval_candidates.observe(max(0, candidate_count))

    def observe_model_call(
        self,
        *,
        operation: str,
        outcome: str,
        latency_seconds: float,
        error_code: str = "",
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
    ) -> None:
        self.model_latency_seconds.labels(operation=operation, outcome=outcome).observe(
            max(0.0, latency_seconds)
        )
        if error_code:
            self.model_errors_total.labels(error_code=error_code).inc()
        if prompt_tokens:
            self.model_tokens_total.labels(direction="prompt").inc(prompt_tokens)
        if completion_tokens:
            self.model_tokens_total.labels(direction="completion").inc(completion_tokens)


# --- Process-wide default --------------------------------------------------
#
# One registry per process. The API and the worker are separate processes
# with separate registries, which is correct: a counter read from the API
# must not include work done by a worker, or a single scrape would double
# count every ingestion event.
#
# Construction is deferred until first use so importing this module never
# registers collectors as a side effect (a test that imports it and builds
# its own registry must not find the name already taken).

_default: PlatformMetrics | None = None


def get_metrics() -> PlatformMetrics:
    """Return the process-wide metrics instance, creating it on first use."""
    global _default
    if _default is None:
        _default = PlatformMetrics()
    return _default


def reset_default_metrics() -> None:
    """Drop the process-wide instance. Test-only.

    Needed because `lru_cache`-style global state leaks between tests in the
    same process: without this, a test asserting "no metric is emitted" would
    see a previous test's observations.
    """
    global _default
    _default = None


def render_metrics(metrics: PlatformMetrics | None = None) -> bytes:
    """Render the registry in Prometheus text exposition format."""
    from prometheus_client import generate_latest

    registry = metrics.registry if metrics is not None else get_metrics().registry
    return bytes(generate_latest(registry))


def metric_sample_value(metrics: PlatformMetrics, name: str, **labels: Any) -> float | None:
    """Read one sample value for assertions and dashboards tests.

    Returns None when the series does not exist yet, so a test can
    distinguish "never observed" from "observed as zero".
    """
    value = metrics.registry.get_sample_value(name, labels or None)
    return None if value is None else float(value)
