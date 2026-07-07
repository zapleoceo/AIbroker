"""Provider abstraction via LiteLLM SDK.

LiteLLM knows the wire format for 100+ providers (cerebras, groq, gemini,
anthropic, openrouter, deepseek, voyage…) — we just pass `model='provider/x'`
and the API key. No HTTP code in our broker for individual providers.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Any

import litellm

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

# Map: provider name → default model per capability. Used when the caller
# doesn't pin a model. Every (provider, capability) that appears in a routing
# chain MUST have an entry here — otherwise the provider is silently skipped.
# Enforced by tests/test_providers.py::test_every_chain_pair_resolves_to_a_model.
_OSS = "gpt-oss-120b"
DEFAULT_MODEL: dict[str, dict[str, str]] = {
    "cerebras": {"chat:fast": f"cerebras/{_OSS}", "chat:smart": f"cerebras/{_OSS}",
                 "chat:code": f"cerebras/{_OSS}", "prefilter": f"cerebras/{_OSS}",
                 "structured": f"cerebras/{_OSS}"},
    "groq": {"chat:fast": f"groq/openai/{_OSS}", "chat:smart": f"groq/openai/{_OSS}",
             "chat:code": f"groq/openai/{_OSS}", "prefilter": f"groq/openai/{_OSS}",
             "structured": f"groq/openai/{_OSS}",
             "translate": f"groq/openai/{_OSS}",
             "transcription": "groq/whisper-large-v3-turbo"},
    "gemini": {"chat:fast": "gemini/gemini-2.5-flash",
               "chat:smart": "gemini/gemini-2.5-pro",
               "chat:code": "gemini/gemini-2.5-flash",
               "chat:edit": "gemini/gemini-2.5-flash",
               "prefilter": "gemini/gemini-2.5-flash",
               "structured": "gemini/gemini-2.5-flash",
               "translate": "gemini/gemini-2.5-flash",
               "vision": "gemini/gemini-2.5-flash"},
    "deepseek": {"chat:fast": "deepseek/deepseek-chat",
                 "chat:smart": "deepseek/deepseek-chat",
                 "chat:edit": "deepseek/deepseek-chat",
                 "chat:code": "deepseek/deepseek-coder"},
    "openrouter": {"chat:fast": f"openrouter/openai/{_OSS}:free",
                   "chat:smart": f"openrouter/openai/{_OSS}:free",
                   "chat:code": f"openrouter/openai/{_OSS}:free",
                   "prefilter": f"openrouter/openai/{_OSS}:free",
                   "structured": f"openrouter/openai/{_OSS}:free"},
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
    "cohere": {"chat:fast":   "cohere/command-r7b-12-2024",
               "chat:smart":  "cohere/command-a-03-2025",
               "chat:code":   "cohere/command-a-03-2025",
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
    # 2026-07-04: GitHub Models (models.inference.ai.azure.com via LiteLLM's
    # github/ prefix) — confirmed live with a real token (200 OK, gpt-4o-mini).
    # gpt-4o-mini everywhere, not gpt-4o: GitHub's "high" tier models (gpt-4o)
    # have a much stricter free-account daily cap than "low" tier (gpt-4o-mini)
    # per GitHub's own docs — gpt-4o unverified, don't default chat:smart to it.
    "github": {"chat:fast": "github/gpt-4o-mini",
               "chat:smart": "github/gpt-4o-mini",
               "chat:code": "github/gpt-4o-mini",
               "prefilter": "github/gpt-4o-mini"},
    # 2026-07-04/05: nvidia — confirmed live with 3 real models on one key
    # (nemotron-3-ultra, kimi-k2.6, deepseek-v4-pro all returned 200). No
    # rate-limit headers, no LiteLLM pricing entry (cost_usd always 0 for
    # this provider — daily_cost_cap_usd is blind here, daily_limit is the
    # only real guard). No card on file on this account, so the earlier
    # "silent billing once one-time credits run out" concern doesn't apply —
    # worst case once the 1,000 one-time credits are spent is the key simply
    # stops working (real 402/"add payment method"), same as any other
    # exhausted free key, not a real charge with nothing to charge against.
    # kimi-k2.6 (fast, ~1.4s, confirmed valid JSON live) → chat:fast.
    # deepseek-v4-pro (slower, ~7.4s, also confirmed valid JSON) → chat:smart,
    # where the latency budget is looser. nemotron stays chat:deep-only — it's
    # the one genuinely too slow (~27s+) for any synchronous capability.
    "nvidia": {"chat:deep": "nvidia_nim/nvidia/nemotron-3-ultra-550b-a55b",
                "chat:fast": "nvidia_nim/moonshotai/kimi-k2.6",
                "chat:smart": "nvidia_nim/deepseek-ai/deepseek-v4-pro"},
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
    "cloudflare": {"vision": "cloudflare/@cf/llava-hf/llava-1.5-7b-hf",
                   "chat:fast": "cloudflare/@cf/openai/gpt-oss-120b",
                   "prefilter": "cloudflare/@cf/openai/gpt-oss-120b"},
    # 2026-07-05: Z.ai (Zhipu) — confirmed live. Only glm-4.5-flash is
    # actually free on this account: glm-4.5/glm-4.5-air both 429'd with
    # "Insufficient balance or no resource package" — no free package for the
    # bigger models here, so chat:smart stays off this provider. LiteLLM DOES
    # have a real (zero) price for glm-4.5-flash — cost_usd isn't blind like
    # nvidia/cloudflare.
    "zai": {"chat:fast": "zai/glm-4.5-flash", "prefilter": "zai/glm-4.5-flash"},
}

# cloudflare needs its account ID embedded in the request URL — LiteLLM has no
# separate kwarg for it, just a full api_base override that already includes
# the model path prefix. See ApiKeyRow.account_id.
_CLOUDFLARE_API_BASE = "https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/run/"


def extra_for_provider(provider: str, account_id: str | None) -> dict[str, Any] | None:
    """Provider-specific `extra` kwargs for `call_llm`, beyond model/api_key.

    Only cloudflare needs this right now. Returns None when there's nothing
    to add (including cloudflare with no account_id set — that call will fail
    downstream with a clear connection error rather than silently here)."""
    if provider == "cloudflare" and account_id:
        return {"api_base": _CLOUDFLARE_API_BASE.format(account_id=account_id)}
    return None


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


def apply_prompt_cache(
    model: str, messages: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Mark the first system message with `cache_control` for providers that
    support explicit prompt caching, so a repeated system prompt is billed as a
    cache read. No-op for other providers and for non-str system content.

    Caching only pays off when the caller sends a byte-stable system prefix;
    the marker is harmless (silently not cached) when it doesn't or when the
    prefix is under the provider's minimum cacheable size."""
    if model.split("/", 1)[0] not in _EXPLICIT_CACHE_PROVIDERS:
        return messages
    out: list[dict[str, Any]] = []
    marked = False
    for m in messages:
        content = m.get("content")
        if not marked and m.get("role") == "system" and isinstance(content, str) \
                and content.strip():
            m = {**m, "content": [{
                "type": "text", "text": content,
                "cache_control": {"type": "ephemeral"},
            }]}
            marked = True
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
        # Gemini 2.5 "thinks" against max_tokens; on a JSON request that can eat
        # the whole budget and truncate the object mid-string. Disable thinking
        # so the JSON fits (mirrors Stepan's thinkingBudget=0). Other providers
        # ignore reasoning_effort=disable, so gate it to gemini.
        if model.startswith("gemini/") and response_format.get("type") in (
            "json_object", "json_schema"
        ):
            kwargs["reasoning_effort"] = "disable"
    if extra:
        kwargs.update(extra)

    t0 = time.time()
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


async def transcribe(
    *, model: str, audio: bytes, filename: str, api_key: str,
) -> tuple[str, dict[str, Any]]:
    """Audio → text via LiteLLM atranscription (Whisper). Returns (text, meta).

    `audio` is raw bytes; `filename` carries the extension so the provider
    infers the format (.ogg/.mp3/.m4a/.wav)."""
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
