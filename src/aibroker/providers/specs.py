"""The provider data — one `build_provider(...)` per provider.

Add a provider = add an entry here (+ its chain slot in routing/chains.py and an
adapter class in providers/adapters.py only if it has request quirks).
Add a model = add it to `defaults` / `rotation` of its provider.

Each comment is a ONE-LINE rationale; the dated incident history and the
measurements behind every choice live in docs/history/provider-choices.md.
"""
from __future__ import annotations

from aibroker.providers.adapters import (
    _AnthropicAdapter,
    _CerebrasAdapter,
    _CloudflareAdapter,
    _DeepseekAdapter,
    _GeminiAdapter,
    _SambanovaAdapter,
    _ZaiAdapter,
)
from aibroker.providers.registry import ProbeSpec, Quota, build_provider, register

_OSS = "gpt-oss-120b"
_CHAT_USER_PING = [{"role": "user", "content": "."}]


def _chat_probe(url: str, model: str, **kw) -> ProbeSpec:
    return ProbeSpec(url, {"model": model, "messages": _CHAT_USER_PING, "max_tokens": 1}, **kw)


# Gemini: free quota is metered PER MODEL per key, so rotate across flash models.
_GEMINI_CHAT_ROTATION = ("gemini/gemini-3.5-flash-lite",
                         "gemini/gemini-3.6-flash",
                         "gemini/gemini-3.1-flash-lite",
                         "gemini/gemini-3.5-flash")
_GEMINI_VISION_ROTATION = ("gemini/gemini-3.5-flash-lite",
                           "gemini/gemini-3.5-flash",
                           "gemini/gemini-3.1-flash-lite")
_GEMINI_CHAT_LANES = ("chat:fast", "chat:smart", "chat:sales", "chat:code",
                      "chat:edit", "structured", "prefilter", "translate")

# ── free-tier workhorses ────────────────────────────────────────────────────

register(build_provider(
    "cerebras", rank=10, max_keys=3, cooldown_base_s=60,
    adapter=_CerebrasAdapter(), json_reliability="unreliable",
    cache_key_param="prompt_cache_key",   # docs: inference-docs.cerebras.ai/capabilities/prompt-caching
    # Free tier is enforced on TOKENS/day, not requests/day.
    quota=Quota(tok_per_day=1_000_000,
                doc="https://inference-docs.cerebras.ai/support/rate-limits"),
    probe=_chat_probe("https://api.cerebras.ai/v1/chat/completions", _OSS),
    quota_headers="openai", trust_req_header=False,
    defaults={"chat:fast": f"cerebras/{_OSS}", "chat:smart": f"cerebras/{_OSS}",
              "chat:code": f"cerebras/{_OSS}", "structured": f"cerebras/{_OSS}"},
))

register(build_provider(
    "groq", rank=20, cooldown_base_s=60, max_request_tokens=8_000,
    json_reliability="unreliable",        # grammar-JSON mode 400s server-side
    quota=Quota(req_per_day=14_400, tok_per_day=500_000,
                doc="https://console.groq.com/docs/rate-limits"),
    probe=_chat_probe("https://api.groq.com/openai/v1/chat/completions", f"openai/{_OSS}"),
    quota_headers="openai",
    defaults={"chat:fast": f"groq/openai/{_OSS}", "chat:smart": f"groq/openai/{_OSS}",
              "chat:code": f"groq/openai/{_OSS}", "prefilter": f"groq/openai/{_OSS}",
              "structured": f"groq/openai/{_OSS}", "translate": f"groq/openai/{_OSS}",
              "transcription": "groq/whisper-large-v3-turbo"},
    model_meta={
        "groq/whisper-large-v3-turbo": {"pricing": "per_minute", "usd_per_minute": 0.04 / 60},
        "groq/whisper-large-v3": {"pricing": "per_minute", "usd_per_minute": 0.111 / 60},
    },
))

