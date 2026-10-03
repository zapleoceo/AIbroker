"""Capability → provider chain + required scope.

Single source of truth for two questions:
  - "for capability X, in what order do we try providers?"  → CAPABILITY_CHAINS
  - "which scope must a key carry to serve capability X?"     → CAPABILITY_SCOPE

This is POLICY (order). Provider FACTS (models, JSON reliability, paid or not,
quotas…) live in the provider registry — providers/registry.py. Dated rationale
for every ordering decision: docs/history/provider-choices.md.

Routes and the selector import from here; never duplicate these tables.
"""
from __future__ import annotations

from typing import Literal

from aibroker.providers.registry import (
    model_for,
    paid_providers,
    providers_with_json,
    spec_or_default,
)

Capability = Literal[
    "chat:fast",
    "chat:smart",
    "chat:sales",
    "chat:code",
    "chat:edit",
    "chat:deep",
    "structured",
    "vision",
    "transcription",
    "embedding",
    "prefilter",
    "translate",
    "decision",
]


CAPABILITY_CHAINS: dict[Capability, list[str]] = {
    # chat:fast is FREE-ONLY (triage / simple follow-ups tolerate a retry when the
    # free pool is dry); every paid dollar is reserved for the money lanes.
    "chat:fast": ["cerebras", "groq", "gemini", "sambanova", "zai", "cloudflare"],
    # Money lane: free gemini first (measured faster, never truncates), deepseek as
    # the paid cache-warm fallback, free DeepSeek-V3.2 on sambanova, anthropic last.
    "chat:smart": ["gemini", "deepseek", "sambanova", "anthropic"],
    # Smart-LLM sales: Sonnet leads on its own daily-capped key (owner-approved
    # exception to free-first); deepseek cheap paid fallback; free tail after.
    "chat:sales": ["anthropic", "gemini", "deepseek", "sambanova"],
    "chat:code": ["cerebras", "groq", "openrouter", "gemini", "sambanova",
                  "cloudflare", "anthropic", "deepseek", "openai"],
    # Coach editor: JSON-reliable providers ONLY (a malformed edit breaks Coach).
    "chat:edit": ["gemini", "deepseek", "anthropic"],
    # Long-context / async reasoning (1M ctx Nemotron); single provider by design.
    "chat:deep": ["nvidia"],
    # Always JSON -> no zai (no response_format); cerebras' only model was deleted.
    "prefilter": ["groq", "gemini", "openrouter", "sambanova", "cloudflare"],
    # Small fast NON-reasoning models first (gpt-oss "thinks" ~16s on one phrase).
    "translate": ["gemini", "groq"],
    "structured": ["groq", "gemini", "anthropic", "openai"],
    # Self-hosted Qwen3-VL first (no fast free cloud alternative exists), then the
    # free cloud pools; paid tail is final-retry only (see FREE_WALK_CAPABILITIES).
    "vision": ["local", "gemini", "sambanova", "openrouter", "deepseek", "openai"],
    # groq first (fastest, free); gemini-3.5-transcribe next (free tier, 1-12 s);
    # local whisper (~80 s/clip on this host) is the last free backstop. 2026-10-04:
    # if local is never reached over a long period it can be switched off for good.
    "transcription": ["groq", "gemini", "local", "openai"],
    # voyage primary; cohere is the fallback when voyage is down.
    "embedding": ["voyage", "cohere"],
    # Typed decisions: OpenRouter is the only host, PAID keys only (see run_decision).
    "decision": ["openrouter"],
}


