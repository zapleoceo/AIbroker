"""Centralized settings — pydantic-settings reads from env once at startup."""
from __future__ import annotations

from functools import lru_cache

from pydantic import Field, computed_field, field_validator
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
    INTERNAL_SECRET: str = Field(..., min_length=20)

    # Alerts + TG login widget for dashboard
    TELEGRAM_BOT_TOKEN: str = ""
    OWNER_TELEGRAM_ID: int = 0
    TELEGRAM_BOT_USERNAME: str = ""   # widget needs it, e.g. "Dimondra_Ai_Bot"

    # Session cookie HMAC (for /dashboard browser sessions).
    SESSION_SECRET: str = ""

    @field_validator("SESSION_SECRET")
    @classmethod
    def _session_secret_strength(cls, v: str) -> str:
        # Empty is allowed — services that never serve the dashboard (the
        # monitor) don't set it, and the dashboard fails closed at runtime if it
        # tries to issue a cookie without one. But a NON-empty secret must be
        # strong: a weak one makes admin cookies forgeable. NB: a plain
        # Field(min_length=32) ALSO validated the empty default under
        # pydantic-settings and crash-looped the monitor (2026-07-10) — hence a
        # validator that skips the empty case instead of a field constraint.
        if v and len(v) < 32:
            raise ValueError("SESSION_SECRET, if set, must be at least 32 characters")
        return v

    # Limits
    GLOBAL_DAILY_CAP_USD: float = 20.0
    DEFAULT_LEASE_SECONDS: int = 60
    # Vending mode (POST /v1/key) hands out a REAL plaintext provider token per
    # call — unlike proxy mode there's no per-call cost signal to gate on, so a
    # compromised project key could otherwise drain the lease pool or exfiltrate
    # tokens at will. Cap per-project vend calls per rolling minute.
    VENDING_RATE_LIMIT_PER_MINUTE: int = 30

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