register(build_provider(
    "gemini", rank=30, max_keys=3, cooldown_base_s=60, adapter=_GeminiAdapter(),
    quota=Quota(req_per_day=1_500, doc="https://ai.google.dev/gemini-api/docs/rate-limits"),
    # Free-quota providers (gemini 20/day/model, cohere 1000/month, mistral monthly,
    # openrouter :free ~50/day) probe via free unmetered key-list endpoints, never a
    # generation; paid ones keep a 1-token generation (a list 200s for a billing-dead key).
    probe=ProbeSpec("https://generativelanguage.googleapis.com/v1beta/models?pageSize=1",
                    auth="gemini_key"),
    billing_probe=ProbeSpec(
        "https://generativelanguage.googleapis.com/v1beta/models/"
        "gemini-2.5-flash-lite:generateContent",
        {"contents": [{"parts": [{"text": "ping"}]}],
         "generationConfig": {"maxOutputTokens": 1}}, auth="gemini"),
    defaults={"chat:fast": "gemini/gemini-2.5-flash", "chat:smart": "gemini/gemini-2.5-flash",
              "chat:sales": "gemini/gemini-2.5-flash", "chat:code": "gemini/gemini-2.5-flash",
              "chat:edit": "gemini/gemini-2.5-flash", "structured": "gemini/gemini-2.5-flash",
              "vision": "gemini/gemini-2.5-flash",
              "prefilter": "gemini/gemini-2.5-flash-lite",
              "translate": "gemini/gemini-2.5-flash-lite",
              "transcription": "gemini/gemini-3.5-transcribe"},
    rotation={**dict.fromkeys(_GEMINI_CHAT_LANES, _GEMINI_CHAT_ROTATION),
              "vision": _GEMINI_VISION_ROTATION},
    model_meta={   # dedicated ASR model needs raw generateContent (not litellm)
        "gemini/gemini-3.5-transcribe": {
            "transport": "gemini_asr", "pricing": "per_minute", "usd_per_minute": 0.005},
    },
))

register(build_provider(
    "sambanova", rank=40, cooldown_base_s=120,
    adapter=_SambanovaAdapter(), quota_headers="openai",
    quota=Quota(req_per_day=20,
                doc="https://docs.sambanova.ai/cloud/docs/get-started/rate-limits"),
    probe=_chat_probe("https://api.sambanova.ai/v1/chat/completions", "gemma-4-31B-it"),
    defaults={"chat:fast": "sambanova/gemma-4-31B-it",
              "chat:smart": "sambanova/DeepSeek-V3.2",
              "chat:sales": "sambanova/DeepSeek-V3.2",
              "chat:code": "sambanova/DeepSeek-V3.2",
              "prefilter": "sambanova/gemma-4-31B-it",
              "vision": "sambanova/gemma-4-31B-it"},
))

register(build_provider(
    "zai", rank=50, cooldown_base_s=60, adapter=_ZaiAdapter(),
    json_reliability="incapable",         # no response_format support at all
    auth_signs=("invalid api parameter",),
    quota=Quota(doc="https://docs.z.ai/guides/overview/quick-start"),
    probe=_chat_probe("https://api.z.ai/api/paas/v4/chat/completions", "glm-4.5-flash"),
    defaults={"chat:fast": "zai/glm-4.7-flash", "prefilter": "zai/glm-4.7-flash"},
))

register(build_provider(
    "cloudflare", rank=60, cooldown_base_s=120, adapter=_CloudflareAdapter(),
    rate_limit_signs=("daily free allocation", "neurons"),
    quota=Quota(doc="https://developers.cloudflare.com/workers-ai/platform/pricing/"),
    probe=_chat_probe(
        "https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/v1/chat/completions",
        "@cf/openai/gpt-oss-120b", needs_account_id=True),
    model_meta={"cloudflare/@cf/llava-hf/llava-1.5-7b-hf": {
        "pricing": "free", "note": "no litellm price; free neuron budget, no card on file"}},
    defaults={"vision": "cloudflare/@cf/llava-hf/llava-1.5-7b-hf",
              "chat:fast": "cloudflare/@cf/openai/gpt-oss-120b",
              "chat:smart": "cloudflare/@cf/openai/gpt-oss-120b",
              "chat:code": "cloudflare/@cf/openai/gpt-oss-120b",
              "prefilter": "cloudflare/@cf/openai/gpt-oss-120b"},
))

