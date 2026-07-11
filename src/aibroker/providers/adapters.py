"""Per-provider request quirks — one adapter per provider (SOLID open/closed).

Providers differ in small, specific ways that LiteLLM doesn't paper over: some
reject the strict `json_schema` sub-type, some need a thinking-budget flag on
JSON, some need a per-account URL. Left inline in `call_llm`, each quirk was
another `if provider == …` branch; here each lives in ONE adapter, so adding a
provider's quirk is a new class, not an edit to the shared call path.

An adapter has two hooks, both no-op by default:
  - `prepare(model, kwargs)` — mutate the outgoing LiteLLM kwargs (request-shape
    quirks: response_format downgrade, reasoning_effort). Stateless.
  - `key_extra(account_id)` — per-KEY kwargs beyond model/api_key (cloudflare's
    account-scoped api_base). Takes state from the specific key.
"""
from __future__ import annotations

from typing import Any


class ProviderAdapter:
    """Default adapter: no quirks. Providers with none use this."""

    def prepare(self, model: str, kwargs: dict[str, Any]) -> None:
        return None

    def key_extra(self, account_id: str | None) -> dict[str, Any] | None:
        return None


class _GeminiAdapter(ProviderAdapter):
    def prepare(self, model: str, kwargs: dict[str, Any]) -> None:
        # Gemini 2.5 "thinks" against max_tokens. On JSON that truncates the
        # object mid-string; on any reply it adds latency that overran our call
        # timeout (measured Timeouts on gemini-2.5-flash chat:fast/smart, 2026-
        # 07-10). The broker never wants gemini to deep-reason — long reasoning
        # is the chat:deep/nvidia lane — so disable thinking UNCONDITIONALLY
        # (was JSON-only). Mirrors Stepan's thinkingBudget=0. Other providers
        # ignore reasoning_effort=disable, so it stays scoped to gemini.
        kwargs["reasoning_effort"] = "disable"


class _AnthropicAdapter(ProviderAdapter):
    def prepare(self, model: str, kwargs: dict[str, Any]) -> None:
        # Claude does NOT honour OpenAI's response_format={"type":"json_object"}
        # (litellm silently drops the unsupported param), so with only a prompt
        # instruction Claude sometimes replies in PLAIN TEXT — especially on
        # follow-ups ("write a short friendly follow-up") — and the JSON gate
        # rejects it as InvalidJSON (measured 2026-07-10: ~30% on chat:smart).
        # Convert a json_object request to a PERMISSIVE json_schema: litellm
        # routes json_schema through Claude's native tool-use, which forces a
        # valid JSON object. Permissive (additionalProperties) so the caller's
        # own fields — driven by the prompt, not this schema — are preserved
        # (verified: 8/8 valid, all 17 Stepan fields present).
        rf = kwargs.get("response_format")
        if rf and rf.get("type") == "json_object":
            kwargs["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "reply",
                                "schema": {"type": "object",
                                           "additionalProperties": True}},
            }


class _DeepseekAdapter(ProviderAdapter):
    def prepare(self, model: str, kwargs: dict[str, Any]) -> None:
        # DeepSeek disabled the strict json_schema sub-type server-side (400s
        # "This response_format type is unavailable now") but accepts
        # json_object — confirmed live 2026-07-07. Downgrade so the provider
        # stays usable; the post-hoc JSON gate + caller validation replace the
        # lost server-side grammar enforcement.
        rf = kwargs.get("response_format")
        if rf and rf.get("type") == "json_schema":
            kwargs["response_format"] = {"type": "json_object"}


# cloudflare needs its account ID embedded in the request URL — LiteLLM has no
# separate kwarg for it, just a full api_base override that already includes
# the model path prefix. See ApiKeyRow.account_id.
_CF_API_BASE = "https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/run/"


class _CloudflareAdapter(ProviderAdapter):
    def key_extra(self, account_id: str | None) -> dict[str, Any] | None:
        # None when no account_id — the call then fails downstream with a clear
        # connection error rather than a silently-wrong URL here.
        if account_id:
            return {"api_base": _CF_API_BASE.format(account_id=account_id)}
        return None


_ADAPTERS: dict[str, ProviderAdapter] = {
    "gemini": _GeminiAdapter(),
    "anthropic": _AnthropicAdapter(),
    "deepseek": _DeepseekAdapter(),
    "cloudflare": _CloudflareAdapter(),
}
_DEFAULT_ADAPTER = ProviderAdapter()


def adapter_for(provider: str) -> ProviderAdapter:
    """The adapter for `provider` (bare name, e.g. 'deepseek'), or a no-op
    default. `provider` is `model.split('/', 1)[0]` at the call site."""
    return _ADAPTERS.get(provider, _DEFAULT_ADAPTER)
