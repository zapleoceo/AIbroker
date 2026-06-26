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
    "embedding",
    "prefilter",
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
    # Coach editor (Stepan): JSON-reliable providers only. gemini first
    # (thinking disabled for JSON), anthropic as the cross-provider fallback.
    # Never deepseek here — it breaks structured JSON.
    "chat:edit": ["gemini", "anthropic"],
    "prefilter": [
        "cerebras", "groq", "gemini",
        "mistral", "cohere",
        "openrouter",
    ],
    "structured": [
        "cerebras", "groq", "gemini",
        "mistral", "cohere",
        "openrouter",
        "anthropic", "openai",
    ],
    "vision": ["gemini", "anthropic", "openai"],
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
    "vision": "llm:vision",
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