register(build_provider(
    "openrouter", rank=70, cooldown_base_s=300, json_reliability="unreliable",
    cache_key_param="session_id",         # docs: openrouter.ai/docs/guides/best-practices/prompt-caching
    quota_headers="openai",
    quota=Quota(req_per_day=200, doc="https://openrouter.ai/docs/api-reference/limits"),
    probe=ProbeSpec("https://openrouter.ai/api/v1/auth/key"),
    billing_probe=_chat_probe("https://openrouter.ai/api/v1/chat/completions",
                              "openai/gpt-4o-mini"),
    defaults={"chat:fast": "openrouter/google/gemma-4-31b-it:free",
              "chat:smart": "openrouter/google/gemma-4-31b-it:free",
              "chat:code": "openrouter/google/gemma-4-31b-it:free",
              "prefilter": "openrouter/google/gemma-4-31b-it:free",
              "structured": "openrouter/google/gemma-4-31b-it:free",
              "vision": "openrouter/google/gemma-4-31b-it:free",
              "decision": "openrouter/typesafe/jev-1.13"},
    model_meta={   # decisions endpoint is not a litellm route; priced by our own list price
        "openrouter/typesafe/jev-1.13": {
            "transport": "openrouter_decisions", "pricing": "override",
            "input_usd_per_mtok": 0.042, "output_usd_per_mtok": 0.0, "register": False},
        "openrouter/inception/mercury-decide:free": {
            "transport": "openrouter_decisions", "pricing": "free",
            "capabilities": ("decision",)},
    },
))

register(build_provider(
    "mistral", rank=80, cooldown_base_s=10,           # 1 RPS — recovers instantly
    cache_key_param="prompt_cache_key",   # docs: docs.mistral.ai/api/endpoint/chat
    rate_limit_signs=("unauthorized",), monthly_signs=("unauthorized",),
    quota_headers="openai",               # bare 401 = monthly plan exhaustion (not a revoked key)
    quota=Quota(doc="https://docs.mistral.ai/deployment/laplateforme/tier/"),
    probe=ProbeSpec("https://api.mistral.ai/v1/models"),
    billing_probe=_chat_probe("https://api.mistral.ai/v1/chat/completions", "mistral-small-latest"),
    defaults={"chat:fast": "mistral/mistral-small-latest",
              "chat:smart": "mistral/mistral-large-latest",
              "chat:code": "mistral/codestral-latest",
              "prefilter": "mistral/mistral-small-latest",
              "structured": "mistral/mistral-small-latest",
              "translate": "mistral/mistral-small-latest"},
))

register(build_provider(
    "cohere", rank=90, cooldown_base_s=60, json_reliability="unreliable",
    quota=Quota(req_per_day=1_000, doc="https://docs.cohere.com/v2/docs/rate-limits"),
    probe=ProbeSpec("https://api.cohere.com/v1/models?page_size=1"),
    billing_probe=ProbeSpec("https://api.cohere.com/v2/chat",
                            {"model": "command-r7b-12-2024", "max_tokens": 1,
                             "messages": _CHAT_USER_PING}),
    defaults={"chat:fast": "cohere/command-r7b-12-2024",
              "chat:smart": "cohere/command-r7b-12-2024",
              "chat:code": "cohere/command-r7b-12-2024",
              "prefilter": "cohere/command-r7b-12-2024",
              "structured": "cohere/command-r7b-12-2024",
              "translate": "cohere/command-r7b-12-2024",
              "embedding": "cohere/embed-english-v3.0"},
))

register(build_provider(
    "nvidia", rank=100, cooldown_base_s=300,
    quota=Quota(doc="https://build.nvidia.com/settings/api-keys"),
    probe=_chat_probe("https://integrate.api.nvidia.com/v1/chat/completions",
                      "nvidia/nemotron-3-ultra-550b-a55b"),
    defaults={"chat:deep": "nvidia_nim/nvidia/nemotron-3-ultra-550b-a55b"},
    model_meta={"nvidia_nim/nvidia/nemotron-3-ultra-550b-a55b": {
        "pricing": "free", "note": "no litellm price; credits-based free tier",
        "litellm_extra": {"litellm_provider": "nvidia_nim"}}},
))

