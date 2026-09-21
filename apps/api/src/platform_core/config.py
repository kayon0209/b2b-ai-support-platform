"""Application configuration.

Configuration is validated at startup per docs/deployment-and-operations.md:
missing security-critical settings must fail startup, not fall back.
"""

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="APP_", extra="ignore")

    environment: Literal["local", "test", "staging", "production"] = "local"

    # Security-critical: no defaults. Startup fails when unset outside tests.
    secret_key: SecretStr | None = Field(default=None)

    # Local default matches infra/compose/docker-compose.yml (ai-postgres is
    # published on 5435 to avoid colliding with a host PostgreSQL on 5432).
    database_url: str = "postgresql+psycopg://platform:platform@localhost:5435/platform"
    # Reserved. The durable work queue is a Postgres table (inbox_events /
    # outbox_events claimed with SKIP LOCKED), not Redis, so nothing reads this
    # today. It is kept, and kept separate from Chatwoot's Redis (6381), so
    # that a future cache/rate-limit feature does not have to invent a setting
    # or accidentally share Chatwoot's instance.
    redis_url: str = "redis://localhost:6380/0"

    # --- Authentication (docs/security.md) --------------------------------
    # Keycloak realm issuer, e.g. http://localhost:8081/realms/platform.
    # When set, OIDC is the request authentication path.
    oidc_issuer: str | None = None
    oidc_audience: str = "platform-api"
    oidc_jwks_cache_seconds: int = 300

    # The bootstrap token scheme (`pt_<tenant-slug>_<user-uuid>`) is UNSIGNED:
    # anyone who knows a slug and a user UUID can impersonate that user. It
    # exists so the platform can be exercised locally before a realm is
    # configured, and it must never be reachable in a deployed environment.
    #
    # Default is False, so the insecure path is opt-in rather than something
    # you get by forgetting to configure OIDC.
    allow_bootstrap_tokens: bool = False

    # Webhook replay protection (docs/api-contracts.md)
    webhook_timestamp_tolerance_seconds: int = 300

    # Chatwoot integration (ticket 4/8 will consume these)
    chatwoot_base_url: str = "http://localhost:3000"
    chatwoot_api_token: SecretStr | None = None
    chatwoot_webhook_secret: SecretStr | None = None

    # LLM provider (Gitee AI / 模力方舟, OpenAI-compatible surface).
    # Credentials are resolved server-side and never reach the model or logs
    # (docs/security.md). Unset api_key means the model boundary fails closed.
    llm_base_url: str = "https://ai.gitee.com/v1"
    llm_api_key: SecretStr | None = None
    llm_model: str = "qwen3.8-flash"
    llm_embedding_model: str = "Qwen3-Embedding-8B"
    llm_rerank_model: str = "bge-reranker-v2-m3"
    # Matches the chunks.embedding vector(1536) column; the provider honors
    # a dimensions request so no schema migration is required.
    llm_embedding_dimensions: int = 1536
    llm_timeout_seconds: float = 30.0
    llm_max_retries: int = 2
    # Retrieval reranker deadline; on breach we fall back to fused order.
    rerank_timeout_seconds: float = 2.0

    # Object storage (MinIO/S3) for immutable document originals.
    # Client-facing access is always a short-lived pre-signed URL
    # (docs/security.md), generated server-side - the API never proxies bytes
    # and never hands out a public path.
    object_storage_endpoint: str = "localhost:9000"
    object_storage_access_key: SecretStr | None = None
    object_storage_secret_key: SecretStr | None = None
    object_storage_bucket: str = "documents"
    object_storage_secure: bool = False
    # Lifetime of a download URL. Short by design: the URL is a bearer
    # credential for the object, so its value is that it stops working.
    presign_expiry_seconds: int = 300

    # --- Observability -------------------------------------------------------
    # Whether /metrics is served. This is an EXPOSURE decision, not a security
    # control: the endpoint is unauthenticated by design so Prometheus can
    # scrape it, and its payload is safe only because no metric carries a
    # tenant-identifying label. What remains visible is business volume, so
    # the default keeps it on where a developer and CI need it and off in
    # deployed environments unless an operator opts in.
    #
    # None means "decide from the environment"; see observability_router.
    metrics_enabled: bool | None = None

    # --- Inbound rate limiting (Phase 4) -------------------------------------
    # A token bucket per tenant (or per client address for traffic that
    # arrives without a tenant, i.e. webhooks). Counters live in Redis:
    # ephemeral, loss-tolerant state, which is exactly what ADR 0002 says
    # Redis is for - durable work stays in Postgres tables.
    rate_limit_enabled: bool = True
    # Interactive API traffic, per tenant.
    rate_limit_requests: int = 600
    rate_limit_window_seconds: int = 60
    # Traffic with no tenant yet, keyed by client address.
    rate_limit_anonymous_requests: int = 300
    # Provider deliveries are burstier and must not be throttled into data
    # loss, so they get their own, more generous budget.
    rate_limit_webhook_requests: int = 1200

    # --- Chunking and ingestion (iteration plan 1.1/1.6/4.6) ------------------
    # Defaults recorded in each document version's metadata together with the
    # pipeline version, so a chunk-set is always reproducible from its row.
    # The tuning report (scripts/tune_chunking.py) is the source these derive
    # from; changing a default here must update that report.
    chunking_max_chars: int = 1200
    chunking_min_chars: int = 50
    # Paragraph-level overlap: the tail of the previous chunk is carried into
    # the next so cross-paragraph arguments survive the cut.
    chunking_overlap_chars: int = 150
    # Cleaning steps (4.6). Each is individually switchable; counts of what
    # each step changed are written into version metadata.
    cleaning_enabled: bool = True
    cleaning_dedupe_chunks: bool = True
    cleaning_strip_boilerplate: bool = True

    # --- Retrieval (iteration plan 1.3/1.5/1.7) -------------------------------
    # Per-path candidate budgets. Each path can be disabled independently
    # (retrieval_enabled_paths) so its contribution to recall is measurable.
    retrieval_fts_candidates: int = 40
    retrieval_vector_candidates: int = 40
    retrieval_trigram_candidates: int = 40
    retrieval_alias_candidates: int = 20
    retrieval_enabled_paths: str = "fts,vector,trigram,alias"
    retrieval_rrf_k: int = 60
    retrieval_top_k: int = 8
    # Scene-tiered top-k: policy questions want wide evidence, fault-code
    # questions want narrow precision.
    retrieval_top_k_policy: int = 12
    retrieval_top_k_technical: int = 6
    # Rerank only the top-N fused candidates (never more than 2x top_k).
    rerank_candidate_cap: int = 16
    # Post-fusion relative score floor: candidates below
    # floor_ratio * top_score are not sent to the model. Relative, because
    # RRF scores are tiny in absolute terms - an absolute floor never fires.
    retrieval_score_floor_ratio: float = 0.55
    # Document authority (4.5) feeds a post-fusion ranking boost. Off until
    # the corpus carries authority values; weights are per-authority
    # multipliers as JSON, e.g. {"official":1.15,"wiki":1.0,"customer":0.9}.
    retrieval_authority_boost_enabled: bool = False
    retrieval_authority_boost_json: str = ""

    # --- Multi-turn memory (iteration plan 2.1-2.8) ---------------------------
    context_budget_chars: int = 1500
    # Evidence wins budget contention: an answer must stay grounded, while a
    # shrunken context degrades gracefully.
    context_min_budget_chars: int = 300
    context_recent_turns: int = 6
    context_max_turns: int = 50
    # Consecutive clarification rounds before the run hands off instead of
    # asking again - an ask-loop is a dead conversation with extra steps.
    clarification_max_streak: int = 2
    # Chatwoot history fetch (2.2). Failure degrades to single-turn; it never
    # blocks the run.
    history_fetch_limit: int = 20
    history_fetch_timeout_seconds: float = 3.0
    # Local redacted turns are pruned after N days (retention policy), and
    # Chatwoot stays the system of record for raw content.
    conversation_turn_days: int = 90

    # --- Cost, concurrency and fallback (iteration plan 5.1/5.3/5.5) ----------
    # Hard limits per run. Hitting one degrades the run (handoff with a
    # recorded event) rather than silently looping retries - the most
    # expensive sessions are always the retry loops.
    run_max_llm_calls: int = 10
    run_token_budget: int = 12000
    # Model fallback chain: primary -> fallback -> abstain. Off until a
    # fallback model is configured and drilled.
    model_fallback_enabled: bool = False
    model_fallback_name: str | None = None
    # Process-wide admission limits for the two contended resources.
    concurrency_model_limit: int = 8
    concurrency_retrieval_limit: int = 16
    # Inbox depth above which new runs are refused with 429 instead of
    # queueing - bounded backlog beats unbounded latency.
    queue_max_depth: int = 500
    # Exponential-backoff jitter: without it every caller retries in the same
    # beat and the retry storm is synchronised. 0 disables.
    retry_jitter_ratio: float = 0.3
    # Estimated model pricing for the cost metric, cents per 1k tokens.
    cost_prompt_cents_per_1k: float = 0.15
    cost_completion_cents_per_1k: float = 0.60
    # Evidence-carrying handoff notes (5.4): private Chatwoot note with the
    # reason code and evidence references, readable by the receiving agent.
    handoff_evidence_enabled: bool = False

    # --- Feature flags for new behaviour (constraint 4) -----------------------
    # Every behaviour change below defaults OFF and flips per tenant through
    # the flag service, following agent.rerank_enabled.
    flag_business_read_tools: str = "agent.business_read_enabled"
    # The agent's write path (plan 3.5). Separate from the read flag on
    # purpose: a tenant can let the AI answer "where is my order" long before
    # it lets the AI propose a change to an external system, and rolling the
    # two out together would make the safe half hostage to the risky one.
    flag_business_write_tools: str = "agent.business_write_enabled"
    flag_citation_guard: str = "agent.citation_guard_enabled"
    flag_query_normalization: str = "agent.query_normalization_enabled"
    flag_metadata_filter: str = "retrieval.metadata_filter_enabled"
    flag_score_floor: str = "retrieval.score_floor_enabled"
    flag_priority_claim: str = "worker.priority_claim_enabled"
    flag_redline_guard: str = "agent.redline_guard_enabled"
    # Shadow mode (9.2): run the whole pipeline, withhold the send. Per tenant
    # and off by default, because it is a rollout control - it is switched on
    # for the window where a newly-automated category is being watched, then
    # off again.
    flag_shadow_mode: str = "agent.shadow_mode_enabled"
    # When a human is actually available (feature list 7.5), UTC hours.
    # 0/0 means unconfigured, which reads as always open: a tenant that has
    # not told us its hours must not gain a new way to refuse its customers.
    support_open_hour: int = 0
    support_close_hour: int = 0
    # Which price table to quote from. "public-reference" uses published
    # industry data with its provenance attached (see pricing/reference);
    # "empty" declines every request so all quoting goes to a person, which is
    # the right setting until a contracted price list exists.
    pricing_ruleset: str = "public-reference"
    # "http" is the real BusinessReadAdapter against the tenant's ERP. "demo"
    # uses local sample data (see integrations/demo_erp) so the read path can
    # run in a deployment that has no ERP to call; its records are marked
    # `source: "demo"` so they are never mistaken for real ones.
    business_api_adapter: str = "demo"
    # Priority claiming is a deployment-level decision (the claim query is
    # cross-tenant), so it is a plain switch rather than a tenant flag.
    priority_claim_enabled: bool = False


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    if settings.environment in ("staging", "production") and settings.secret_key is None:
        raise RuntimeError("APP_SECRET_KEY is required in staging/production")
    _assert_auth_is_configured(settings)
    return settings


