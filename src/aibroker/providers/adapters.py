"""Per-provider request quirks — one adapter per provider (SOLID open/closed).

Providers differ in small, specific ways that LiteLLM doesn't paper over: some
reject the strict `json_schema` sub-type, some need a thinking-budget flag on
JSON, some need a per-account URL. Left inline in `call_llm`, each quirk was
another `if provider == …` branch; here each lives in ONE adapter, so adding a
provider's quirk is a new class, not an edit to the shared call path.

An adapter has three hooks, all no-op by default:
  - `prepare(model, kwargs)` — mutate the outgoing LiteLLM kwargs (request-shape
    quirks: response_format downgrade, reasoning_effort). Stateless.
  - `normalize_json_text(text, response_format)` — normalize a JSON-mode
    response BODY before it reaches the caller (anthropic's tool-call envelope
    unwrap). The post-response twin of `prepare`. Stateless.
  - `key_extra(account_id)` — per-KEY kwargs beyond model/api_key (cloudflare's
    account-scoped api_base). Takes state from the specific key.
"""
from __future__ import annotations

import json
from typing import Any


class ProviderAdapter:
    """Default adapter: no quirks. Providers with none use this.

    `capability` is the lane the call belongs to (chat:sales, chat:smart, …).
    Most quirks are provider-wide and ignore it; anthropic needs it because the
    same model (claude-sonnet-5) serves several lanes that want DIFFERENT
    trade-offs — see _AnthropicAdapter. Optional so callers without a lane in
    hand (health probes) keep working."""

    def prepare(self, _model: str, kwargs: dict[str, Any],
                capability: str | None = None) -> None:
        return None

    def normalize_json_text(
        self, text: str, _response_format: dict[str, Any] | None
    ) -> str:
        """Clean up a JSON-mode response body. Default: return it unchanged."""
        return text

    def key_extra(self, account_id: str | None) -> dict[str, Any] | None:
        return None


class _ZaiAdapter(ProviderAdapter):
    def prepare(self, _model: str, kwargs: dict[str, Any],
                capability: str | None = None) -> None:
        # GLM defaults to thinking mode and spends the WHOLE max_tokens budget
        # on hidden reasoning, returning an empty body — the same failure shape
        # DeepSeek's v4 had. Measured live 2026-08-16 on a trivial "reply ok"
        # prompt at max_tokens=64:
        #     glm-4.7-flash  as-is        → out=64, text=''
        #     glm-4.7-flash  thinking off → out=2,  text='ok'
        #     glm-4.5-flash  as-is        → out=64, text=''
        #     glm-4.5-flash  thinking off → out=2,  text='ok'
        # Identical on both model versions, so this is the provider's default,
        # not a model regression. It explains why 7 live zai keys served only
        # ~15 calls a week: every reply came back empty and was rejected by the
        # JSON/empty gate. setdefault so an explicit caller value still wins.
        kwargs.setdefault("extra_body", {}).setdefault("thinking", {"type": "disabled"})


# Gemini 3.7+ rejects the MINIMAL thinking level that litellm maps
# reasoning_effort="disable" onto ("Thinking level MINIMAL is not supported for
# this model", HTTP 400 — measured live 2026-08-16). "low" is accepted and is
# just as cheap in practice: on the same prompt 3.7-flash returned out=1 in
# 926ms with "low" versus out=92 in 3567ms with no thinking parameter at all.
# Older models keep "disable" — 3.6-flash still accepts it (out=1, 894ms).
_GEMINI_NO_DISABLE_PREFIXES = ("gemini-3.7", "gemini-3.8", "gemini-4")


