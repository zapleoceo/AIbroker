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
    # SQLAlchemy pool per process. Defaults suit the api; monitor and the agent
    # workers touch the DB lightly and set a small pool in docker-compose.yml so
    # they do not each hold up to 30 pgbouncer client connections.
    DB_POOL_SIZE: int = Field(10, ge=1)
    DB_MAX_OVERFLOW: int = Field(20, ge=0)

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
        from aibroker.agent.constants import OWNER_ONLY_SCOPES
        return [x.strip() for x in self.SIGNUP_DEFAULT_SCOPES.split(",")
                if x.strip() and x.strip() not in OWNER_ONLY_SCOPES]

    # Agent runs (docs/agent-runs.md): a dedicated, owner-only worker lane. OFF by default;
    # enable on exactly the process/container that has the CLIs and subscription logins.
    AGENT_WORKER_ENABLED: bool = False
    AGENT_CONCURRENCY_CODEX: int = 1       # per capability, independent of LLM jobs
    AGENT_CONCURRENCY_CLAUDE: int = 1
    # How long a subscription run waits for the caller's tool result before the call expires (and
    # its worker slot is freed). The run keeps its slot while it waits.
    AGENT_TOOL_WAIT_S: int = Field(300, ge=10, le=1800)
    # Model-retry inside one run (docs/subscription-tool-loop.md): schema-breaking tool calls the
    # model may be told about, and "call the tool now" follow-up turns after a plain-text finish.
    AGENT_TOOL_ARG_RETRIES: int = Field(2, ge=0, le=5)
    AGENT_TOOL_REQUIRED_REMINDERS: int = Field(1, ge=0, le=3)
    AGENT_WORKSPACE_ROOT: str = "/var/lib/aibroker/agent-workspaces"
    # The owner has checked in the Claude account settings that usage credits / extra usage are
    # OFF (a missing API key does NOT prove that). Until true, autonomous/exam agent runs are
    # refused; a manual run needs its own explicit per-run acknowledgement. We never change
    # account settings.
    AGENT_PAID_CONTINUATION_VERIFIED_OFF: bool = False
    # Separate owner confirmation for the Codex account. Claude's confirmation never
    # authorizes an autonomous Codex run.
    AGENT_CODEX_PAID_CONTINUATION_VERIFIED_OFF: bool = False
    # After a gated Codex run proves the login works at the current `last_refresh`, new runs skip
    # the start-up auth gate for this long (agent/codex_refresh_proof.py). 0 = always gate when due.
    AGENT_CODEX_REFRESH_PROOF_S: float = Field(1800.0, ge=0, le=3600)
    # ...but never once last_refresh is this old: near Codex's own (~8 d, UNVERIFIED) interval every
    # start-up is gated again, so the time-based refresh happens under the lease.
    AGENT_CODEX_REFRESH_PROOF_MAX_AGE_S: float = Field(6.5 * 24 * 3600, ge=0, le=7 * 24 * 3600)
    # Server-side allowlist of project ids that may use agent runs (comma-separated ints). EMPTY =
    # FAIL-CLOSED: every agent request is refused (403 agent_project_not_allowed) even with the
    # llm:agent scope, and the worker refuses queued runs of other projects.
    AGENT_ALLOWED_PROJECT_IDS: str = ""
    # Per-project agent policy, JSON: {"11": {"models": ["<exact id>"], "accounts": ["owner"]}}.
    # "*" admits known subscription candidates of the requested agent capability only.
    # Fail-closed: no entry / empty lists -> 403 agent_policy_missing; malformed -> everyone refused.
    AGENT_PROJECT_POLICY: str = ""
    # Which agent capabilities THIS worker process claims (comma-separated). Production runs one
    # container per CLI (agent-worker: agent:claude, agent-worker-codex: agent:codex) so each CLI
    # sees only its own auth volume; the default claims both (dev / single container).
    AGENT_WORKER_CAPABILITIES: str = "agent:claude,agent:codex"
    # Which account's runs THIS worker claims (agent_runs.account_id). One container per
    # subscription login (agent-worker = "owner", agent-worker-claude2 = "claude-2"). EMPTY = claim
    # runs of every account (dev / tests only): a worker that claims agent:claude refuses to start
    # with it empty (worker_image_problem), so a Claude login never serves another account's runs.
    AGENT_ACCOUNT_ID: str = ""
    # A never-started agent run still pending after this many seconds (no worker serves its
    # account) is failed `account_unavailable`. 0 disables.
    AGENT_PENDING_MAX_S: float = 1800.0
    # Explicit subscription pins visible in /v1/models. The Codex lane stays off until its
    # isolated worker profile is separately approved and deployed.
    AGENT_SUBSCRIPTION_MODELS: str = ('{"claude-sub":["claude-sonnet-5-5","claude-opus-5-5",'
                                      '"claude-haiku-5-5"],"codex-sub":['
                                      '"gpt-6.1-sol","gpt-6-astra","gpt-6-sol","gpt-6-luna",'
                                      '"gpt-5.6-sol","gpt-5.6-terra","gpt-5.6-luna"]}')
    # Exact account-confirmed route ids. Candidate models remain visible but unavailable until
    # confirmed; confirmation never overrides project policy or the Codex worker gate.
    AGENT_SUBSCRIPTION_VERIFIED_MODELS: str = '{"claude-sub":["claude-sonnet-5-5"],"codex-sub":[]}'
    # Input image capability is confirmed separately from a model's text availability.
    AGENT_SUBSCRIPTION_VISION_VERIFIED_MODELS: str = '{"codex-sub":[]}'
    # Codex models whose image GENERATION (exported PNG files) was verified live; the catalog lists
    # them with output_modalities ["text", "image"]. Must be verified Codex models.
    AGENT_SUBSCRIPTION_IMAGE_OUTPUT_VERIFIED_MODELS: str = '{"codex-sub":[]}'
    AGENT_SUBSCRIPTION_CODEX_ENABLED: bool = False
    AGENT_SUBSCRIPTION_CODEX_TOOLS_ENABLED: bool = False
    AGENT_SUBSCRIPTION_CODEX_TOOLS_VERIFIED_MODELS: str = "[]"
    AGENT_SUBSCRIPTION_CODEX_TOOLS_CANARY_MODELS: str = "[]"
    # Comma-separated project ids for the exact-model tool canaries of BOTH providers (the name is
    # historical); duplicates or junk make it empty. Prod: "12" (Orchestra).
    AGENT_SUBSCRIPTION_CODEX_TOOLS_CANARY_PROJECT_IDS: str = ""
    # Claude client tools (claude_tool_turn): same gate shape as Codex.
    AGENT_SUBSCRIPTION_CLAUDE_TOOLS_ENABLED: bool = False
    AGENT_SUBSCRIPTION_CLAUDE_TOOLS_VERIFIED_MODELS: str = "[]"
    AGENT_SUBSCRIPTION_CLAUDE_TOOLS_CANARY_MODELS: str = "[]"
    # Canary routes with a recorded live client-tool round-trip (call -> caller result -> final
    # answer) on that exact id: the catalog advertises supports_tools=true for them to the canary
    # projects. Evidence before listing: Sonnet job 557226 (2026-10-14).
    AGENT_SUBSCRIPTION_TOOLS_ATTESTED_MODELS: str = '["claude-sub/claude-sonnet-5-5"]'
    # Session lease (agent_session_leases): TTL must exceed ~3 heartbeats so one missed beat
    # never frees a live session, yet a crashed worker's lease expires on its own.
    AGENT_SESSION_LEASE_TTL_S: float = 120.0
    AGENT_SESSION_LEASE_HEARTBEAT_S: float = 30.0
    AGENT_CANCEL_POLL_S: float = 1.0
    AGENT_CANCEL_GRACE_S: float = 15.0     # how long adapter.cancel() may take
    AGENT_FILE_MAX_BYTES: int = 256 * 1024     # per returned file
    AGENT_FILES_TOTAL_BYTES: int = 1024 * 1024
    AGENT_FILES_MAX_COUNT: int = 20
    # per exported generated image; Codex gpt-image PNGs run 1.7-2.1 MB, so 2 MiB dropped some
    AGENT_IMAGE_MAX_BYTES: int = 8 * 1024 * 1024
    AGENT_IMAGES_MAX_COUNT: int = 4
    # all images of one run together (generated + written to out/); over it: skipped=total_budget
    AGENT_IMAGES_TOTAL_BYTES: int = 24 * 1024 * 1024
    AGENT_FILES_TTL_S: int = 3600          # returned file content expires after this

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
