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

    def prepare(self, _model: str, kwargs: dict[str, Any]) -> None:
        return None

    def key_extra(self, account_id: str | None) -> dict[str, Any] | None:
        return None


class _GeminiAdapter(ProviderAdapter):
    def prepare(self, _model: str, kwargs: dict[str, Any]) -> None:
        # Gemini 2.5 "thinks" against max_tokens. On JSON that truncates the
        # object mid-string; on any reply it adds latency that overran our call
        # timeout (measured Timeouts on gemini-2.5-flash chat:fast/smart, 2026-
        # 07-10). The broker never wants gemini to deep-reason — long reasoning
        # is the chat:deep/nvidia lane — so disable thinking UNCONDITIONALLY
        # (was JSON-only). Mirrors Stepan's thinkingBudget=0. Other providers
        # ignore reasoning_effort=disable, so it stays scoped to gemini.
        kwargs["reasoning_effort"] = "disable"


class _AnthropicAdapter(ProviderAdapter):
    def prepare(self, _model: str, kwargs: dict[str, Any]) -> None:
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


# Non-thinking deepseek (== deepseek-chat) goes deterministically EMPTY on
# json_object once the prompt nears ~30k chars (verified 30k→empty 4/4,
# 16k→OK 4/4). Threshold sits under the verified failure point with margin —
# Stepan's failing followups were ~25k system + dialog. The mt floor keeps
# thinking from starving the content budget on short-reply calls.
_DEEPSEEK_JSON_EMPTY_CHARS = 24_000
_DEEPSEEK_THINKING_MT_FLOOR = 1_000

# On the thinking-keep path, reasoning_content shares max_tokens with the
# visible JSON body — at Stepan's real max_tokens=2000 the reasoning pass
# routinely eats nearly the whole budget (usage_log: EmptyBody/InvalidJSON
# calls averaged 1662-1936 output tokens, right up against the 2000 cap;
# clean successes averaged only 1202). Live A/B on job 75792's real prompt
# (N=6 each, thinking enabled): mt=2000 → 1/6 bad, ~28s avg, 38s max;
# mt=3000 → 0/6 bad, ~18s avg, 24s max (headroom removes the truncation AND
# is FASTER — no retry-inducing dead end); mt=4000 → 1/6 bad again, higher
# latency (not monotonic, no reason to go further). This floor only RAISES a
# caller's max_tokens on the thinking-keep path, never lowers it — comfortably
# under the 60s call timeout and the 90s chat:smart client budget either way.
_DEEPSEEK_THINKING_HEADROOM_TOKENS = 3_000


def _prompt_chars(messages: list[dict[str, Any]]) -> int:
    return sum(len(str(m.get("content") or "")) for m in messages)


def is_deepseek_big_json_prompt(
    response_format: dict[str, Any] | None, messages: list[dict[str, Any]]
) -> bool:
    """True for a JSON request big enough to trigger deepseek-v4-flash's
    empty-body bug (see deepseek_model_for_json) — shared with run_chat's
    savings-side chain reorder (deprioritize_deepseek_for_savings) so both the
    "which model" and "which chain position" decisions use the exact same
    threshold, not two copies that could drift."""
    rf = response_format or {}
    if rf.get("type") not in ("json_object", "json_schema"):
        return False
    return _prompt_chars(messages) >= _DEEPSEEK_JSON_EMPTY_CHARS


