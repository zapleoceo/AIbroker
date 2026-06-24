"""Provider abstraction via LiteLLM SDK.

LiteLLM knows the wire format for 100+ providers (cerebras, groq, gemini,
anthropic, openrouter, deepseek, voyage…) — we just pass `model='provider/x'`
and the API key. No HTTP code in our broker for individual providers.
"""
from __future__ import annotations

import logging
import time
from typing import Any

import litellm

log = logging.getLogger(__name__)

# Map: provider name → default model for each capability.
# Used when caller doesn't pin a model.
DEFAULT_MODEL: dict[str, dict[str, str]] = {
    "cerebras": {"chat:fast": "cerebras/gpt-oss-120b",
                 "chat:smart": "cerebras/gpt-oss-120b",
                 "chat:code": "cerebras/gpt-oss-120b"},
    "groq": {"chat:fast": "groq/openai/gpt-oss-120b",
             "chat:smart": "groq/openai/gpt-oss-120b"},
    "gemini": {"chat:fast": "gemini/gemini-2.5-flash",
               "chat:smart": "gemini/gemini-2.5-pro",
               "vision":     "gemini/gemini-2.5-flash"},
    "deepseek": {"chat:fast": "deepseek/deepseek-chat",
                 "chat:code": "deepseek/deepseek-coder"},
    "openrouter": {"chat:fast": "openrouter/openai/gpt-oss-120b:free",
                   "chat:smart": "openrouter/openai/gpt-oss-120b:free"},
    "anthropic": {"chat:fast": "anthropic/claude-haiku-4-5",
                  "chat:smart": "anthropic/claude-sonnet-4-6",
                  "chat:code":  "anthropic/claude-sonnet-4-6",
                  "vision":     "anthropic/claude-sonnet-4-6"},
    "openai": {"chat:fast": "openai/gpt-5-mini",
               "chat:smart": "openai/gpt-5"},
    "voyage": {"embedding": "voyage/voyage-3"},
}


def model_for(provider: str, capability: str) -> str | None:
    return DEFAULT_MODEL.get(provider, {}).get(capability)


def estimate_llm_cost(model: str, tokens_in: int, tokens_out: int) -> float:
    """LiteLLM exposes accurate per-model pricing — use it for cap math."""
    try:
        return float(
            litellm.completion_cost(
                model=model, prompt_tokens=tokens_in, completion_tokens=tokens_out
            )
        )
    except Exception:
        return 0.0


async def call_llm(
    *,
    model: str,
    messages: list[dict[str, Any]],
    api_key: str,
    max_tokens: int = 1024,
    temperature: float = 0.7,
    response_format: dict[str, Any] | None = None,
    extra: dict[str, Any] | None = None,
) -> tuple[str, dict[str, Any]]:
    """Call LiteLLM. Returns (text, meta).

    `meta` contains: model, tokens_in, tokens_out, cost_usd, latency_ms,
    finish_reason, raw_response (omitted to save context).
    """
    kwargs: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "api_key": api_key,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    if response_format:
        kwargs["response_format"] = response_format
    if extra:
        kwargs.update(extra)

    t0 = time.time()
    resp = await litellm.acompletion(**kwargs)
    latency_ms = int((time.time() - t0) * 1000)

    choices = resp.choices or []
    if choices:
        ch = choices[0]
        msg = getattr(ch, "message", None) or (ch.get("message") if isinstance(ch, dict) else None)
        if isinstance(msg, dict):
            text = msg.get("content") or ""
        else:
            text = getattr(msg, "content", "") or ""
    else:
        text = ""
    usage = getattr(resp, "usage", None) or {}
    if isinstance(usage, dict):
        tokens_in = usage.get("prompt_tokens", 0) or 0
        tokens_out = usage.get("completion_tokens", 0) or 0
    else:
        tokens_in = getattr(usage, "prompt_tokens", 0)
        tokens_out = getattr(usage, "completion_tokens", 0)
    cost = estimate_llm_cost(model, tokens_in, tokens_out)

    meta = {
        "model": model,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "cost_usd": cost,
        "latency_ms": latency_ms,
        "finish_reason": (choices[0].finish_reason if choices else None),
    }
    return text, meta


async def embed(
    *, model: str, texts: list[str], api_key: str
) -> tuple[list[list[float]], dict[str, Any]]:
    t0 = time.time()
    resp = await litellm.aembedding(model=model, input=texts, api_key=api_key)
    latency_ms = int((time.time() - t0) * 1000)
    # LiteLLM may return either objects with .embedding or plain dicts
    data_items = resp.data or []
    vectors: list[list[float]] = []
    for d in data_items:
        if isinstance(d, dict):
            vectors.append(d.get("embedding") or d.get("vector") or [])
        else:
            vectors.append(getattr(d, "embedding", None) or [])
    usage = getattr(resp, "usage", None) or {}
    if isinstance(usage, dict):
        tokens_in = usage.get("prompt_tokens", 0) or usage.get("total_tokens", 0)
    else:
        tokens_in = getattr(usage, "prompt_tokens", 0)
    meta = {
        "model": model,
        "tokens_in": tokens_in,
        "tokens_out": 0,
        "cost_usd": estimate_llm_cost(model, tokens_in, 0),
        "latency_ms": latency_ms,
    }
    return vectors, meta
