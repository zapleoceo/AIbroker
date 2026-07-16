"""Cheapest possible call per provider — used by monitor to check key liveness."""
from __future__ import annotations

import asyncio
import logging
import re

import httpx

log = logging.getLogger(__name__)


PROBE_TIMEOUT_S = 15


async def probe(
    provider: str, plain_key: str, account_id: str | None = None
) -> tuple[str, int, str]:
    """Returns (verdict, http_status, hint).
    verdict in {alive, cooldown, dead, neterr, skip}."""
    verdict, code, hint, _ = await probe_with_headers(provider, plain_key, account_id)
    return verdict, code, hint


async def probe_with_headers(
    provider: str, plain_key: str, account_id: str | None = None
) -> tuple[str, int, str, dict[str, str]]:
    """Same as probe() but also returns the provider's response headers — used
    by the key-create flow to extract published rate limits via
    extract_quota_headers(). Empty dict on network error.

    An UNPROBEABLE key (no probe configured for the provider, or a cloudflare
    key missing its account_id) returns the neutral verdict "skip", NOT
    "alive": the old force-"alive" default made the monitor RESURRECT a
    dead/revoked key of any unprobed provider on every sweep (is_alive=True,
    last_error wiped), so it flapped pick→fail→dead→revive forever
    (cloudflare, caught 2026-07-16). "skip" tells the monitor to leave the
    key's state exactly as real traffic left it."""
    cfg = _PROBES.get(provider)
    if cfg is None:
        return "skip", 0, "no probe configured", {}

    req = cfg(plain_key, account_id)
    if req is None:
        return "skip", 0, "unprobeable key (missing account_id)", {}
    method, url, headers, body = req
    try:
        async with httpx.AsyncClient(timeout=PROBE_TIMEOUT_S) as c:
            r = await c.request(method, url, headers=headers, json=body)
    except Exception as e:
        return "neterr", 0, f"{type(e).__name__}: {e}", {}

    # Defensive — tests mock httpx Response with AsyncMock; dict() then chokes
    # on the coroutine wrapped .keys(). Production httpx is fine either way.
    try:
        h = dict(r.headers)
    except (TypeError, ValueError):
        h = {}
    b = r.text.lower()
    if 200 <= r.status_code < 300:
        return "alive", r.status_code, "", h
    if r.status_code == 429:
        return "cooldown", 429, "rate limit", h
    if r.status_code in (401, 403):
        if "insufficient" in b or "balance" in b or "payment" in b:
            return "dead", r.status_code, "no funds", h
        # mistral's bare 401 "Unauthorized" on our accounts = monthly Vibe-plan
        # quota exhausted, not a revoked key (see llm_service / cooldown). Treat
        # it as a cooldown (key stays alive, cooled to the billing-cycle reset
        # by the monitor's monthly branch), so the probe doesn't re-kill a key
        # the request path correctly cooled.
        if provider == "mistral":
            return "cooldown", r.status_code, "monthly quota", h
        return "dead", r.status_code, "auth failed", h
    if r.status_code == 402:
        return "dead", 402, "payment required", h
    return "alive", r.status_code, "uncertain", h


# Headers different providers use to advertise their rate limits. Mostly
# OpenAI-compat (x-ratelimit-limit-{requests,tokens}); Anthropic has its own
# prefix; Gemini/Cohere/Voyage don't expose useful daily limits in headers.
_DAY_VARIANTS = (
    "-day", "-1d", "-daily", "",   # ascending specificity
)


def _read_int(headers: dict[str, str], *keys: str) -> int | None:
    """Pull the first key present that parses to a positive int."""
    h = {k.lower(): v for k, v in headers.items()}
    for k in keys:
        v = h.get(k.lower())
        if not v:
            continue
        try:
            n = int(str(v).strip())
            if n > 0:
                return n
        except (TypeError, ValueError):
            continue
    return None


# "1h33m36s", "547ms", "2400s", "1d" — the provider's own reset-window duration
# strings (groq/OpenAI-compat style). Used to sanity-check whether a bare
# (non -day-suffixed) rate-limit header is actually daily-scoped.
_DURATION_RE = re.compile(
    r"(?:(?P<days>\d+)d)?(?:(?P<hours>\d+)h)?(?:(?P<minutes>\d+)m(?!s))?"
    r"(?:(?P<seconds>\d+(?:\.\d+)?)s)?(?:(?P<millis>\d+)ms)?"
)


def _parse_duration_seconds(value: str) -> float | None:
    m = _DURATION_RE.fullmatch(value.strip()) if value else None
    if not m or not any(m.groups()):
        return None
    return (
        int(m.group("days") or 0) * 86400
        + int(m.group("hours") or 0) * 3600
        + int(m.group("minutes") or 0) * 60
        + float(m.group("seconds") or 0)
        + int(m.group("millis") or 0) / 1000
    )