def deepseek_model_for_json(
    model: str | None,
    response_format: dict[str, Any] | None,
    messages: list[dict[str, Any]],
) -> str | None:
    """Which deepseek model to actually call for a (possibly JSON) request.

    v4-flash empties json_object DETERMINISTICALLY once the prompt nears ~30k
    chars — BOTH thinking modes (verified live on a real 51k-char Stepan reply
    prompt: empty 4/4 with thinking, 4/4 without). The input is billed for
    nothing and the answer only survives by falling through to the free tail.
    v4-pro handles the same prompt (0/3 empty, valid JSON, ~4.6s no-thinking)
    AND still hits the per-key prompt cache (cache-read priced at ~1/120th of
    miss), so the static-catalog prefix stays cheap. So upgrade ONLY the big
    JSON calls to pro; flash stays for everything else (3x cheaper, and it works
    below the threshold). Caller must feed the RESULT into cost estimation so
    the pro price is booked — see run_chat's use_model."""
    if not model or "deepseek-v4-flash" not in model:
        return model
    if not is_deepseek_big_json_prompt(response_format, messages):
        return model
    return model.replace("deepseek-v4-flash", "deepseek-v4-pro")


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
        # v4 models default to THINKING mode; hidden reasoning_content eats the
        # max_tokens budget so short JSON replies truncate to empty (the 2026-
        # 07-10 "v4-flash regression" was this default, not the model — and
        # reasoning_effort="disable" is NOT the deepseek knob). So disable it
        # via the documented body param (confirmed live 2026-07-17: valid JSON
        # at max_tokens=120, reasoning_content empty)…
        #
        # CORRECTED 2026-07-21 (hours after the v4-pro upgrade shipped): first
        # attempt at "v4-pro always no-thinking" was based on a single N=3 live
        # test on ONE prompt shape (flat system+user) that happened to show 0/3
        # empty without thinking. Re-tested live against job 75792's REAL
        # multi-turn (19-message) reply prompt at N=6 and got the opposite
        # result — pro is WORSE without thinking there, not better:
        #   pro no-thinking:   6/6 EMPTY  (100% — this shipped and made things
        #                                  WORSE than pre-upgrade flash)
        #   pro thinking:      2/6 EMPTY  (33% — still not perfect, but by far
        #                                  the best of the 4 combinations)
        #   flash thinking:    5/6 EMPTY  (83%)
        #   flash no-thinking: 5/6 EMPTY  (83%)
        # So the thinking-keep condition below applies uniformly to EVERY v4-*
        # model, not just flash — pro gets no special-cased no-thinking. On
        # multi-turn dialog prompts the reasoning pass is apparently what makes
        # DeepSeek actually emit the JSON body at all, for both models; without
        # it the empty-body bug reappears regardless of which v4 variant.
        # Scoped to v4-*: deepseek-reasoner IS the thinking mode, and legacy
        # names pre-date the param.
        tail = model.split("/", 1)[-1]
        if tail.startswith("deepseek-v4"):
            rf_now = kwargs.get("response_format") or {}
            keep_thinking = (
                str(rf_now.get("type", "")).startswith("json")
                and _prompt_chars(kwargs.get("messages", [])) >= _DEEPSEEK_JSON_EMPTY_CHARS
                and kwargs.get("max_tokens", 0) >= _DEEPSEEK_THINKING_MT_FLOOR
            )
            if not keep_thinking:
                kwargs.setdefault("extra_body", {}).setdefault(
                    "thinking", {"type": "disabled"})
            else:
                kwargs["max_tokens"] = max(
                    kwargs.get("max_tokens", 0), _DEEPSEEK_THINKING_HEADROOM_TOKENS)


class _CerebrasAdapter(ProviderAdapter):
    def prepare(self, _model: str, kwargs: dict[str, Any]) -> None:
        # Cerebras rejects strict json_schema whose array fields carry validation
        # keywords it doesn't implement ("Invalid fields for schema with types
        # ['array']: {'maxItems'}", ~194 BadRequests/45min on Stepan's chat:smart,
        # 2026-07-11). It's already out of `structured` for emitting malformed
        # JSON on schemas anyway, so drop the schema entirely — json_object keeps
        # it usable and the post-hoc JSON gate + caller validation cover grammar.
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
    "cerebras": _CerebrasAdapter(),
    "cloudflare": _CloudflareAdapter(),
}
_DEFAULT_ADAPTER = ProviderAdapter()


def adapter_for(provider: str) -> ProviderAdapter:
    """The adapter for `provider` (bare name, e.g. 'deepseek'), or a no-op
    default. `provider` is `model.split('/', 1)[0]` at the call site."""
    return _ADAPTERS.get(provider, _DEFAULT_ADAPTER)
