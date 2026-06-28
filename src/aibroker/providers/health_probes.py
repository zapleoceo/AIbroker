"""Cheapest possible call per provider — used by monitor to check key liveness."""
from __future__ import annotations

import asyncio
import logging

import httpx

log = logging.getLogger(__name__)


PROBE_TIMEOUT_S = 15


async def probe(provider: str, plain_key: str) -> tuple[str, int, str]:
    """Returns (verdict, http_status, hint). verdict in {alive, cooldown, dead, neterr}."""
    verdict, code, hint, _ = await probe_with_headers(provider, plain_key)
    return verdict, code, hint


async def probe_with_headers(
    provider: str, plain_key: str
) -> tuple[str, int, str, dict[str, str]]:
    """Same as probe() but also returns the provider's response headers — used
    by the key-create flow to extract published rate limits via
    extract_quota_headers(). Empty dict on network error."""
    cfg = _PROBES.get(provider)
    if cfg is None:
        return "alive", 0, "no probe configured", {}

    method, url, headers, body = cfg(plain_key)
    try:
        async with httpx.AsyncClient(timeout=PROBE_TIMEOUT_S) as c:
            r = await c.request(method, url, headers=headers, json=body)
    except Exception as e:
        return "neterr", 0, f"{type(e).__name__}: {e}", {}

    h = dict(r.headers)
    b = r.text.lower()
    if 200 <= r.status_code < 300:
        return "alive", r.status_code, "", h
    if r.status_code == 429:
        return "cooldown", 429, "rate limit", h
    if r.status_code in (401, 403):
        if "insufficient" in b or "balance" in b or "payment" in b:
            return "dead", r.status_code, "no funds", h
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
    # OpenAI-compat family (groq, openai, deepseek, mistral, openrouter, cerebras)
    if provider in ("cerebras", "groq", "openai", "deepseek",
                     "mistral", "openrouter"):
        req = _read_int(
            headers,
            "x-ratelimit-limit-requests-day",
            "x-ratelimit-limit-requests-1d",
            "x-ratelimit-limit-requests",
        )
        tok = _read_int(
            headers,
            "x-ratelimit-limit-tokens-day",
            "x-ratelimit-limit-tokens-1d",
            "x-ratelimit-limit-tokens",
        )
        return req, tok
    # gemini / cohere / voyage — no documented daily-limit headers
    return None, None


def _bearer(k: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {k}", "content-type": "application/json"}


_PROBES = {
    "cerebras": lambda k: ("POST", "https://api.cerebras.ai/v1/chat/completions",
                            _bearer(k),
                            {"model": "gpt-oss-120b",
                             "messages": [{"role": "user", "content": "."}],
                             "max_tokens": 1}),
    "groq": lambda k: ("POST", "https://api.groq.com/openai/v1/chat/completions",
                        _bearer(k),
                        {"model": "openai/gpt-oss-120b",
                         "messages": [{"role": "user", "content": "."}],
                         "max_tokens": 1}),
    "openrouter": lambda k: ("POST", "https://openrouter.ai/api/v1/chat/completions",
                              _bearer(k),
                              {"model": "openai/gpt-oss-120b:free",
                               "messages": [{"role": "user", "content": "."}],
                               "max_tokens": 1}),
    "deepseek": lambda k: ("POST", "https://api.deepseek.com/chat/completions",
                            _bearer(k),
                            {"model": "deepseek-chat",
                             "messages": [{"role": "user", "content": "."}],
                             "max_tokens": 1}),
    "anthropic": lambda k: ("POST", "https://api.anthropic.com/v1/messages",
                             {"x-api-key": k, "anthropic-version": "2023-06-01",
                              "content-type": "application/json"},
                             {"model": "claude-haiku-4-5", "max_tokens": 1,
                              "messages": [{"role": "user", "content": "."}]}),
    "gemini": lambda k: ("POST",
                          f"https://generativelanguage.googleapis.com/v1beta/models/"
                          f"gemini-2.5-flash:generateContent?key={k}",
                          {"content-type": "application/json"},
                          {"contents": [{"parts": [{"text": "."}]}],
                           "generationConfig": {"maxOutputTokens": 1}}),
    "voyage": lambda k: ("POST", "https://api.voyageai.com/v1/embeddings",
                          _bearer(k),
                          {"model": "voyage-3", "input": "."}),
    "mistral": lambda k: ("POST", "https://api.mistral.ai/v1/chat/completions",
                           _bearer(k),
                           {"model": "mistral-small-latest",
                            "messages": [{"role": "user", "content": "."}],
                            "max_tokens": 1}),
    # Cohere v2 (/v2/chat). command-r was retired 2025-09-15; use the small
    # current model for probes — it's cheapest and most likely to stay live.
    "cohere": lambda k: ("POST", "https://api.cohere.com/v2/chat",
                          _bearer(k),
                          {"model": "command-r7b-12-2024",
                           "messages": [{"role": "user", "content": "."}],
                           "max_tokens": 1}),
}


async def probe_all(keys: list[tuple[int, str, str]]) -> dict[int, tuple[str, int, str]]:
    """keys: list of (api_key_id, provider, plain_token)."""
    sem = asyncio.Semaphore(8)

    async def one(kid: int, provider: str, plain: str):
        async with sem:
            return kid, await probe(provider, plain)

    out: dict[int, tuple[str, int, str]] = {}
    tasks = [one(kid, p, k) for kid, p, k in keys]
    for coro in asyncio.as_completed(tasks):
        kid, result = await coro
        out[kid] = result
    return out