# A bare limit header is trusted as a DAILY cap only if its own reset window is
# within this margin of 24h. Groq's bare x-ratelimit-limit-tokens resets in
# ~500ms (a rolling TPM bucket) and x-ratelimit-limit-requests in ~1h33m (not a
# day either) — a single key logged 90k-170k tokens/day against an "8000
# tokens/day" reading from this header, instantly red on the dashboard despite
# being perfectly healthy. Requiring a near-24h reset (the provider's OWN
# signal, not a guess) rejects sub-day buckets instead of mis-storing them.
_MIN_DAILY_RESET_S = 20 * 3600


def _read_daily_int(headers: dict[str, str], limit_key: str, reset_key: str) -> int | None:
    """Like `_read_int`, but for a header with NO -day/-1d variant: only trust
    it as daily if `reset_key`'s duration is close to 24h."""
    h = {k.lower(): v for k, v in headers.items()}
    reset_s = _parse_duration_seconds(h.get(reset_key.lower(), ""))
    if reset_s is None or reset_s < _MIN_DAILY_RESET_S:
        return None
    return _read_int(headers, limit_key)


def extract_quota_headers(
    provider: str, headers: dict[str, str]
) -> tuple[int | None, int | None]:
    """Best-effort parse of (requests/day, tokens/day) from provider headers.

    Returns (None, None) when the provider doesn't expose these. Per-provider
    header names cribbed from each provider's docs as of 2026-06-28.
    """
    if provider == "anthropic":
        return (
            _read_int(headers, "anthropic-ratelimit-requests-limit"),
            _read_int(headers, "anthropic-ratelimit-tokens-limit"),
        )
    # OpenAI-compat family (groq, openai, deepseek, mistral, openrouter, cerebras,
    # sambanova — confirmed same x-ratelimit-limit-requests-day header live 2026-07-04)
    if provider in ("cerebras", "groq", "openai", "deepseek",
                     "mistral", "openrouter", "sambanova"):
        req = _read_int(headers, "x-ratelimit-limit-requests-day",
                          "x-ratelimit-limit-requests-1d")
        if req is None:
            req = _read_daily_int(headers, "x-ratelimit-limit-requests",
                                    "x-ratelimit-reset-requests")
        tok = _read_int(headers, "x-ratelimit-limit-tokens-day",
                          "x-ratelimit-limit-tokens-1d")
        if tok is None:
            tok = _read_daily_int(headers, "x-ratelimit-limit-tokens",
                                    "x-ratelimit-reset-tokens")
        # cerebras' requests-day header (2400 for gpt-oss-120b) isn't a hard
        # cap — a single key logged 4,866 req without a 429. It meters on
        # tokens, so drop the req axis to avoid a false >100% on the dashboard.
        if provider == "cerebras":
            req = None
        return req, tok
    # gemini / cohere / voyage — no documented daily-limit headers
    return None, None


