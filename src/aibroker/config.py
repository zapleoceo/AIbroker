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

    # Hard wall-clock ceilings (asyncio.wait_for) on the non-chat provider
    # calls, like call_llm's timeout (2026-10-03 review): litellm's own
    # `timeout` kwarg does not reliably cut a hung call (confirmed live on zai,
    # see call_llm), and embed/transcribe had NO ceiling at all, so one hung
    # upstream could pin a request and its reservation until the client gave
    # up. A hit raises TimeoutError → classified rate_limit → key cooled.
    EMBED_TIMEOUT_S: float = 60.0
    # Whisper (groq/openai atranscription) and chat-based transcription (gemini
    # fallback). Typical is ~1 s; the ceiling covers a 25 MB upload.
    TRANSCRIBE_TIMEOUT_S: float = 120.0
    GEMINI_ASR_TIMEOUT_S: float = 60.0

    # How long a request family stays pinned to the (provider, model, key) whose
    # provider-side prompt cache is warm (routing/affinity.py). Default 2h.
    AFFINITY_TTL_S: float = 7200.0

    # Self-hosted vision (llama.cpp serving Qwen3-VL-4B-Instruct Q4_K_M on
    # CPU) — empty = the "local" provider is unreachable and vision falls
    # straight through to gemini/openrouter/openai (see routing/chains.py).
    VISION_LOCAL_URL: str = ""
    # Measured on this host 2026-08-31: 69s for a chat screenshot through
    # llama-server with the model already resident, 81-192s (median 163s) for
    # the same images through the one-shot CLI that reloads the model every
    # call. Documents are the slow end. 300s leaves room for the slowest
    # document plus a cold model load (22s) without cooling the key on a
    # provider that is simply still working — the same logic as any slow
    # self-hosted provider that is simply still working.
    VISION_LOCAL_TIMEOUT_S: float = 300.0
    # Longest edge, in pixels, an image is downscaled to before it reaches the
    # model. NOT a nicety: at native resolution the vision encoder does not fit
    # in memory on this host, and a native-resolution probe ran past 600s
    # without ever completing while the same image at 1024px took 69s.
    VISION_LOCAL_MAX_PX: int = 1024
    # How long a second vision request may WAIT for the one local slot before
    # it escalates to the cloud tail (2026-09-12). llama-server runs
    # --parallel 1; before this, concurrent requests queued INSIDE it against
    # the 300s HTTP timeout, so the waiting one timed out (45 TimeoutErrors a
    # day), cooled the local key, and every image behind it spilled to the
    # rate-limited cloud pool. The slot is an in-process semaphore: waiting
    # costs nothing, and 240s covers one worst-case document ahead of you.
    VISION_LOCAL_QUEUE_WAIT_S: float = 240.0

    # Public self-signup (POST /v1/signup, services/signup.py). A signed-up project
    # is free-providers-only ($0/day) with a lifetime request cap; the owner
    # raises either in the dashboard. SIGNUP_ENABLED is the kill switch.
    SIGNUP_ENABLED: bool = True
    SIGNUP_PER_IP_PER_DAY: int = 3
    SIGNUP_PER_DAY: int = 50
    SIGNUP_REQUEST_CAP: int = 100
    # Comma-separated (a plain env var cannot carry a list without JSON).
    SIGNUP_DEFAULT_SCOPES: str = "llm:chat,llm:embed"

    @property
    def signup_scopes(self) -> list[str]:
        return [x.strip() for x in self.SIGNUP_DEFAULT_SCOPES.split(",") if x.strip()]

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
