"""Provider registration in the historical order.

Definitions live in provider_specs; the existing registration order is kept
for compatibility with consumers that traverse the registry.
"""
from __future__ import annotations

from aibroker.providers.provider_specs import (
    anthropic,
    cerebras,
    cloudflare,
    cohere,
    deepseek,
    gemini,
    groq,
    local,
    mistral,
    nvidia,
    openai,
    openrouter,
    sambanova,
    voyage,
    zai,
)
from aibroker.providers.registry import register

register(cerebras.SPEC)
register(groq.SPEC)
register(gemini.SPEC)
register(sambanova.SPEC)
register(zai.SPEC)
register(cloudflare.SPEC)
register(openrouter.SPEC)
register(mistral.SPEC)
register(cohere.SPEC)
register(nvidia.SPEC)
register(voyage.SPEC)
register(local.SPEC)
register(deepseek.SPEC)
register(anthropic.SPEC)
register(openai.SPEC)