class _GeminiAdapter(ProviderAdapter):
    def prepare(self, model: str, kwargs: dict[str, Any],
                capability: str | None = None) -> None:
        # Gemini 2.5 "thinks" against max_tokens. On JSON that truncates the
        # object mid-string; on any reply it adds latency that overran our call
        # timeout (measured Timeouts on gemini-2.5-flash chat:fast/smart, 2026-
        # 07-10). The broker never wants gemini to deep-reason — long reasoning
        # is the chat:deep/nvidia lane — so disable thinking UNCONDITIONALLY
        # (was JSON-only). Mirrors Stepan's thinkingBudget=0. Other providers
        # ignore reasoning_effort=disable, so it stays scoped to gemini.
        #
        # 2026-08-16: model-aware, because "disable" became a hard 400 on
        # gemini-3.7+ (see _GEMINI_NO_DISABLE_PREFIXES). The intent is unchanged
        # — spend as little as possible on reasoning — only the wire value
        # differs per model generation.
        tail = model.split("/", 1)[-1]
        kwargs["reasoning_effort"] = (
            "low" if tail.startswith(_GEMINI_NO_DISABLE_PREFIXES) else "disable"
        )


# Keys LiteLLM can leave a forced-tool JSON reply wrapped in. Claude has no
# native json_object mode, so the adapter upgrades it to a permissive
# json_schema, which LiteLLM serves via a FORCED TOOL CALL and converts back to
# content. Sonnet intermittently emits the tool *input* inside a generic
# function-call envelope — {"parameters": {…the caller's real fields…}} — and
# LiteLLM forwards it verbatim (its conversion unwraps only a "values" wrapper,
# litellm#6741). Measured live on chat:sales: ~half of replies arrived wrapped.
_TOOL_ENVELOPE_KEYS = ("parameters", "arguments", "input")


class _AnthropicAdapter(ProviderAdapter):
    def prepare(self, _model: str, kwargs: dict[str, Any],
                capability: str | None = None) -> None:
        # Claude does NOT honour OpenAI's response_format={"type":"json_object"}
        # (litellm silently drops the unsupported param), so with only a prompt
        # instruction Claude often replies in PLAIN TEXT and the JSON gate
        # rejects it as InvalidJSON (~30% on chat:smart, 2026-07-10). Convert a
        # json_object request to a PERMISSIVE json_schema: litellm routes
        # json_schema through Claude's native tool-use, which forces a valid
        # JSON object. Permissive (additionalProperties) so the caller's own
        # fields — driven by the prompt, not this schema — are preserved
        # (verified: 8/8 valid, all 17 Stepan fields present).
        #
        # 2026-07-26 — chat:sales USED to be exempt from this, to keep Sonnet's
        # reasoning (forced tool-use suppresses thinking; the two are mutually
        # exclusive on this model). Production killed that trade-off: with the
        # exemption live, chat:sales returned **44% InvalidJSON** (19 of 43
        # billed calls unusable). Sampling the bodies showed why — Claude does
        # not "almost" produce JSON there, it ignores the instruction entirely
        # and answers in prose (3/3 plain Bahasa replies on the real 81k-char
        # sales prompt). The earlier 3/3-valid measurement had used a short
        # prompt that literally said "reply ONLY with a JSON object", which does
        # not survive the real prompt. So the guarantee wins: every lane forces
        # JSON again. Reasoning returns for free if a caller stops sending
        # response_format on this lane.
        rf = kwargs.get("response_format")
        if rf and rf.get("type") == "json_object":
            kwargs["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "reply",
                                "schema": {"type": "object",
                                           "additionalProperties": True}},
            }

    def normalize_json_text(
        self, text: str, response_format: dict[str, Any] | None
    ) -> str:
        """Unwrap ONE level of LiteLLM's forced-tool envelope (see
        _TOOL_ENVELOPE_KEYS). Fires only on an unambiguous shape: a JSON
        request whose body is an object holding EXACTLY one of those keys, whose
        value is itself an object, and which the caller's own json_schema does
        not declare — a schema legitimately asking for a top-level "input"
        object is returned untouched. Everything else (plain-text requests,
        arrays, sibling keys, non-object inners) passes through byte-identical.
        """
        rf = response_format or {}
        if str(rf.get("type", "")) not in ("json_object", "json_schema"):
            return text
        try:
            body = json.loads(text)
        except (ValueError, TypeError):
            return text
        if not isinstance(body, dict) or len(body) != 1:
            return text
        key = next(iter(body))
        if key not in _TOOL_ENVELOPE_KEYS or not isinstance(body[key], dict):
            return text
        # the caller genuinely asked for this key at the top level → keep it
        declared = (rf.get("json_schema") or {}).get("schema", {})
        if key in (declared.get("properties") or {}):
            return text
        return json.dumps(body[key], ensure_ascii=False)


