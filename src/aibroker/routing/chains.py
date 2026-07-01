"""Capability → provider chain + required scope.

Single source of truth for two questions:
  - "for capability X, in what order do we try providers?"  → CAPABILITY_CHAINS
  - "which scope must a key carry to serve capability X?"     → CAPABILITY_SCOPE

Routes and the selector import from here; never duplicate these tables.
"""
from __future__ import annotations

from typing import Literal

Capability = Literal[
    "chat:fast",
    "chat:smart",
    "chat:code",
    "chat:edit",
    "structured",
    "vision",
    "transcription",
    "embedding",
    "prefilter",
    "translate",
]


CAPABILITY_CHAINS: dict[Capability, list[str]] = {
    "chat:fast": [
        "cerebras", "groq", "gemini",
        "mistral", "cohere",
        "deepseek",
        "openrouter",
        "anthropic",
        "openai",
    ],
    "chat:smart": [
        "cerebras", "groq", "gemini",
        "mistral", "cohere",
        "anthropic",
        "openrouter",
        "openai", "deepseek",
    ],
    "chat:code": [
        "cerebras", "groq", "openrouter", "gemini",
        "mistral",
        "anthropic",
        "deepseek", "openai",
    ],
    # Coach editor (Stepan): JSON-reliable providers ONLY. gemini first
    # (thinking disabled → JSON fits), deepseek the paid fallback that stays
    # available when gemini's quota is exhausted (validate-retry guards its
    # occasional bad JSON), anthropic last (top JSON quality, trial credits).
    # 2026-07-01: narrowed back from [gemini, mistral, cohere, deepseek,
    # anthropic]. mistral-small / cohere-r7b returned Bahasa-drifted and torn
    # JSON when gemini was on cooldown, breaking Coach; the free breadth isn't
    # worth a malformed edit. cerebras/groq/openrouter stay excluded for the
    # same reason.
    "chat:edit": ["gemini", "deepseek", "anthropic"],
    "prefilter": [
        "cerebras", "groq", "gemini",
        "mistral", "cohere",
        "openrouter",
    ],
    # Trivial utility task (message translation): does NOT need premium/reasoning
    # models. Put SMALL FAST non-reasoning models FIRST — cerebras/groq gpt-oss is a
    # REASONING model that "thinks" for ~16s even on one short phrase (starved the
    # 15s client timeout → translate button failed). cohere-r7b / mistral-small /
    # gemini-flash answer in ~1-2s. Also uses the models the bot's reply chains reach
    # LAST, so translation barely competes with live replies for keys.
    "translate": [
        "mistral", "gemini", "cohere", "groq",
    ],
    "structured": [
        "cerebras", "groq", "gemini",
        "mistral", "cohere",
        "openrouter",
        "anthropic", "openai",
    ],
    "vision": ["gemini", "anthropic", "openai"],
    # whisper: groq is free + fast (whisper-large-v3-turbo); openai paid fallback.
    "transcription": ["groq", "openai"],
    # voyage stays primary; cohere as fallback for embed when voyage is down.
    "embedding": ["voyage", "cohere"],
}


# Scope a key must carry (api_keys.scopes) to be eligible for a capability.
# Also the scope the calling project must hold. Lets us run a reserved lane:
# a key scoped only to 'llm:edit' is invisible to bot 'llm:chat' traffic.
CAPABILITY_SCOPE: dict[Capability, str] = {
    "chat:fast": "llm:chat",
    "chat:smart": "llm:chat",
    "chat:code": "llm:chat",
    "chat:edit": "llm:edit",
    "structured": "llm:chat",
    "prefilter": "llm:chat",
    "translate": "llm:chat",
    "vision": "llm:vision",
    "transcription": "llm:audio",
    "embedding": "llm:embed",
}


def is_known_capability(capability: str) -> bool:
    return capability in CAPABILITY_CHAINS


def chain_for(capability: Capability) -> list[str]:
    """Return providers in fallback order for `capability`."""
    if capability not in CAPABILITY_CHAINS:
        raise ValueError(f"unknown capability: {capability}")
    return list(CAPABILITY_CHAINS[capability])


def scope_for(capability: Capability) -> str:
    """Return the scope a key (and project) needs to serve `capability`."""
    if capability not in CAPABILITY_SCOPE:
        raise ValueError(f"unknown capability: {capability}")
    return CAPABILITY_SCOPE[capability]
