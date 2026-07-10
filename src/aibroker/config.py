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
    INTERNAL_SECRET: str = Field(..., min_length=20)

    # Alerts + TG login widget for dashboard
    TELEGRAM_BOT_TOKEN: str = ""
    OWNER_TELEGRAM_ID: int = 0
    TELEGRAM_BOT_USERNAME: str = ""   # widget needs it, e.g. "Dimondra_Ai_Bot"

    # Session cookie HMAC (for /dashboard browser sessions). Empty is allowed
    # (dashboard just fails closed — auth_session raises), but any value that IS
    # set must be strong enough not to be brute-forceable: a weak secret means
    # forgeable admin cookies. min_length only constrains a provided value; an
    # absent one keeps the "" default (unvalidated) and fails closed at runtime.
    SESSION_SECRET: str = Field("", min_length=32)

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
