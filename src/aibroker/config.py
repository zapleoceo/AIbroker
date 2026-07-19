"""Centralized settings — pydantic-settings reads from env once at startup."""
from __future__ import annotations

from functools import lru_cache
from typing import Annotated

from pydantic import AfterValidator, Field, computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict


def _validate_session_secret(v: str) -> str:
    # Empty is allowed — services that never serve the dashboard (the monitor)
    # set no SESSION_SECRET, and the dashboard fails closed at runtime if it
    # tries to issue a cookie without one. But a NON-empty secret must be strong:
    # a weak one makes admin cookies forgeable. NB: a plain Field(min_length=32)
    # validated even the empty DEFAULT under pydantic-settings and crash-looped
    # the monitor (2026-07-10, pinning a CPU core) — hence an AfterValidator that
    # skips the empty case, not a field constraint.
    if v and len(v) < 32:
        raise ValueError("SESSION_SECRET, if set, must be at least 32 characters")
    return v


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    # DB
    DATABASE_URL: str = Field(..., description="postgres+asyncpg://...")
    # Bypass URL for the ONE consumer PgBouncer's transaction pooling can't
    # serve: the deep-jobs LISTEN connection (NOTIFY subscriptions need a
    # session pinned to a real server backend). Empty = no pooler in front,
    # fall back to DATABASE_URL — single-node setups set nothing.
    DIRECT_DATABASE_URL: str = ""

    @property
    def direct_database_url(self) -> str:
        return self.DIRECT_DATABASE_URL or self.DATABASE_URL

    # Crypto
    TOKEN_SECRET: str = Field(..., min_length=20)

    # Auth
    ADMIN_KEY: str = Field(..., min_length=20)
    INTERNAL_SECRET: str = Field(..., min_length=20)

    # Alerts + TG login widget for dashboard
    TELEGRAM_BOT_TOKEN: str = ""
    OWNER_TELEGRAM_ID: int = 0
    TELEGRAM_BOT_USERNAME: str = ""   # widget needs it, e.g. "Dimondra_Ai_Bot"

    # Session cookie HMAC (for /dashboard browser sessions). See the validator.
    SESSION_SECRET: Annotated[str, AfterValidator(_validate_session_secret)] = ""

    # Limits
    GLOBAL_DAILY_CAP_USD: float = 20.0

    # Self-hosted transcription (this repo's own services/asr-local) — empty
    # = the "local" provider is unreachable and every transcription request
    # falls straight through to groq/openai (see routing/chains.py).
    ASR_LOCAL_URL: str = ""
    # 2026-07-18: 90s -> 180s. Model is `small` int8 on 1 CPU thread with
    # beam_size=5 (large-v3-turbo/medium were both OOM-killed on this shared
    # host — see services/asr-local/app.py); the slower beam search plus a
    # possible no-VAD retry pass on an empty first decode means a single
    # request can run long. Low volume (~10 req/day, no backfill) makes the
    # wait affordable, and a real timeout beats a false-negative cooldown on a
    # local, private, free provider that's just still working.
    ASR_LOCAL_TIMEOUT_S: float = 180.0

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
