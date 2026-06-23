"""Capability → provider fallback chains.

This is the only place where 'when LLM call needs chat:fast, try these
providers in this order' is defined. Selector just walks the chain.
"""
from __future__ import annotations

from typing import Literal

Capability = Literal[
    "chat:fast",
    "chat:smart",
    "chat:code",
    "structured",
    "vision",
    "embedding",
    "prefilter",
]


CAPABILITY_CHAINS: dict[Capability, list[str]] = {
    "chat:fast": [
        "cerebras", "groq", "gemini",
        "deepseek",
        "openrouter", "sambanova", "nvidia", "mistral",
        "anthropic",
        "openai",
    ],
    "chat:smart": [
        "cerebras", "groq", "gemini", "sambanova",
        "anthropic",
        "openrouter", "nvidia", "mistral",
        "openai", "deepseek",
    ],
    "chat:code": [
        "cerebras", "groq", "openrouter", "gemini", "nvidia", "sambanova",
        "anthropic",
        "deepseek", "openai",
    ],
    "prefilter": [
        "cerebras", "groq", "gemini", "sambanova", "nvidia",
        "openrouter", "mistral",
    ],
    "structured": [
        "cerebras", "groq", "gemini",
        "openrouter", "sambanova", "nvidia", "mistral",
        "anthropic", "openai",
    ],
    "vision": ["gemini", "anthropic", "openai"],
    "embedding": ["voyage"],
}


def chain_for(capability: Capability) -> list[str]:
    """Return providers in fallback order for `capability`."""
    if capability not in CAPABILITY_CHAINS:
        raise ValueError(f"unknown capability: {capability}")
    return list(CAPABILITY_CHAINS[capability])
