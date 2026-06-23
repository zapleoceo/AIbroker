"""Centralized settings — pydantic-settings reads from env once at startup."""
from __future__ import annotations

from functools import lru_cache

from pydantic import Field, computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    # DB
    DATABASE_URL: str = Field(..., description="postgres+asyncpg://...")

    # Crypto
    TOKEN_SECRET: str = Field(..., min_length=20)

    # Auth
    ADMIN_KEY: str = Field(..., min_length=20)
    INTERNAL_SECRET: str = Field(..., min_length=8)

    # Alerts
    TELEGRAM_BOT_TOKEN: str = ""
    OWNER_TELEGRAM_ID: int = 0

    # Limits
    GLOBAL_DAILY_CAP_USD: float = 20.0
    DEFAULT_LEASE_SECONDS: int = 60

    # Host
    PUBLIC_HOST: str = "aib.zapleo.com"

    # Ops
    LOG_LEVEL: str = "INFO"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def alerts_enabled(self) -> bool:
        return bool(self.TELEGRAM_BOT_TOKEN) and self.OWNER_TELEGRAM_ID > 0


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
