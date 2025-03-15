"""Application settings (environment-overridable via PERPLEXIFY_* variables)."""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PERPLEXIFY_", env_file=".env", extra="ignore")

    data_dir: Path = Path("data/cache")
    bot_state_dir: Path = Path("bot_state")
    default_exchange: str = "hyperliquid"
    http_timeout_seconds: float = 20.0
    http_max_retries: int = 4
    # Minimum spacing between requests to one venue (seconds). Keeps us well
    # inside every venue's public rate limit.
    http_min_interval_seconds: float = 0.15


settings = Settings()
