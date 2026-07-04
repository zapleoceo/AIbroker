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
    "chat:deep",
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
        # 2026-07-04: sambanova (20 req/day/key) and github (~150 req/day/key
        # on gpt-4o-mini, Free Copilot tier) both confirmed live — DEFAULT_MODEL
        # + health probe + prod key test. Tail position, pure extra breadth;
        # github first (more headroom per key).
        "github", "sambanova",
    ],
    "chat:smart": [
        "cerebras", "groq", "gemini",
        "mistral", "cohere",
        "anthropic",
        "openrouter",
        "openai", "deepseek",
        "github", "sambanova",
    ],
    "chat:code": [
        "cerebras", "groq", "openrouter", "gemini",
        "mistral",
        "anthropic",
        "deepseek", "openai",
        "github", "sambanova",
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
    # 2026-07-04: long-context / async reasoning lane. nvidia's Nemotron 3
    # Ultra (nvidia_nim/nvidia/nemotron-3-ultra-550b-a55b) is a 550B MoE with a
    # real 1M-token context (95% RULER@1M) and strong agentic benchmarks
    # (91% PinchBench), but is SLOW (~27s for 5 output tokens on the free,
    # oversubscribed pool in a live test) — unfit for any latency-sensitive
    # chain above. Gated behind its own scope (llm:deep) so it's never reached
    # by normal chat traffic; callers who don't need 1M context or can't
    # tolerate long waits should keep using chat:smart. Single-provider chain
    # (no other free provider offers this context length) — a miss here falls
    # straight to a 503, by design.
    "chat:deep": ["nvidia"],
    "prefilter": [
        "cerebras", "groq", "gemini",
        "mistral", "cohere",
        "openrouter",
        "github", "sambanova",
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
    # 2026-07-01: cerebras dropped. Its gpt-oss returns HTTP-200 but malformed
    # JSON on structured requests (~4.6k/wk InvalidJSON) — every one wasted a
    # pick and fell through. groq (same model) does not exhibit this at volume,
    # so it stays.
    "structured": [
        "groq", "gemini",
        "mistral", "cohere",
        "openrouter",
        "anthropic", "openai",
    ],
    # 2026-07-01: anthropic dropped from vision. gemini's free tier is
    # RPM-capped, so vision fell to anthropic ~1.4k/wk — every call 400'd with
    # "Unable to download the file": Vera passes image URLs anthropic's fetcher
    # can't reach (gemini could). The key/model are fine (chat/structured work);
    # this is a vision image-passing issue. Re-add anthropic here once the
    # caller sends images as base64 rather than a fetch-gated URL. openai is the
    # working paid fallback when gemini is exhausted.
    "vision": ["gemini", "openai"],
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
    "chat:deep": "llm:deep",
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


# Providers whose default model emits malformed JSON at a meaningful rate on
# structured/JSON requests: cerebras gpt-oss (~4.6k/wk InvalidJSON before it was
# pulled from `structured`), cohere command-r7b, and openrouter's gpt-oss:free.
# groq runs the same gpt-oss but shows ~0 InvalidJSON at volume (grammar-
# constrained JSON mode), so it stays reliable. mistral-small is borderline but
# is our free workhorse (≈0 on chat), so it's not demoted. Kept in the chain as
# a last resort (a maybe-malformed retry still beats a 503) but pushed behind
# the JSON-reliable providers whenever the caller asks for JSON.
JSON_UNRELIABLE_PROVIDERS: frozenset[str] = frozenset(
    {"cerebras", "cohere", "openrouter"}
)


def deprioritize_for_json(chain: list[str]) -> list[str]:
    """Stable-partition `chain`: JSON-reliable providers first (original order),
    then the JSON_UNRELIABLE_PROVIDERS (original order). Never drops one, so a
    JSON request still reaches every provider — just tries the reliable ones
    first, cutting InvalidJSON waste at the source instead of after the fact."""
    reliable = [p for p in chain if p not in JSON_UNRELIABLE_PROVIDERS]
    unreliable = [p for p in chain if p in JSON_UNRELIABLE_PROVIDERS]
    return reliable + unreliable
