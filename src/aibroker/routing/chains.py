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
    # 2026-07-17: deepseek MOVED TO THE HEAD of chat:smart — the one deliberate
    # exception to strict free-first, owner-approved (cap raised $0.50→$1 for
    # it). chat:smart is Stepan's money lane (sales replies): quality beats
    # price there, and routing every reply to ONE strong model (v4-flash)
    # instead of whichever free key happens to be uncooled gives (a) stable
    # answer quality, (b) a warm per-account prompt cache on every call —
    # cache-hit input is $0.0028/M (50×), measured 80-99% hit on repeat reply
    # prompts — so a reply costs ~$0.0003-0.0005, ~$0.4/day at current volume,
    # and (c) independence from the free-pool storms that killed reply latency.
    # Free providers stay as the fallback tail (deepseek flake/EmptyBody on
    # ~50k-char prompts walks over to them; budget-downgrade walks there when
    # the $1 cap is spent). Also pre-positions the lane for 2026-08-17 when
    # cerebras' free tier dies (was ~70% of Stepan's tokens).
    # 2026-07-21: pruned to ONLY providers that give GOOD smart answers (owner
    # request). 7-day quality audit on Stepan's sales-reply JSON:
    #   REMOVED mistral (0 successful smart calls in 7d, keys in AuthError),
    #   cohere command-r7b (25 InvalidJSON / 4 ok = 86% garbage — 7B can't do the
    #   structured reply), openrouter gemma-4-31b (0 ok / 114 rate-limited, ever).
    # KEPT (clean valid answers in prod): deepseek (quality anchor + sticky
    # cache; note ~40% EmptyBody on huge prompts, walks over on empty), the
    # gpt-oss-120b trio cerebras/groq/cloudflare (0-0.4% bad), gemini-2.5-flash
    # (0 empty), sambanova llama-3.3-70b (0 bad when it has quota), and the paid
    # anthropic/openai quality tail. nvidia stays out (2026-07-10: v4-pro 91s
    # timeouts; nemotron only in chat:deep).
    "chat:smart": [
        "deepseek",
        "cerebras", "groq", "gemini",
        "sambanova",
        "cloudflare",
        "anthropic", "openai",
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
    # 2026-07-16: zai removed — prefilter requests are ALWAYS JSON and zai has
    # zero response_format support (see JSON_INCAPABLE_PROVIDERS), so every
    # zai prefilter attempt was a guaranteed billed-but-unusable InvalidJSON.
    "prefilter": [
        "cerebras", "groq", "gemini",
        "mistral", "cohere",
        "openrouter",
        "sambanova",
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
    "vision": ["gemini", "openrouter", "openai"],
    # 2026-07-18: "local" (self-hosted faster-whisper, vera3's asr-local
    # service on the same host) goes FIRST — free, private, no external rate
    # limit, so a transcription request never has to wait on groq's daily
    # Whisper quota or a saturated gemini pool. Falls through to groq/gemini/
    # openai when ASR_LOCAL_URL is unset or the service is unreachable (see
    # litellm_adapter._transcribe_via_local_asr).
    # whisper: groq is free + fast (whisper-large-v3-turbo); openai paid fallback.
    "transcription": ["local", "groq", "gemini", "openai"],
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


def usable_scopes_for_provider(provider: str) -> frozenset[str]:
    """Scopes this provider can ACTUALLY serve — it must be in the capability's
    chain AND have a model wired for it. Any other scope on its key is inert:
    the broker never reaches that provider for that capability, so the checkbox
    only misleads the operator (anthropic + `llm:audio` — Claude has no
    speech-to-text at all; anthropic + `llm:vision` — it HAS a vision model but
    was dropped from the vision chain after 400-ing on image URLs, 2026-07-01).
    Imported lazily: chains is a leaf table module and must not drag litellm in
    at import time — only the dashboard calls this."""
    from aibroker.providers.litellm_adapter import DEFAULT_MODEL
    models = DEFAULT_MODEL.get(provider, {})
    return frozenset(
        CAPABILITY_SCOPE[cap]
        for cap, chain in CAPABILITY_CHAINS.items()
        if provider in chain and cap in models
    )


# Providers billed per-token (a paid-tier key). The job queue's final-retry
# paid_only escalation is only meaningful for a capability whose chain reaches
# one of these with a wired model — otherwise (e.g. chat:deep is nvidia-only)
# demanding a paid key is a guaranteed no-op.
PAID_PROVIDERS: frozenset[str] = frozenset({"deepseek", "anthropic", "openai"})


def has_paid_tail(capability: Capability) -> bool:
    """True if `capability`'s chain reaches a paid provider with a wired model —
    the only case where the job queue's final-retry paid_only escalation can do
    anything. Imported lazily (leaf table module must not drag litellm in at
    import time)."""
    from aibroker.providers.litellm_adapter import model_for
    return any(
        p in PAID_PROVIDERS and model_for(p, capability)
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

# Providers with ZERO response_format support — not "often malformed" but
# structurally incapable of a JSON instruction, so a JSON request to them is a
# 100%-guaranteed billed-but-unusable body. These are EXCLUDED from the
# effective chain on JSON requests (unlike JSON_UNRELIABLE_PROVIDERS, which
# are merely deprioritized — a maybe-malformed retry still beats a 503; a
# certainly-malformed one never does).
#
# 2026-07-05: zai — confirmed via
# `litellm.get_supported_openai_params(model="glm-4.5-flash",
# custom_llm_provider="zai")`: no `response_format` in the supported list at
# all. litellm.drop_params=True (broker-wide) SILENTLY strips it on every
# call, so the model never even receives an instruction to emit JSON.
# Confirmed live (request #871336): 200 OK, unparseable body.
# 2026-07-16: deprioritizing wasn't enough — measured 44 InvalidJSON/45min
# from zai as JSON traffic overflowed to the chain tail, each one a wasted
# billed call. Promoted from deprioritize to exclude.
JSON_INCAPABLE_PROVIDERS: frozenset[str] = frozenset({"zai"})


def deprioritize_for_json(chain: list[str]) -> list[str]:
    """Shape `chain` for a JSON request: drop the JSON_INCAPABLE_PROVIDERS
    (they can never return usable JSON), then stable-partition the rest —
    JSON-reliable providers first (original order), JSON_UNRELIABLE_PROVIDERS
    after (original order). Cuts InvalidJSON waste at the source instead of
    after the wasted call. Plain-text requests never come through here, so
    incapable providers still serve those."""
    capable = [p for p in chain if p not in JSON_INCAPABLE_PROVIDERS]
    reliable = [p for p in capable if p not in JSON_UNRELIABLE_PROVIDERS]
    unreliable = [p for p in capable if p in JSON_UNRELIABLE_PROVIDERS]
    return reliable + unreliable
