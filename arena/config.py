from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Read from environment variables (case-insensitive) or .env."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql://arena:arena@localhost:5432/arena"
    anthropic_api_key: str | None = None
    openai_api_key: str | None = None

    arena_roster_path: str = "roster.toml"
    arena_lease_seconds: float = 60.0
    arena_heartbeat_seconds: float = 20.0  # must be well under the lease
    arena_max_run_attempts: int = 5  # infra failures before a match is marked failed


@lru_cache
def settings() -> Settings:
    return Settings()
