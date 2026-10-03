"""Transports — the small Protocols every provider call goes through.

A *transport* is how a model is actually called: litellm for most providers, raw
HTTP for self-hosted / special endpoints. Each transport implements only the
Protocols it serves. Which transport serves a model is `ModelSpec.transport` in
the provider registry; `call_llm` / `embed` / `transcribe` / `decide` below
dispatch through it, so no caller special-cases a provider.

Return convention for every method: `(payload, meta)` where meta carries at least
model, model_served, tokens_in, tokens_out, cost_usd, latency_ms.
"""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from aibroker.providers.registry import model_spec


@runtime_checkable
class ChatTransport(Protocol):
    async def chat(self, *, model: str, messages: list[dict[str, Any]], api_key: str,
                   **_options: Any) -> tuple[str, dict[str, Any]]: ...


@runtime_checkable
class EmbedTransport(Protocol):
    async def embed(self, *, model: str, texts: list[str], api_key: str,
                    ) -> tuple[list[list[float]], dict[str, Any]]: ...


@runtime_checkable
class TranscribeTransport(Protocol):
    async def transcribe(self, *, model: str, audio: bytes, filename: str,
                         api_key: str) -> tuple[str, dict[str, Any]]: ...


@runtime_checkable
class DecideTransport(Protocol):
    async def decide(self, *, model: str, state: str, questions: dict[str, Any],
                     api_key: str) -> tuple[dict[str, Any], dict[str, Any]]: ...


_transports: dict[str, Any] = {}


def _load() -> dict[str, Any]:
    """Build the name -> instance table on first use (heavy imports: litellm, httpx)."""
    if not _transports:
        from aibroker.providers.decisions import OpenRouterDecisions
        from aibroker.providers.gemini_asr import GeminiAsrTransport
        from aibroker.providers.litellm_client import (
            LiteLLMChatAudioTransport,
            LiteLLMTransport,
        )
        from aibroker.providers.local_asr import LocalAsrTransport
        from aibroker.providers.local_vision import LocalVisionTransport
        _transports.update({
            "litellm": LiteLLMTransport(),
            "litellm_chat_audio": LiteLLMChatAudioTransport(),
            "local_vision": LocalVisionTransport(),
            "local_asr": LocalAsrTransport(),
            "gemini_asr": GeminiAsrTransport(),
            "openrouter_decisions": OpenRouterDecisions(),
        })
    return _transports


def transport_for(model: str) -> Any:
    """The transport serving `model`. A model the registry does not list (a
    caller-pinned litellm string) is served by litellm, as it always was."""
    spec = model_spec(model)
    return _load()[spec.transport if spec else "litellm"]


async def call_llm(
    *, model: str, messages: list[dict[str, Any]], api_key: str,
    max_tokens: int = 1024, temperature: float = 0.7,
    response_format: dict[str, Any] | None = None,
    extra: dict[str, Any] | None = None, timeout: float | None = None,
    capability: str | None = None, tools: list[dict[str, Any]] | None = None,
    tool_choice: Any = None, cache_key: str | None = None,
) -> tuple[str, dict[str, Any]]:
    """Chat call through the model's transport. Returns (text, meta).

    `timeout` (seconds) caps one provider call so a hung upstream cannot consume
    the caller's whole budget; `capability` lets an adapter pick per-lane
    trade-offs; `cache_key` is the stable prompt-cache key (sent only to
    providers that document one)."""
    if tools:
        from aibroker.services.tool_contract import TOOL_PROVIDERS
        if model.split("/", 1)[0] not in TOOL_PROVIDERS or response_format:
            raise ValueError("incompatible native tool request")
    return await transport_for(model).chat(
        model=model, messages=messages, api_key=api_key, max_tokens=max_tokens,
        temperature=temperature, response_format=response_format, extra=extra,
        timeout=timeout, capability=capability, tools=tools,
        tool_choice=tool_choice, cache_key=cache_key)


async def embed(*, model: str, texts: list[str], api_key: str,
                ) -> tuple[list[list[float]], dict[str, Any]]:
    return await transport_for(model).embed(model=model, texts=texts, api_key=api_key)


async def transcribe(*, model: str, audio: bytes, filename: str,
                     api_key: str) -> tuple[str, dict[str, Any]]:
    """Audio -> text through the model's transport. `filename` carries the
    extension so the format is inferred."""
    return await transport_for(model).transcribe(
        model=model, audio=audio, filename=filename, api_key=api_key)


async def decide(*, model: str, state: str, questions: dict[str, Any],
                 api_key: str) -> tuple[dict[str, Any], dict[str, Any]]:
    return await transport_for(model).decide(
        model=model, state=state, questions=questions, api_key=api_key)