register(build_provider(
    "voyage", rank=300, cooldown_base_s=60,
    rate_limit_signs=("reduced rate limits",),
    quota=Quota(doc="https://docs.voyageai.com/docs/pricing"),
    probe=ProbeSpec("https://api.voyageai.com/v1/embeddings",
                    {"model": "voyage-4", "input": "."}),
    defaults={"embedding": "voyage/voyage-4"},
    model_meta={"voyage/voyage-4": {          # absent from litellm's map: $0.06/M, 200M free/mo
        "pricing": "override", "input_usd_per_mtok": 0.06, "output_usd_per_mtok": 0.0,
        "litellm_extra": {"litellm_provider": "voyage", "mode": "embedding"}}},
))

# ── self-hosted (no bill; raw HTTP behind the same transport Protocol) ──────

register(build_provider(
    "local", rank=5, cooldown_base_s=30,   # a timeout = busy decode lock, not a dead credential
    empty_is_failure=True,
    quota=Quota(doc="https://github.com/zapleoceo/muai/blob/master/vera3/docs/asr-local.md"),
    defaults={"vision": "local/qwen3vl"},
    model_meta={
        "local/qwen3vl": {"transport": "local_vision", "pricing": "local"},
    },
))

# ── paid tail ───────────────────────────────────────────────────────────────

register(build_provider(
    "deepseek", rank=200, paid=True, cache_sticky=True, cooldown_base_s=30,
    adapter=_DeepseekAdapter(),
    rate_limit_signs=("response_format type is unavailable",),
    quota_headers="openai", quota=Quota(doc="https://api-docs.deepseek.com/quick_start/pricing"),
    probe=ProbeSpec("https://api.deepseek.com/chat/completions",
                    {"model": "deepseek-flash", "messages": _CHAT_USER_PING,
                     "max_tokens": 1, "thinking": {"type": "disabled"}}),
    defaults={"chat:fast": "deepseek/deepseek-flash", "chat:smart": "deepseek/deepseek-flash",
              "chat:sales": "deepseek/deepseek-flash", "chat:edit": "deepseek/deepseek-flash",
              "chat:code": "deepseek/deepseek-flash", "vision": "deepseek/deepseek-flash"},
    model_meta={"deepseek/deepseek-flash": {   # absent from litellm: OFF-PEAK list prices
        "pricing": "override", "input_usd_per_mtok": 0.15,
        "cache_read_usd_per_mtok": 0.003, "output_usd_per_mtok": 0.60,
        "litellm_extra": {
            "max_input_tokens": 1_000_000, "max_output_tokens": 384_000,
            "litellm_provider": "deepseek", "mode": "chat", "supports_vision": True,
            "supports_response_schema": True, "supports_prompt_caching": True}}},
))

register(build_provider(
    "anthropic", rank=210, paid=True, cache_sticky=True, explicit_cache=True, cooldown_base_s=120,
    adapter=_AnthropicAdapter(), quota_headers="anthropic",
    quota=Quota(doc="https://docs.claude.com/en/docs/about-claude/usage-limits"),
    probe=ProbeSpec("https://api.anthropic.com/v1/messages",
                    {"model": "claude-haiku-4-5", "max_tokens": 1,
                     "messages": _CHAT_USER_PING}, auth="anthropic"),
    defaults={"chat:fast": "anthropic/claude-haiku-4-5",
              "chat:smart": "anthropic/claude-sonnet-5",
              "chat:sales": "anthropic/claude-sonnet-5",
              "chat:code": "anthropic/claude-sonnet-5",
              "chat:edit": "anthropic/claude-sonnet-5",
              "structured": "anthropic/claude-haiku-4-5",
              "vision": "anthropic/claude-sonnet-5"},
))

register(build_provider(
    "openai", rank=220, paid=True, cooldown_base_s=120,
    cache_key_param="prompt_cache_key",   # docs: platform.openai.com/docs/guides/prompt-caching
    quota_headers="openai", quota=Quota(doc="https://platform.openai.com/docs/guides/rate-limits"),
    probe=_chat_probe("https://api.openai.com/v1/chat/completions", "gpt-4o-mini"),
    defaults={"chat:fast": "openai/gpt-5-mini", "chat:smart": "openai/gpt-5",
              "chat:code": "openai/gpt-5", "structured": "openai/gpt-5-mini",
              "vision": "openai/gpt-5-mini", "transcription": "openai/whisper-1"},
    model_meta={"openai/whisper-1": {"pricing": "per_minute", "usd_per_minute": 0.006}},
))