# Non-thinking deepseek (== deepseek-chat) goes deterministically EMPTY on
# json_object once the prompt gets large (originally probed at only two points:
# 30k→empty 4/4, 16k→OK 4/4). The mt floor keeps thinking from starving the
# content budget on short-reply calls.
#
# 2026-07-23: lowered 24k→16k. 24k was picked "under the verified failure point
# with margin", but the 16k-30k range had never actually been measured, and the
# real onset is far below 24k. Production evidence (Stepan chat:smart):
#   - flash SUCCEEDS at avg 3397 prompt tokens (~11k chars)
#   - flash EMPTIES at 6526-7183 prompt tokens (~22-24k chars)
#   - a measured batch of 30 live prompts: median 23515 chars, 11/30 sitting in
#     the 16k-24k band — i.e. ~37% of traffic was landing JUST under the old
#     threshold, staying on flash, and emptying (the user's log dump showed ~27
#     consecutive empties at tokens_in 7036-7084, output cut at 57-150 tokens).
# 16k is the highest size flash is actually VERIFIED to handle (4/4 OK), so the
# threshold now sits at evidence rather than at a guessed margin.
#
# This is a stop-gap that trades cost for reliability: those ~37% now escalate
# to v4-pro (3x the per-token price) or get served free by the savings-side
# reorder. The real fix is caller-side — Stepan's system prompt alone is 94% of
# the payload (median 22122 of 23515 chars); trimming it under ~15k chars puts
# this traffic back on cheap flash entirely. Raise this back toward 24k only
# with fresh measurements, never by intuition (this constant has now been wrong
# in that direction once).
_DEEPSEEK_JSON_EMPTY_CHARS = 16_000
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


# 2026-09-12: the "upgrade big JSON prompts to deepseek-v4-pro" escalation
# (deepseek_model_for_json, 2026-07-21 → 09-12) is GONE. DeepSeek retires
# v4-pro on 2026-09-14 (requests are routed to V4.1-Flash at flash pricing),
# so the swap would have become a silent no-op that still BOOKED pro's 4x
# price against the caps. And it no longer helped: on Stepan's real 112k-char
# multi-turn JSON prompt V4.1-Flash and v4-pro both returned an all-whitespace
# body (N=5 + N=1, both thinking modes). The free gemini rotation ahead of
# deepseek serves that prompt 5/5 — deepseek is the fallback, not the fix.
# is_deepseek_big_json_prompt stays: it still drives the savings-side chain
# reorder (run_chat) and the thinking-keep condition below.

# Models that take DeepSeek's `thinking` body param. deepseek-flash (V4.1) is
# the new name for the same hybrid family; deepseek-reasoner IS the thinking
# mode and legacy names pre-date the param, so they are deliberately outside.
_DEEPSEEK_HYBRID_PREFIXES = ("deepseek-v4", "deepseek-flash")


class _DeepseekAdapter(ProviderAdapter):
    def prepare(self, model: str, kwargs: dict[str, Any],
                capability: str | None = None) -> None:
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
        if tail.startswith(_DEEPSEEK_HYBRID_PREFIXES):
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
    def prepare(self, _model: str, kwargs: dict[str, Any],
                capability: str | None = None) -> None:
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
    "zai": _ZaiAdapter(),
}
_DEFAULT_ADAPTER = ProviderAdapter()


def adapter_for(provider: str) -> ProviderAdapter:
    """The adapter for `provider` (bare name, e.g. 'deepseek'), or a no-op
    default. `provider` is `model.split('/', 1)[0]` at the call site."""
    return _ADAPTERS.get(provider, _DEFAULT_ADAPTER)