def _assert_auth_is_configured(settings: Settings) -> None:
    """Refuse to start a deployed environment with an unusable auth setup.

    Two failure modes are caught here rather than at request time, because at
    request time both look like an ordinary 401 and would be debugged as a
    token problem:

    1. No OIDC issuer and bootstrap tokens disabled - every request would be
       rejected and the platform would be unreachable, with no clue why.
    2. Bootstrap tokens enabled outside local/test - the unsigned token scheme
       would be a live impersonation path in a deployed environment.

    `test` is included alongside `local` because the integration suite needs
    to authenticate without standing up a realm; it is never a deployed
    environment.
    """
    local_like = settings.environment in ("local", "test")

    if settings.allow_bootstrap_tokens and not local_like:
        raise RuntimeError(
            "APP_ALLOW_BOOTSTRAP_TOKENS is set but bootstrap tokens are unsigned "
            "and allow impersonation with a known slug + user id. It is only "
            f"permitted in local/test, not {settings.environment!r}. Configure "
            "APP_OIDC_ISSUER instead."
        )

    # `environment` defaults to "local", so a deployment that simply forgets to
    # declare it is treated as a local machine - and if it also inherited
    # `APP_ALLOW_BOOTSTRAP_TOKENS=true` from a copied .env, the check above
    # passes and the impersonation path is live in production.
    #
    # `model_fields_set` distinguishes "declared" from "defaulted" (the .env
    # file counts as declared; a default does not), so the guard is about
    # intent rather than about the value. Opting into an unsigned token scheme
    # now requires saying which environment you are in.
    if settings.allow_bootstrap_tokens and "environment" not in settings.model_fields_set:
        raise RuntimeError(
            "APP_ALLOW_BOOTSTRAP_TOKENS is set but APP_ENVIRONMENT was never "
            "declared, so this process cannot tell a laptop from production. "
            "Bootstrap tokens are unsigned and impersonate a known slug + user "
            "id, so declare APP_ENVIRONMENT explicitly (local/test) before "
            "enabling them."
        )

    if settings.oidc_issuer is None and not settings.allow_bootstrap_tokens:
        raise RuntimeError(
            "no authentication configured: set APP_OIDC_ISSUER, or set "
            "APP_ALLOW_BOOTSTRAP_TOKENS=true for local development"
        )
