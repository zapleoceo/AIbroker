"""Provider abstraction via LiteLLM SDK.

LiteLLM knows the wire format for 100+ providers (cerebras, groq, gemini,
anthropic, openrouter, deepseek, voyage…) — we just pass `model='provider/x'`
and the API key. No HTTP code in our broker for individual providers.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from typing import Any

import litellm

from aibroker.providers.adapters import adapter_for
from aibroker.providers.peak_pricing import peak_multiplier

log = logging.getLogger(__name__)

# Broker sends every provider the same kwargs (temperature, response_format…).
# Some providers reject params they don't support instead of ignoring them —
# cohere 400'd with UnsupportedParamsError on every structured/chat call
# (~1.2k/wk wasted). drop_params tells LiteLLM to strip params a given provider
# doesn't support (per its own param map) rather than forward-and-fail. It only
# drops genuinely-unsupported params, so providers that DO support a param keep
# it — safe broker-wide default.
litellm.drop_params = True

# voyage-4 (our default embedder since 2026-07-07) has no entry in LiteLLM's
# pricing map, so estimate_llm_cost warned "no LiteLLM pricing for
# voyage/voyage-4 — cost recorded as 0" on embed traffic (pure log spam) and
# would have billed a PAID voyage key $0 forever, blinding its daily cost cap.
# Register the list price ($0.06/M input, embeddings have no output cost) so
# the cost path prices it like any other model (2026-07-16).
litellm.register_model({
    "voyage/voyage-4": {
        "input_cost_per_token": 0.00000006,
        "output_cost_per_token": 0.0,
        "litellm_provider": "voyage",
        "mode": "embedding",
    }
})

# Map: provider name → default model per capability. Used when the caller
# doesn't pin a model. Every (provider, capability) that appears in a routing
# chain MUST have an entry here — otherwise the provider is silently skipped.
# Enforced by tests/test_providers.py::test_every_chain_pair_resolves_to_a_model.
_OSS = "gpt-oss-120b"
DEFAULT_MODEL: dict[str, dict[str, str]] = {
    # 2026-07-10: cerebras added gemma-4-31b + zai-glm-4.7 to its free tier.
    # gemma-4-31b is a fast NON-reasoning model (verified: instant, finish=stop) —
    # unlike gpt-oss-120b which "thinks" ~16s even on a one-line prompt. So it's
    # wired for the latency-sensitive utility lanes (translate, prefilter) where
    # cerebras' gpt-oss was previously excluded/slow, giving those a free
    # cerebras-speed option. zai-glm-4.7 evaluated but skipped: it's reasoning
    # like gpt-oss (content=None at low max_tokens), no gain over what we have.
    # chat:* stay on gpt-oss-120b (proven at volume).
    "cerebras": {"chat:fast": f"cerebras/{_OSS}", "chat:smart": f"cerebras/{_OSS}",
                 "chat:code": f"cerebras/{_OSS}", "prefilter": "cerebras/gemma-4-31b",
                 "structured": f"cerebras/{_OSS}", "translate": "cerebras/gemma-4-31b"},
    "groq": {"chat:fast": f"groq/openai/{_OSS}", "chat:smart": f"groq/openai/{_OSS}",
             "chat:code": f"groq/openai/{_OSS}", "prefilter": f"groq/openai/{_OSS}",
             "structured": f"groq/openai/{_OSS}",
             "translate": f"groq/openai/{_OSS}",
             "transcription": "groq/whisper-large-v3-turbo"},
    # 2026-07-10: chat:smart gemini-2.5-pro → gemini-2.5-flash. On the free tier
    # 2.5-pro is capped at ~50-100 req/day @ 5 RPM per key, so under Stepan's
    # smart volume it 429'd ~100% (4096 errors / 0 ok in 3 days) — pure wasted
    # attempts that fell straight through to paid deepseek. 2.5-flash gets
    # ~250 req/day @ 10 RPM per key (~2000/day across our 8 keys, covering the
    # ~1200 smart calls/day) at near-pro quality — moving most smart traffic
    # back onto FREE gemini and off the paid tail.
    "gemini": {"chat:fast": "gemini/gemini-2.5-flash",
               "chat:smart": "gemini/gemini-2.5-flash",
               "chat:code": "gemini/gemini-2.5-flash",
               "chat:edit": "gemini/gemini-2.5-flash",
               "prefilter": "gemini/gemini-2.5-flash",
               "structured": "gemini/gemini-2.5-flash",
               "translate": "gemini/gemini-2.5-flash",
               "vision": "gemini/gemini-2.5-flash",
               "transcription": "gemini/gemini-2.5-flash"},
    # 2026-07-17: moved to deepseek-v4-flash AHEAD of deepseek-chat's
    # deprecation (2026-07-24 15:59 UTC; DeepSeek: "deepseek-chat corresponds
    # to the non-thinking mode of deepseek-v4-flash"). The 2026-07-10 "v4-flash
    # regression" (truncated/empty JSON, ~49% InvalidJSON on chat:fast) was NOT
    # the model — v4 defaults to THINKING mode and reasoning_content ate the
    # max_tokens budget; reasoning_effort="disable" was the wrong knob. The
    # right one is body param thinking={"type":"disabled"} — _DeepseekAdapter
    # sets it for every v4-* call. Confirmed live 2026-07-17: valid JSON at
    # max_tokens=120 on a 17k-token system prompt, reasoning empty, prompt
    # cache hitting 17280/17286. Cheaper too: $0.14/M in (cache-hit $0.0028)
    # vs chat's $0.28/M. Prod track record: 482 Stepan calls, avg 10.4k-token
    # prompts, zero EmptyBody (vs 1590 on deepseek-chat over the same week).
    # deepseek-coder is gone → chat:code also uses v4-flash.
    "deepseek": {"chat:fast": "deepseek/deepseek-v4-flash",
                 "chat:smart": "deepseek/deepseek-v4-flash",
                 "chat:edit": "deepseek/deepseek-v4-flash",
                 "chat:code": "deepseek/deepseek-v4-flash"},
    # 2026-07-16: openai/gpt-oss-120b:free DELISTED by OpenRouter (404
    # NotFoundError, 48 errs/75min — same fate as llama-3.2-vision earlier).
    # All chat lanes moved to google/gemma-4-31b-it:free: verified live on our
    # keys (vision lane, real completions), instruct NON-reasoning (JSON-safe
    # at low max_tokens, unlike the reasoning gpt-oss), 262k ctx.
    "openrouter": {"chat:fast": "openrouter/google/gemma-4-31b-it:free",
                   "chat:smart": "openrouter/google/gemma-4-31b-it:free",
                   "chat:code": "openrouter/google/gemma-4-31b-it:free",
                   "prefilter": "openrouter/google/gemma-4-31b-it:free",
                   "structured": "openrouter/google/gemma-4-31b-it:free",
                   "vision": "openrouter/google/gemma-4-31b-it:free"},
    # 2026-07-02: chat:smart/chat:code/vision/chat:edit bumped sonnet-4-6 →
    # sonnet-5 (near-Opus coding/agentic quality at Sonnet cost; same $3/$15
    # sticker, $2/$10 intro through 2026-08-31). chat:edit also bumped off
    # haiku-4-5 — it's Stepan/Stepan2's Coach fallback (after gemini, deepseek
    # both fail) and needs sonnet-tier reliability on the big-context JSON-edit
    # task, not the fast/cheap tier. Verified the key reaches sonnet-5 and
    # tolerates the broker's temperature=0.7 (drop_params strips it — sonnet-5
    # rejects non-default sampling params). chat:fast/structured stay on
    # haiku-4-5 (fast tier, unrelated to Coach).
    "anthropic": {"chat:fast": "anthropic/claude-haiku-4-5",
                  "chat:smart": "anthropic/claude-sonnet-5",
                  "chat:code": "anthropic/claude-sonnet-5",
                  "chat:edit": "anthropic/claude-sonnet-5",
                  "structured": "anthropic/claude-haiku-4-5",
                  "vision": "anthropic/claude-sonnet-5"},
    "openai": {"chat:fast": "openai/gpt-5-mini", "chat:smart": "openai/gpt-5",
               "chat:code": "openai/gpt-5", "structured": "openai/gpt-5-mini",
               "vision": "openai/gpt-5-mini",
               "transcription": "openai/whisper-1"},
    "mistral": {"chat:fast":   "mistral/mistral-small-latest",
                "chat:smart":  "mistral/mistral-large-latest",
                "chat:code":   "mistral/codestral-latest",
                "prefilter":   "mistral/mistral-small-latest",
                "structured":  "mistral/mistral-small-latest",
                "translate":   "mistral/mistral-small-latest"},
    # 2026-06-26: command-r/r-plus retired 2025-09-15. command-a-03-2025 is
    # flagship; command-r7b-12-2024 is the small/fast model.
    # 2026-07-10: chat:smart/chat:code command-a → command-r7b. command-a is
    # flagship-priced and it was billing ~$2.4/day on Stepan — mostly on FAILED
    # calls (only ~2 ok/day, 96% error) since cohere sits mid-chain behind the
    # free providers and its own free keys are monthly-exhausted, so only the one
    # PAID cohere key reached it, expensively. As a deep fallback, the cheap r7b
    # is the right tier; deepseek remains the quality paid tail.
    "cohere": {"chat:fast":   "cohere/command-r7b-12-2024",
               "chat:smart":  "cohere/command-r7b-12-2024",
               "chat:code":   "cohere/command-r7b-12-2024",
               "prefilter":   "cohere/command-r7b-12-2024",
               "structured":  "cohere/command-r7b-12-2024",
               "translate":   "cohere/command-r7b-12-2024",
               "embedding":   "cohere/embed-english-v3.0"},
    # 2026-07-07: voyage-3 → voyage-4. Confirmed live via Voyage's own
    # dashboard that the whole voyage-3 family has ZERO free-token
    # allocation on our accounts (real $ from token 1 — see _billed_cost),
    # while voyage-4 gets 200M free/month. Confirmed live that voyage-4
    # outputs the SAME 1024 dims as voyage-3 (no storage schema change
    # needed downstream in Vera/Stepan2) but is a DIFFERENT vector space —
    # every existing embedding needs re-embedding with the new model before
    # being compared against a voyage-4 query vector, or similarity scores
    # go silently wrong (same dims, so the callers' `len(a) != len(b)` guard
    # does NOT catch a stale voyage-3 row). See the backfill scripts in the
    # vera3/stepan2 repos run right after this deploy.
    "voyage": {"embedding": "voyage/voyage-4"},
    # 2026-07-04: free tier confirmed live (real key, 200 OK) but capped at
    # req_per_day=20 (x-ratelimit-limit-requests-day header) — see quotas.py.
    # Too thin to be a primary workhorse; added at chain tail as extra free
    # breadth across however many keys accumulate.
    "sambanova": {"chat:fast": "sambanova/Meta-Llama-3.3-70B-Instruct",
                  "chat:smart": "sambanova/Meta-Llama-3.3-70B-Instruct",
                  "chat:code": "sambanova/Meta-Llama-3.3-70B-Instruct",
                  "prefilter": "sambanova/Meta-Llama-3.3-70B-Instruct"},
    # 2026-07-10: GitHub Models REMOVED entirely. Its free tier is tiny (~150
    # req/day on 1 key) and its reset window doesn't align with UTC midnight, so
    # the one key sat exhausted — 155 attempts / 0 success / all 429 on the last
    # full day — pure dead-weight. Key deleted from the DB and provider dropped
    # from chains/quotas/probes.
    # 2026-07-04/05: nvidia — no LiteLLM pricing entry (cost_usd always 0;
    # daily_limit is the only real guard), no card on file (worst case is a
    # 402, not a real charge).
    # 2026-07-10: chat:fast/chat:smart models REMOVED — both went dead on this
    # account (kimi-k2.6 → 404 "Function not found for account"; deepseek-v4-pro
    # → ~91s timeout on the oversubscribed free pool), see chains.py. nemotron
    # (chat:deep) confirmed still alive — kept. A per-(provider,model) liveness
    # handler (roadmap §3.1) would catch this drift automatically instead of by
    # hand; until then, only nemotron is wired.
    "nvidia": {"chat:deep": "nvidia_nim/nvidia/nemotron-3-ultra-550b-a55b"},
    # 2026-07-04: cloudflare Workers AI — confirmed live (real token + account
    # ID). Vision only for now: llava is a real, working image model. NOT
    # wired for transcription — LiteLLM's cloudflare provider only implements
    # chat (see litellm/llms/cloudflare/, no audio submodule); Workers AI's
    # whisper endpoint has a different request shape litellm doesn't speak, so
    # using it would need a raw HTTP call outside litellm. Left for later.
    # 2026-07-07: chat:fast/prefilter added — confirmed live with the real
    # strict Vera triage json_schema (not just json_object): valid JSON,
    # ~1.6s, correct classification, and litellm.get_supported_openai_params
    # confirms response_format is genuinely supported (not silently dropped
    # like zai). Same gpt-oss-120b family already proven reliable on
    # cerebras/groq. Previously only vision was wired here — this was idle
    # free capacity (10k neurons/day, no card on file).
    # 2026-07-10: chat:smart + chat:code added — same @cf/openai/gpt-oss-120b
    # that already serves chat:fast (proven: valid JSON, ~1.6s) and the same
    # model family cerebras/groq run on chat:smart, so it's quality-neutral extra
    # FREE burst capacity for the smart lane (kept off `structured` — gpt-oss
    # emits malformed JSON there, same reason cerebras was dropped from it).
    "cloudflare": {"vision": "cloudflare/@cf/llava-hf/llava-1.5-7b-hf",
                   "chat:fast": "cloudflare/@cf/openai/gpt-oss-120b",
                   "chat:smart": "cloudflare/@cf/openai/gpt-oss-120b",
                   "chat:code": "cloudflare/@cf/openai/gpt-oss-120b",
                   "prefilter": "cloudflare/@cf/openai/gpt-oss-120b"},
    # 2026-07-05: Z.ai (Zhipu) — confirmed live. Only glm-4.5-flash is
    # actually free on this account: glm-4.5/glm-4.5-air both 429'd with
    # "Insufficient balance or no resource package" — no free package for the
    # bigger models here, so chat:smart stays off this provider. LiteLLM DOES
    # have a real (zero) price for glm-4.5-flash — cost_usd isn't blind like
    # nvidia/cloudflare.
    "zai": {"chat:fast": "zai/glm-4.5-flash", "prefilter": "zai/glm-4.5-flash"},
}

def extra_for_provider(provider: str, account_id: str | None) -> dict[str, Any] | None:
    """Per-KEY `extra` kwargs for `call_llm`, beyond model/api_key — thin
    delegate to the provider's adapter (cloudflare's account-scoped api_base is
    the only one today). See providers/adapters.py."""
    return adapter_for(provider).key_extra(account_id)


def model_for(provider: str, capability: str) -> str | None:
    return DEFAULT_MODEL.get(provider, {}).get(capability)


_pricing_warned: set[str] = set()


def estimate_llm_cost(
    model: str, tokens_in: int, tokens_out: int, *,
    at: datetime | None = None,
    cache_read_tokens: int = 0, cache_write_tokens: int = 0,
) -> float:
    """Real per-model cost from LiteLLM's pricing map, times any time-of-day
    surcharge (DeepSeek peak/valley). Returns 0.0 only when the model is
    genuinely unpriced — and logs that once per model so a silent pricing break
    can't hide (a `completion_cost` signature change zeroed every cost for days
    before we noticed, blinding the cost guard). `at` overrides the clock in
    tests; production passes None (= now, UTC).

    `cache_read_tokens`/`cache_write_tokens` are the SUBSET of `tokens_in` that
    hit/wrote anthropic's prompt cache (see apply_prompt_cache) — passed through
    to LiteLLM so a cache read prices at ~0.1x and a cache write at its real
    (higher) creation rate, instead of every prompt token pricing at the flat
    input rate. Without this, cost_usd over-counted anthropic calls that hit
    cache — safe direction (never under-charges) but not the real bill."""
    try:
        p_cost, c_cost = litellm.cost_per_token(
            model=model, prompt_tokens=tokens_in, completion_tokens=tokens_out,
            cache_read_input_tokens=cache_read_tokens,
            cache_creation_input_tokens=cache_write_tokens,
        )
        base = float(p_cost + c_cost)
    except Exception as e:
        if model not in _pricing_warned:
            _pricing_warned.add(model)
            log.warning("no LiteLLM pricing for %s (%s) — cost recorded as 0", model, e)
        return 0.0
    return base * peak_multiplier(model.split("/", 1)[0], at)


# Providers with EXPLICIT prompt caching (a stable system prefix is cached at
# ~0.1x read cost once written). deepseek caches automatically server-side (no
# param), gemini needs its own context-cache lifecycle — neither belongs here.
_EXPLICIT_CACHE_PROVIDERS = ("anthropic",)

# Anthropic allows at most 4 cache_control breakpoints per request.
_MAX_CACHE_MARKS = 4


def apply_prompt_cache(
    model: str, messages: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Mark every LEADING system message (the contiguous role=="system" run at
    the head — Stepan sends its static prefix as several) with `cache_control`
    for providers that support explicit prompt caching, so the whole repeated
    prefix is billed as a cache read, not just the first message. Capped at
    _MAX_CACHE_MARKS breakpoints. No-op for other providers and for non-str
    system content.

    Caching only pays off when the caller sends a byte-stable system prefix;
    the marker is harmless (silently not cached) when it doesn't or when the
    prefix is under the provider's minimum cacheable size."""
    if model.split("/", 1)[0] not in _EXPLICIT_CACHE_PROVIDERS:
        return messages
    out: list[dict[str, Any]] = []
    marks = 0
    head = True
    for m in messages:
        if m.get("role") != "system":
            head = False
        content = m.get("content")
        if head and marks < _MAX_CACHE_MARKS and isinstance(content, str) \
                and content.strip():
            m = {**m, "content": [{
                "type": "text", "text": content,
                "cache_control": {"type": "ephemeral"},
            }]}
            marks += 1
        out.append(m)
    return out


