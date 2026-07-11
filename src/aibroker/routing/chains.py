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
    # 2026-07-05: strict free-first — paid (deepseek/anthropic/openai) moved
    # to the tail of every chat:* chain, after ALL free providers including
    # github/sambanova/zai. Was: deepseek sat ahead of openrouter/github/
    # sambanova/zai "for backfill speed" — but that meant a paid call fired
    # the moment the first 5 free providers were saturated, even though 3+
    # more free providers (all confirmed live) were still untried further
    # down the chain. Explicit choice: slow-but-free beats fast-but-paid.
    "chat:fast": [
        "cerebras", "groq", "gemini",
        "mistral", "cohere",
        "openrouter",
        "sambanova", "zai",
        # 2026-07-07: cloudflare (gpt-oss-120b) — confirmed live with the
        # real strict Vera triage json_schema, valid JSON, ~1.6s. Previously
        # idle capacity (only vision was wired).
        "cloudflare",
        # 2026-07-10: nvidia REMOVED from chat:fast. Its kimi-k2.6 model (wired
        # 2026-07-05) went dead — every call now 404s "Function 'x': Not found
        # for account" (confirmed live, ~30 errors/hr). The model vanished from
        # our account's provisioning even though it's still in NVIDIA's catalog
        # listing. nvidia STAYS in chat:deep (nemotron, confirmed still alive).
        # 2026-07-10: anthropic re-added — balance topped up (was removed while
        # out of credit "credit balance is too low"). Quality paid fallback at
        # the very tail, reached only after deepseek.
        "deepseek", "anthropic", "openai",
    ],
    "chat:smart": [
        "cerebras", "groq", "gemini",
        "mistral", "cohere",
        "openrouter",
        "sambanova",
        # 2026-07-10: nvidia REMOVED from chat:smart. Its deepseek-v4-pro model
        # now times out on 100% of calls (~91s wall, confirmed live, past our
        # 60s ceiling) — the free NVIDIA pool is oversubscribed. Pure wasted
        # attempts + timeout waits. Stays in chat:deep (nemotron alive).
        # 2026-07-10: cloudflare added — extra free gpt-oss-120b burst (see
        # DEFAULT_MODEL), tried before the paid tail. anthropic re-added (balance
        # topped up) as the quality paid fallback.
        "cloudflare",
        "anthropic", "openai", "deepseek",
    ],
    "chat:code": [
        "cerebras", "groq", "openrouter", "gemini",
        "mistral",
        "sambanova",
        "cloudflare",
        # 2026-07-10: anthropic re-added (balance topped up).
        "anthropic", "deepseek", "openai",
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
    # 2026-07-10: anthropic re-added (balance topped up) — Coach's top-quality
    # JSON fallback after gemini (free, thinking-disabled) and deepseek.
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
        "sambanova", "zai",
        "cloudflare",
    ],
    # Trivial utility task (message translation): does NOT need premium/reasoning
    # models. Put SMALL FAST non-reasoning models FIRST — cerebras/groq gpt-oss is a
    # REASONING model that "thinks" for ~16s even on one short phrase (starved the
    # 15s client timeout → translate button failed). cohere-r7b / mistral-small /
    # gemini-flash answer in ~1-2s. Also uses the models the bot's reply chains reach
    # LAST, so translation barely competes with live replies for keys.
    # 2026-07-10: cerebras (gemma-4-31b) added FIRST — a fast non-reasoning model
    # (unlike cerebras gpt-oss, which was excluded here for its ~16s think time)
    # at cerebras speed, free. Translate is low-volume so cerebras' 5 RPM is fine.
    "translate": [
        "cerebras", "mistral", "gemini", "cohere", "groq",
    ],
    # 2026-07-01: cerebras dropped. Its gpt-oss returns HTTP-200 but malformed
    # JSON on structured requests (~4.6k/wk InvalidJSON) — every one wasted a
    # pick and fell through. groq (same model) does not exhibit this at volume,
    # so it stays.
    "structured": [
        "groq", "gemini",
        "mistral", "cohere",
        "openrouter",
        # 2026-07-10: anthropic re-added (balance topped up).
        "anthropic", "openai",
    ],
    # 2026-07-01: anthropic dropped from vision. gemini's free tier is
    # RPM-capped, so vision fell to anthropic ~1.4k/wk — every call 400'd with
    # "Unable to download the file": Vera passes image URLs anthropic's fetcher
    # can't reach (gemini could). The key/model are fine (chat/structured work);
    # this is a vision image-passing issue. Re-add anthropic here once the
    # caller sends images as base64 rather than a fetch-gated URL. openai is the
    # working paid fallback when gemini is exhausted.
    # 2026-07-04: cloudflare (llava-1.5-7b) tried as tail fallback, then
    # REMOVED same day — a garbage-bytes probe returned 200 (proving
    # auth+connectivity work), but a real base64 data-URL image (the format
    # gemini/openai actually receive here) 400'd with "Unsupported image
    # data": {"code":3010}. Workers AI's llava wants raw byte-array image
    # input, not an OpenAI-style image_url — LiteLLM doesn't convert between
    # them for cloudflare. Would be dead weight in the chain (always fails)
    # until that conversion is written. DEFAULT_MODEL/quotas/cooldown/probe
    # entries stay (same "known but not chained" treatment as github before
    # its own prod key test — see docs/routing.md).
    "vision": ["gemini", "cloudflare", "openai"],
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
#
# 2026-07-05: zai added — confirmed via
# `litellm.get_supported_openai_params(model="glm-4.5-flash",
# custom_llm_provider="zai")`: no `response_format` in the supported list at
# all. litellm.drop_params=True (broker-wide) SILENTLY strips it on every
# call, so the model never even receives an instruction to emit JSON — a
# 100%-guaranteed InvalidJSON on any JSON-format request, not just "a
# meaningful rate". Confirmed live (request #871336): 200 OK, unparseable
# body, correctly fell through to the next provider per the JSON quality
# gate — but no reason to try it first on JSON requests again.
JSON_UNRELIABLE_PROVIDERS: frozenset[str] = frozenset(
    {"cerebras", "cohere", "openrouter", "zai"}
)


def deprioritize_for_json(chain: list[str]) -> list[str]:
    """Stable-partition `chain`: JSON-reliable providers first (original order),
    then the JSON_UNRELIABLE_PROVIDERS (original order). Never drops one, so a
    JSON request still reaches every provider — just tries the reliable ones
    first, cutting InvalidJSON waste at the source instead of after the fact."""
    reliable = [p for p in chain if p not in JSON_UNRELIABLE_PROVIDERS]
    unreliable = [p for p in chain if p in JSON_UNRELIABLE_PROVIDERS]
    return reliable + unreliable