def _bearer(k: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {k}", "content-type": "application/json"}


_PROBES = {
    "cerebras": lambda k, _acc=None: ("POST", "https://api.cerebras.ai/v1/chat/completions",
                            _bearer(k),
                            {"model": "gpt-oss-120b",
                             "messages": [{"role": "user", "content": "."}],
                             "max_tokens": 1}),
    "groq": lambda k, _acc=None: ("POST", "https://api.groq.com/openai/v1/chat/completions",
                        _bearer(k),
                        {"model": "openai/gpt-oss-120b",
                         "messages": [{"role": "user", "content": "."}],
                         "max_tokens": 1}),
    "openrouter": lambda k, _acc=None: ("POST", "https://openrouter.ai/api/v1/chat/completions",
                              _bearer(k),
                              {"model": "google/gemma-4-31b-it:free",  # gpt-oss:free delisted 2026-07-16
                               "messages": [{"role": "user", "content": "."}],
                               "max_tokens": 1}),
    # deepseek-chat (matches DEFAULT_MODEL — reverted from v4-flash, which is a
    # reasoning model that broke our JSON replies; see litellm_adapter). Watch
    # the ~2026-07-24 deprecation — swap the probe with the model.
    "deepseek": lambda k, _acc=None: ("POST", "https://api.deepseek.com/chat/completions",
                            _bearer(k),
                            {"model": "deepseek-chat",
                             "messages": [{"role": "user", "content": "."}],
                             "max_tokens": 1}),
    "anthropic": lambda k, _acc=None: ("POST", "https://api.anthropic.com/v1/messages",
                             {"x-api-key": k, "anthropic-version": "2023-06-01",
                              "content-type": "application/json"},
                             {"model": "claude-haiku-4-5", "max_tokens": 1,
                              "messages": [{"role": "user", "content": "."}]}),
    # Key goes in the x-goog-api-key header, NOT the URL query string — a key in
    # the URL can leak into any proxy/exception that renders the request URL.
    "gemini": lambda k, _acc=None: ("POST",
                          "https://generativelanguage.googleapis.com/v1beta/models/"
                          "gemini-2.5-flash:generateContent",
                          {"content-type": "application/json", "x-goog-api-key": k},
                          {"contents": [{"parts": [{"text": "."}]}],
                           "generationConfig": {"maxOutputTokens": 1}}),
    # voyage-4, NOT voyage-3: the voyage-3 family has zero free-token allocation
    # (real $ from token 1 — see litellm_adapter migration 2026-07-07), so a
    # probe on voyage-3 billed real money every monitor sweep. voyage-4 has the
    # 200M/month free allocation.
    "voyage": lambda k, _acc=None: ("POST", "https://api.voyageai.com/v1/embeddings",
                          _bearer(k),
                          {"model": "voyage-4", "input": "."}),
    "mistral": lambda k, _acc=None: ("POST", "https://api.mistral.ai/v1/chat/completions",
                           _bearer(k),
                           {"model": "mistral-small-latest",
                            "messages": [{"role": "user", "content": "."}],
                            "max_tokens": 1}),
    # Cohere v2 (/v2/chat). command-r was retired 2025-09-15; use the small
    # current model for probes — it's cheapest and most likely to stay live.
    "cohere": lambda k, _acc=None: ("POST", "https://api.cohere.com/v2/chat",
                          _bearer(k),
                          {"model": "command-r7b-12-2024",
                           "messages": [{"role": "user", "content": "."}],
                           "max_tokens": 1}),
    # 2026-07-04: confirmed live — 200 OK + x-ratelimit-limit-requests-day header.
    "sambanova": lambda k, _acc=None: ("POST", "https://api.sambanova.ai/v1/chat/completions",
                             _bearer(k),
                             {"model": "Meta-Llama-3.3-70B-Instruct",
                              "messages": [{"role": "user", "content": "."}],
                              "max_tokens": 1}),
    # openai — probe with the cheapest current model. A revoked key 401s
    # (correctly dead); a live key returns 200/429 (alive).
    "openai": lambda k, _acc=None: ("POST", "https://api.openai.com/v1/chat/completions",
                          _bearer(k),
                          {"model": "gpt-4o-mini",
                           "messages": [{"role": "user", "content": "."}],
                           "max_tokens": 1}),
    # Probe with nemotron — the ONLY confirmed-live nvidia model (it's the
    # chat:deep default). It used to probe kimi-k2.6, but that model now 404s
    # "Function not found for account" (removed from routing 2026-07-10), and a
    # 404 fell through to the "alive/uncertain" catch-all — so a genuinely
    # revoked nvidia key read as alive and never got flagged. nemotron is slow
    # to GENERATE (~27s), but a revoked key 401s on auth *before* generation, so
    # dead keys are still detected fast; only a live key's probe runs long.
    "nvidia": lambda k, _acc=None: ("POST", "https://integrate.api.nvidia.com/v1/chat/completions",
                          _bearer(k),
                          {"model": "nvidia/nemotron-3-ultra-550b-a55b",
                           "messages": [{"role": "user", "content": "."}],
                           "max_tokens": 1}),
    # 2026-07-05: confirmed live — 200 OK on glm-4.5-flash (the only free
    # model on this account; glm-4.5/glm-4.5-air 429 with "Insufficient
    # balance", so the probe deliberately targets the confirmed-free model).
    "zai": lambda k, _acc=None: ("POST", "https://api.z.ai/api/paas/v4/chat/completions",
                        _bearer(k),
                        {"model": "glm-4.5-flash",
                         "messages": [{"role": "user", "content": "."}],
                         "max_tokens": 1}),
    # cloudflare needs the account-scoped URL (same reason as the adapter's
    # api_base — the account ID rides in the path, not a header). Without a
    # probe here, probe_with_headers' old force-"alive" default resurrected
    # dead cloudflare keys every sweep (2026-07-16). A key with no account_id
    # can't be called at all → None → "skip" verdict, state left unchanged.
    "cloudflare": lambda k, acc=None: None if not acc else (
        "POST",
        f"https://api.cloudflare.com/client/v4/accounts/{acc}/ai/v1/chat/completions",
        _bearer(k),
        {"model": "@cf/openai/gpt-oss-120b",
         "messages": [{"role": "user", "content": "."}],
         "max_tokens": 1}),
}


async def probe_all(
    keys: list[tuple[int, str, str, str | None]],
) -> dict[int, tuple[str, int, str]]:
    """keys: list of (api_key_id, provider, plain_token, account_id)."""
    sem = asyncio.Semaphore(8)

    async def one(kid: int, provider: str, plain: str, account_id: str | None):
        async with sem:
            return kid, await probe(provider, plain, account_id)

    out: dict[int, tuple[str, int, str]] = {}
    tasks = [one(kid, p, k, acc) for kid, p, k, acc in keys]
    for coro in asyncio.as_completed(tasks):
        kid, result = await coro
        out[kid] = result
    return out
