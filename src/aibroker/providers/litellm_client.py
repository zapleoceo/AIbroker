"""LiteLLM-backed transport — chat, embeddings and transcription.

LiteLLM knows the wire format for 100+ providers (cerebras, groq, gemini,
anthropic, openrouter, deepseek, voyage…) — we pass `model='provider/x'` and the
API key. Per-provider request quirks live in providers/adapters.py; prices in
providers/cost.py; prompt-cache marks in providers/prompt_cache.py.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import time
from typing import Any

import litellm

from aibroker.config import get_settings
from aibroker.providers import cost as _cost  # noqa: F401 — registers price overrides
from aibroker.providers.adapters import adapter_for
from aibroker.providers.cost import _estimate_audio_seconds, estimate_llm_cost, whisper_cost
from aibroker.providers.model_identity import served_model
from aibroker.providers.pricing import reported_cost
from aibroker.providers.prompt_cache import (
    _cache_tokens,
    _usage_field,
    apply_cache_key,
    apply_prompt_cache,
)

log = logging.getLogger(__name__)

# Broker sends every provider the same kwargs (temperature, response_format…).
# Some providers reject params they don't support instead of ignoring them —
# drop_params tells LiteLLM to strip what a given provider doesn't support
# rather than forward-and-fail. Safe broker-wide default (history: cohere 400'd
# with UnsupportedParamsError on every structured/chat call, ~1.2k/wk).
litellm.drop_params = True


def _reported_model(resp: Any) -> str | None:
    """The model name a provider put in its own response (`.model` on a
    LiteLLM object, `["model"]` on a raw dict). Feeds served_model, which
    drops it when it merely echoes the routing name — see
    providers/model_identity.py."""
    value = resp.get("model") if isinstance(resp, dict) else getattr(resp, "model", None)
    return value if isinstance(value, str) else None



async def litellm_chat(
    *,
    model: str,
    messages: list[dict[str, Any]],
    api_key: str,
    max_tokens: int = 1024,
    temperature: float = 0.7,
    response_format: dict[str, Any] | None = None,
    extra: dict[str, Any] | None = None,
    timeout: float | None = None,
    capability: str | None = None,
    tools: list[dict[str, Any]] | None = None,
    tool_choice: Any = None,
    cache_key: str | None = None,
) -> tuple[str, dict[str, Any]]:
    """Call LiteLLM. Returns (text, meta).

    `meta` contains: model, tokens_in, tokens_out, cost_usd, latency_ms,
    finish_reason, cache_read_tokens, cache_write_tokens.

    `timeout` (seconds) caps a single provider call so one hung upstream can't
    consume the caller's whole budget — without it, a provider that accepts the
    connection but never responds blocks until the client's own read timeout
    fires (a hard 504/abort) instead of the broker cleanly failing over to the
    next key/provider. None = no cap (LiteLLM default).

    `capability` is passed to the provider adapter: one model can serve several
    lanes that want different trade-offs (anthropic's claude-sonnet-5 forces
    JSON via tool-use on most lanes, but keeps its reasoning on chat:sales —
    the two are mutually exclusive). Optional; adapters ignore it by default.
    """
    kwargs: dict[str, Any] = {
        "model": model,
        "messages": apply_prompt_cache(model, messages),
        "api_key": api_key,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    if timeout is not None:
        kwargs["timeout"] = timeout
    if response_format:
        kwargs["response_format"] = response_format
    if tools:
        kwargs.update(tools=tools, tool_choice=tool_choice or "auto", drop_params=False)
    # Per-provider request quirks (json_schema downgrade, gemini thinking-off,
    # …) live in one adapter each — see providers/adapters.py. adapter.prepare
    # mutates kwargs in place; the default adapter is a no-op.
    adapter_for(model.split("/", 1)[0]).prepare(model, kwargs, capability)
    apply_cache_key(model.split("/", 1)[0], cache_key, kwargs)
    if extra:
        kwargs.update(extra)
    if tools and (kwargs.get("tools") != tools or kwargs.get("drop_params") is not False):
        raise ValueError("adapter cannot downgrade native tool requests")

    t0 = time.time()
    # 2026-07-07: confirmed live — LiteLLM's own `timeout` kwarg does NOT
    # reliably cut off a hung zai call (observed real completions at 90-180s
    # wall time on a `timeout=60` request, ending in a normal — if
    # JSON-invalid — response, not a TimeoutError). Whatever LiteLLM/the
    # provider plugin does internally with `timeout` isn't enough on its own.
    # Enforce the ceiling ourselves with asyncio.wait_for as a hard backstop —
    # this is what actually protects the attempt budget and the caller's own
    # read timeout (Stepan's chat:fast client + this broker's nginx
    # proxy_read_timeout) from a single hung/slow call.
    if timeout is not None:
        resp = await asyncio.wait_for(litellm.acompletion(**kwargs), timeout=timeout)
    else:
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
    # Post-response provider quirk, the twin of adapter.prepare above: anthropic
    # unwraps LiteLLM's forced-tool JSON envelope here. Applied BEFORE the JSON
    # gate, record_usage and the response cache, so every caller — and every
    # cached copy — gets the clean body rather than each client unwrapping it.
    text = adapter_for(model.split("/", 1)[0]).normalize_json_text(
        text, response_format)
    usage = getattr(resp, "usage", None) or {}
    if isinstance(usage, dict):
        tokens_in = usage.get("prompt_tokens", 0) or 0
        tokens_out = usage.get("completion_tokens", 0) or 0
    else:
        tokens_in = getattr(usage, "prompt_tokens", 0)
        tokens_out = getattr(usage, "completion_tokens", 0)
    cache_read, cache_write = _cache_tokens(usage)
    cost = _prefer_reported_cost(
        model, usage,
        estimate_llm_cost(model, tokens_in, tokens_out,
                              cache_read_tokens=cache_read, cache_write_tokens=cache_write))

    meta = {
        "model": model,
        "model_served": served_model(model, _reported_model(resp)),
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "cost_usd": cost,
        "latency_ms": latency_ms,
        "cache_read_tokens": cache_read,
        "cache_write_tokens": cache_write,
        "finish_reason": (choices[0].finish_reason if choices else None),
        "tool_calls": _native_field(msg, "tool_calls") if choices else None,
        "refusal": _native_field(msg, "refusal") if choices else None,
    }
    return text, meta


def _prefer_reported_cost(model: str, usage: Any, estimated: float) -> float:
    """OpenRouter returns the real charge as usage.cost; use it over the price-table
    estimate (same rule as providers/decisions.py). Other providers report no cost,
    so the estimate stands."""
    if model.split("/", 1)[0] != "openrouter":
        return estimated
    reported = reported_cost(usage)
    return estimated if reported is None else reported


def _native_field(message: Any, name: str) -> Any:
    value = message.get(name) if isinstance(message, dict) else getattr(message, name, None)
    if isinstance(value, list):
        return [item.model_dump(exclude_none=True) if hasattr(item, "model_dump") else item
                for item in value]
    return value


async def _bounded(awaitable: Any, timeout: float, what: str) -> Any:
    """Await with a HARD wall-clock ceiling (asyncio.wait_for), the way litellm_chat
    does. The labelled TimeoutError keeps provider context in the logs and is
    classified as a transient slow key (classify_provider_error)."""
    try:
        return await asyncio.wait_for(awaitable, timeout=timeout)
    except TimeoutError as e:
        raise TimeoutError(f"{what} timed out after {timeout:g}s") from e


async def litellm_embed(
    *, model: str, texts: list[str], api_key: str
) -> tuple[list[list[float]], dict[str, Any]]:
    t0 = time.time()
    timeout = get_settings().EMBED_TIMEOUT_S
    resp = await _bounded(
        litellm.aembedding(model=model, input=texts, api_key=api_key, timeout=timeout),
        timeout, f"embedding {model}")
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
        "model_served": served_model(model, _reported_model(resp)),
        "tokens_in": tokens_in,
        "tokens_out": 0,
        "cost_usd": estimate_llm_cost(model, tokens_in, 0),
        "latency_ms": latency_ms,
    }
    return vectors, meta


# Chat-based transcription providers (audio in via acompletion, not the
# Whisper atranscription endpoint). groq/openai use real Whisper; gemini has no
# Whisper endpoint but its multimodal chat model transcribes audio natively.
_TRANSCRIBE_PROMPT = (
    "Transcribe this audio verbatim in its original language. "
    "Output only the transcription text — no preamble, no translation, no notes."
)
_AUDIO_MIME: dict[str, str] = {
    ".ogg": "audio/ogg", ".oga": "audio/ogg", ".opus": "audio/ogg",
    ".mp3": "audio/mp3", ".m4a": "audio/mp4", ".mp4": "audio/mp4",
    ".wav": "audio/wav", ".aac": "audio/aac", ".flac": "audio/flac",
    ".webm": "audio/webm",
}


def _audio_mime(filename: str) -> str:
    import os
    return _AUDIO_MIME.get(os.path.splitext(filename)[1].lower(), "audio/ogg")


def _audio_chat_messages(audio: bytes, filename: str) -> list[dict[str, Any]]:
    b64 = base64.b64encode(audio).decode()
    return [{"role": "user", "content": [
        {"type": "text", "text": _TRANSCRIBE_PROMPT},
        {"type": "file",
         "file": {"file_data": f"data:{_audio_mime(filename)};base64,{b64}"}},
    ]}]


async def _transcribe_via_chat(
    *, model: str, audio: bytes, filename: str, api_key: str,
) -> tuple[str, dict[str, Any]]:  # pragma: no cover
    kwargs: dict[str, Any] = {
        "model": model, "messages": _audio_chat_messages(audio, filename),
        "api_key": api_key, "temperature": 0, "max_tokens": 2048,
    }
    adapter_for(model.split("/", 1)[0]).prepare(model, kwargs)  # gemini: thinking off
    t0 = time.time()
    timeout = get_settings().TRANSCRIBE_TIMEOUT_S
    resp = await _bounded(litellm.acompletion(timeout=timeout, **kwargs),
                          timeout, f"chat transcription {model}")
    latency_ms = int((time.time() - t0) * 1000)
    usage = getattr(resp, "usage", None)
    tokens_in = getattr(usage, "prompt_tokens", 0) or 0
    tokens_out = getattr(usage, "completion_tokens", 0) or 0
    # Audio dominates a transcription prompt; if the provider does not split the
    # prompt tokens, price ALL of them as audio (safe: over-counts a few text tokens).
    details = getattr(usage, "prompt_tokens_details", None)
    audio_tokens = _usage_field(details, "audio_tokens") if details is not None else 0
    audio_tokens = audio_tokens or tokens_in
    meta = {
        "model": model,
        "model_served": served_model(model, _reported_model(resp)),
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        # Unlike Whisper (per-second, billed elsewhere), chat transcription bills
        # per token — price it so a PAID key's cost cap is honoured (_billed_cost
        # zeroes free-tier keys anyway). Audio tokens count as prompt tokens.
        "cost_usd": estimate_llm_cost(
            model, tokens_in, tokens_out, audio_input_tokens=audio_tokens),
        "latency_ms": latency_ms,
    }
    return (resp.choices[0].message.content or "").strip(), meta


async def litellm_whisper(
    *, model: str, audio: bytes, filename: str, api_key: str,
) -> tuple[str, dict[str, Any]]:
    """Whisper-style transcription (groq/openai) via litellm.atranscription."""
    import io

    t0 = time.time()
    buf = io.BytesIO(audio)
    buf.name = filename   # litellm/openai SDK reads .name for the format
    timeout = get_settings().TRANSCRIBE_TIMEOUT_S
    resp = await _bounded(
        litellm.atranscription(model=model, file=buf, api_key=api_key, timeout=timeout),
        timeout, f"transcription {model}")
    latency_ms = int((time.time() - t0) * 1000)
    # Response is an object with .text (or a dict)
    text = resp.get("text", "") if isinstance(resp, dict) else (getattr(resp, "text", "") or "")
    # Whisper bills per audio-minute, not per token. Prefer the duration the
    # provider reports; fall back to a bitrate estimate from the byte size.
    # Was a flat 0.0 (2026-09-07 review): a PAID whisper key's daily cap was
    # decorative — nothing ever debited it. _billed_cost still zeroes free keys.
    duration = resp.get("duration") if isinstance(resp, dict) else getattr(resp, "duration", None)
    audio_s = float(duration) if duration else _estimate_audio_seconds(len(audio))
    meta = {
        "model": model,
        "model_served": served_model(model, _reported_model(resp)),
        "tokens_in": 0,
        "tokens_out": 0,
        "cost_usd": whisper_cost(model, audio_s),
        "audio_s": round(audio_s, 1),
        "latency_ms": latency_ms,
    }
    return text.strip(), meta



class LiteLLMTransport:
    """Chat + embed + transcribe through litellm (the default transport)."""

    async def chat(self, **kw: Any) -> tuple[str, dict[str, Any]]:
        return await litellm_chat(**kw)

    async def embed(self, **kw: Any) -> tuple[list[list[float]], dict[str, Any]]:
        return await litellm_embed(**kw)

    async def transcribe(self, *, model: str, **kw: Any) -> tuple[str, dict[str, Any]]:
        return await litellm_whisper(model=model, **kw)

    def call_timeout(self, capability: str) -> float | None:
        return None


class LiteLLMChatAudioTransport(LiteLLMTransport):
    """Transcription by a multimodal chat model with the audio inlined."""

    async def transcribe(self, *, model: str, **kw: Any) -> tuple[str, dict[str, Any]]:
        return await _transcribe_via_chat(model=model, **kw)
