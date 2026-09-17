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

    if settings.oidc_issuer is None and not settings.allow_bootstrap_tokens:
        raise RuntimeError(
            "no authentication configured: set APP_OIDC_ISSUER, or set "
            "APP_ALLOW_BOOTSTRAP_TOKENS=true for local development"
        )