# Scope a key must carry (api_keys.scopes) to be eligible for a capability.
# Also the scope the calling project must hold. Lets us run a reserved lane:
# a key scoped only to 'llm:edit' is invisible to bot 'llm:chat' traffic.
CAPABILITY_SCOPE: dict[Capability, str] = {
    "chat:fast": "llm:chat",
    "chat:smart": "llm:chat",
    # chat:sales reuses llm:chat: every provider in its chain already carries it,
    # so the lane works with zero re-scoping; cost is bounded by the key's own cap.
    "chat:sales": "llm:chat",
    "chat:code": "llm:chat",
    "chat:edit": "llm:edit",
    "chat:deep": "llm:deep",
    "structured": "llm:chat",
    "prefilter": "llm:chat",
    "translate": "llm:chat",
    "vision": "llm:vision",
    "transcription": "llm:audio",
    "embedding": "llm:embed",
    # Its own scope: the lane is paid and a project must opt in explicitly.
    "decision": "llm:decision",
}


def usable_scopes_for_provider(provider: str) -> frozenset[str]:
    """Scopes this provider can ACTUALLY serve — it must be in the capability's
    chain AND have a model wired for it. Any other scope on its key is inert and
    only misleads the operator (anthropic + `llm:audio`, ...)."""
    return frozenset(
        CAPABILITY_SCOPE[cap]
        for cap, chain in CAPABILITY_CHAINS.items()
        if provider in chain and model_for(provider, cap)
    )


# Capabilities whose regular walk is FREE providers only; the paid tail is
# reachable solely through the job queue's final-retry paid_only escalation
# (vision is a bulk backfill nobody waits on: slow-but-free beats fast-but-paid).
FREE_WALK_CAPABILITIES: frozenset[str] = frozenset({"vision"})


def free_first_walk(capability: str, chain: list[str], *, paid_only: bool) -> list[str]:
    """`chain` minus the paid providers when `capability` is free-walk and
    this is not the final paid_only attempt; otherwise `chain` unchanged."""
    if paid_only or capability not in FREE_WALK_CAPABILITIES:
        return chain
    paid = paid_providers()
    return [p for p in chain if p not in paid]


def has_paid_tail(capability: Capability) -> bool:
    """True if `capability`'s chain reaches a paid provider with a wired model —
    the only case where the job queue's final-retry paid_only escalation can do
    anything."""
    paid = paid_providers()
    return any(
        p in paid and model_for(p, capability)
        for p in CAPABILITY_CHAINS.get(capability, [])
    )


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


def deprioritize_for_json(chain: list[str]) -> list[str]:
    """Shape `chain` for a JSON request: drop providers that can never return
    usable JSON (json_reliability="incapable"), then stable-partition the rest —
    reliable first, "unreliable" after (a maybe-malformed retry still beats a
    503). Plain-text requests never come through here."""
    incapable = providers_with_json("incapable")
    unreliable = providers_with_json("unreliable")
    capable = [p for p in chain if p not in incapable]
    return ([p for p in capable if p not in unreliable]
            + [p for p in capable if p in unreliable])


def deprioritize_deepseek_for_savings(chain: list[str], *, should_defer: bool) -> list[str]:
    """Sink deepseek behind any FREE provider that already follows it in
    `chain`, when `should_defer` is True — the caller decides why (deepseek's
    peak-hour 2x surcharge, a big-JSON prompt that empties its body, or a
    provider-side empty-body storm). Only deepseek and the providers AFTER it
    move: anything deliberately placed AHEAD of deepseek (chat:sales leads with
    anthropic Sonnet) keeps its lead, so this is safe to apply to every chain."""
    if not should_defer or "deepseek" not in chain:
        return chain
    idx = chain.index("deepseek")
    prefix, suffix = chain[:idx], chain[idx + 1:]
    paid = paid_providers()
    free_after = [p for p in suffix if p not in paid]
    if not free_after:
        return chain  # no free provider follows deepseek — nothing to gain
    paid_after = [p for p in suffix if p in paid]
    return [*prefix, *free_after, "deepseek", *paid_after]


def provider_rank(provider: str) -> int:
    """Fixed provider order for tie-breaking (ProviderSpec.rank)."""
    return spec_or_default(provider).rank
