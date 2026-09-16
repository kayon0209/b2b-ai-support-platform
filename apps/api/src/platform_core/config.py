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


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    if settings.environment in ("staging", "production") and settings.secret_key is None:
        raise RuntimeError("APP_SECRET_KEY is required in staging/production")
    return settings
