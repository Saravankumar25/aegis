"""Application settings, loaded from environment / .env (ESD §16: secrets via env only).

A single ``Settings`` instance is the one place configuration is read; nothing else reaches into
``os.environ`` directly, so tests can override values and no credential is hardcoded.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Typed configuration for the Aegis backend."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- Database (single Postgres + pgvector, ESD §19) ---
    database_url: str = Field(
        default="postgresql+asyncpg://aegis:change-me-locally@localhost:5432/aegis",
        description="SQLAlchemy async URL for the single Postgres instance.",
    )

    # --- Redis (cache only, never a source of truth, ESD §11) ---
    redis_url: str = Field(default="redis://localhost:6379/0")

    # --- Auth (self-issued JWT in httpOnly cookies, ESD §8) ---
    jwt_secret: str = Field(default="change-me-generate-a-long-random-string")
    jwt_access_ttl_seconds: int = Field(default=900)
    jwt_refresh_ttl_seconds: int = Field(default=604800)

    # --- LLM provider (Strategy pattern, ESD §20). Default deterministic stub, no key needed. ---
    llm_provider: str = Field(default="stub")

    # --- Incident tuning (PRD FR-1.2, FR-3.1; ESD §15) ---
    dedup_window_seconds: int = Field(default=300)
    rca_ensemble_passes: int = Field(default=3)
    incident_token_budget: int = Field(default=200_000)

    # --- Infra evidence sources (read-only MCP servers in MVP) ---
    prometheus_url: str = Field(default="http://localhost:9090")
    github_repo: str = Field(default="Saravankumar25/aegis")

    # Runtime environment marker; "test" disables some production-only guards.
    environment: str = Field(default="local")


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings singleton."""
    return Settings()
