"""Cheapest possible call per provider — used by monitor to check key liveness."""
from __future__ import annotations

import asyncio
import logging

import httpx

log = logging.getLogger(__name__)


PROBE_TIMEOUT_S = 15


async def probe(provider: str, plain_key: str) -> tuple[str, int, str]:
    """Returns (verdict, http_status, hint). verdict in {alive, cooldown, dead, neterr}."""
    cfg = _PROBES.get(provider)
    if cfg is None:
        return "alive", 0, "no probe configured"

    method, url, headers, body = cfg(plain_key)
    try:
        async with httpx.AsyncClient(timeout=PROBE_TIMEOUT_S) as c:
            r = await c.request(method, url, headers=headers, json=body)
    except Exception as e:
        return "neterr", 0, f"{type(e).__name__}: {e}"

    b = r.text.lower()
    if 200 <= r.status_code < 300:
        return "alive", r.status_code, ""
    if r.status_code == 429:
        return "cooldown", 429, "rate limit"
    if r.status_code in (401, 403):
        if "insufficient" in b or "balance" in b or "payment" in b:
            return "dead", r.status_code, "no funds"
        return "dead", r.status_code, "auth failed"
    if r.status_code == 402:
        return "dead", 402, "payment required"
    return "alive", r.status_code, "uncertain"


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