def _usage_field(usage: Any, name: str) -> int:
    if isinstance(usage, dict):
        return int(usage.get(name) or 0)
    return int(getattr(usage, name, 0) or 0)


def _cache_tokens(usage: Any) -> tuple[int, int]:
    """(read, write) prompt-cache tokens from a LiteLLM usage object. anthropic
    reports cache_read_input_tokens / cache_creation_input_tokens; OpenAI-shape
    providers nest cached reads under prompt_tokens_details.cached_tokens."""
    read = _usage_field(usage, "cache_read_input_tokens")
    write = _usage_field(usage, "cache_creation_input_tokens")
    if not read:
        details = usage.get("prompt_tokens_details") if isinstance(usage, dict) \
            else getattr(usage, "prompt_tokens_details", None)
        if details is not None:
            read = _usage_field(details, "cached_tokens")
    return read, write


async def call_llm(
    *,
    model: str,
    messages: list[dict[str, Any]],
    api_key: str,
    max_tokens: int = 1024,
    temperature: float = 0.7,
    response_format: dict[str, Any] | None = None,
    extra: dict[str, Any] | None = None,
    timeout: float | None = None,
) -> tuple[str, dict[str, Any]]:
    """Call LiteLLM. Returns (text, meta).

    `meta` contains: model, tokens_in, tokens_out, cost_usd, latency_ms,
    finish_reason, cache_read_tokens, cache_write_tokens.

    `timeout` (seconds) caps a single provider call so one hung upstream can't
    consume the caller's whole budget — without it, a provider that accepts the
    connection but never responds blocks until the client's own read timeout
    fires (a hard 504/abort) instead of the broker cleanly failing over to the
    next key/provider. None = no cap (LiteLLM default).
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
    # Per-provider request quirks (json_schema downgrade, gemini thinking-off,
    # …) live in one adapter each — see providers/adapters.py. adapter.prepare
    # mutates kwargs in place; the default adapter is a no-op.
    adapter_for(model.split("/", 1)[0]).prepare(model, kwargs)
    if extra:
        kwargs.update(extra)

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
    usage = getattr(resp, "usage", None) or {}
    if isinstance(usage, dict):
        tokens_in = usage.get("prompt_tokens", 0) or 0
        tokens_out = usage.get("completion_tokens", 0) or 0
    else:
        tokens_in = getattr(usage, "prompt_tokens", 0)
        tokens_out = getattr(usage, "completion_tokens", 0)
    cache_read, cache_write = _cache_tokens(usage)
    cost = estimate_llm_cost(model, tokens_in, tokens_out,
                              cache_read_tokens=cache_read, cache_write_tokens=cache_write)

    meta = {
        "model": model,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "cost_usd": cost,
        "latency_ms": latency_ms,
        "cache_read_tokens": cache_read,
        "cache_write_tokens": cache_write,
        "finish_reason": (choices[0].finish_reason if choices else None),
    }
    return text, meta


async def embed(
    *, model: str, texts: list[str], api_key: str
) -> tuple[list[list[float]], dict[str, Any]]:
    t0 = time.time()
    resp = await litellm.aembedding(model=model, input=texts, api_key=api_key)
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
        "tokens_in": tokens_in,
        "tokens_out": 0,
        "cost_usd": estimate_llm_cost(model, tokens_in, 0),
        "latency_ms": latency_ms,
    }
    return vectors, meta


# Chat-based transcription providers (audio in via acompletion, not the
# Whisper atranscription endpoint). groq/openai use real Whisper; gemini has no
# Whisper endpoint but its multimodal chat model transcribes audio natively.
_CHAT_TRANSCRIBE_PROVIDERS = frozenset({"gemini"})
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
    import base64
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
    resp = await litellm.acompletion(**kwargs)
    latency_ms = int((time.time() - t0) * 1000)
    usage = getattr(resp, "usage", None)
    tokens_in = getattr(usage, "prompt_tokens", 0) or 0
    tokens_out = getattr(usage, "completion_tokens", 0) or 0
    meta = {
        "model": model,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        # Unlike Whisper (per-second, billed elsewhere), chat transcription bills
        # per token — price it so a PAID key's cost cap is honoured (_billed_cost
        # zeroes free-tier keys anyway). Audio tokens count as prompt tokens.
        "cost_usd": estimate_llm_cost(model, tokens_in, tokens_out),
        "latency_ms": latency_ms,
    }
    return (resp.choices[0].message.content or "").strip(), meta


async def transcribe(
    *, model: str, audio: bytes, filename: str, api_key: str,
) -> tuple[str, dict[str, Any]]:
    """Audio → text. Whisper providers (groq/openai) via LiteLLM atranscription;
    chat providers (gemini) via acompletion with the audio inlined. Returns
    (text, meta). `filename` carries the extension so the format is inferred."""
    if model.split("/", 1)[0] in _CHAT_TRANSCRIBE_PROVIDERS:
        return await _transcribe_via_chat(
            model=model, audio=audio, filename=filename, api_key=api_key,
        )
    import io

    t0 = time.time()
    buf = io.BytesIO(audio)
    buf.name = filename   # litellm/openai SDK reads .name for the format
    resp = await litellm.atranscription(model=model, file=buf, api_key=api_key)
    latency_ms = int((time.time() - t0) * 1000)
    # Response is an object with .text (or a dict)
    text = resp.get("text", "") if isinstance(resp, dict) else (getattr(resp, "text", "") or "")
    meta = {
        "model": model,
        # Whisper bills per audio-second, not tokens; cost left to caller/usage.
        "tokens_in": 0,
        "tokens_out": 0,
        "cost_usd": 0.0,
        "latency_ms": latency_ms,
    }
    return text.strip(), meta
