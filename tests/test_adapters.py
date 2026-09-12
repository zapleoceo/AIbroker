"""Per-provider adapters — request-shape quirks + per-key extras."""
from __future__ import annotations

import json

import pytest

from aibroker.providers.adapters import (
    ProviderAdapter,
    adapter_for,
    is_deepseek_big_json_prompt,
)

_SCHEMA = {"type": "json_schema", "json_schema": {"name": "r", "strict": True,
           "schema": {"type": "object", "properties": {"ok": {"type": "boolean"}},
                      "required": ["ok"], "additionalProperties": False}}}


def _prepared(provider: str, **kwargs) -> dict:
    """Run the provider's adapter.prepare over `kwargs` and return the result."""
    adapter_for(provider).prepare(f"{provider}/model", kwargs)
    return kwargs


def test_deepseek_downgrades_json_schema_to_json_object():
    """REGRESSION (2026-07-07): deepseek 400s on json_schema ('This
    response_format type is unavailable now') but accepts json_object — every
    triage call to deepseek was wasted. The deepseek adapter downgrades it."""
    out = _prepared("deepseek", response_format=dict(_SCHEMA))
    assert out["response_format"] == {"type": "json_object"}


@pytest.mark.parametrize("provider", ["deepseek", "cerebras"])
def test_json_schema_downgrade_inlines_the_schema_into_the_last_user_message(provider):
    """REGRESSION (2026-09-12): the downgrade used to DROP the schema. DeepSeek
    then 400'd every call whose prompt lacks the word "json" ("Prompt must
    contain the word 'json' in some form to use 'response_format' of type
    'json_object'") — 30 wasted attempts in one burst on vera's summariser —
    and even when it didn't, the model no longer knew the required keys. The
    schema now rides along as text at the END of the last user message (so
    the provider's prompt-cache prefix is untouched), and the caller's own
    message objects are never mutated (run_chat reuses them for the next
    provider)."""
    msgs = [{"role": "system", "content": "Summarise the session."},
            {"role": "user", "content": "Here is the transcript..."}]
    kwargs: dict = {"messages": msgs, "response_format": dict(_SCHEMA)}
    adapter_for(provider).prepare(f"{provider}/model", kwargs)
    assert kwargs["response_format"] == {"type": "json_object"}
    tail = kwargs["messages"][-1]["content"]
    assert tail.startswith("Here is the transcript...")
    assert "JSON schema" in tail and '"ok"' in tail          # word json + shape
    assert kwargs["messages"][0]["content"] == "Summarise the session."
    assert msgs[1]["content"] == "Here is the transcript..."  # caller untouched


def test_json_schema_downgrade_handles_list_content_and_no_user_turn():
    from aibroker.providers.adapters import downgrade_json_schema
    listy: dict = {"messages": [{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
                   "response_format": dict(_SCHEMA)}
    downgrade_json_schema(listy)
    blocks = listy["messages"][0]["content"]
    assert blocks[0] == {"type": "text", "text": "hi"} and "JSON schema" in blocks[-1]["text"]
    system_only: dict = {"messages": [{"role": "system", "content": "x"}],
                         "response_format": dict(_SCHEMA)}
    downgrade_json_schema(system_only)
    assert system_only["messages"][-1]["role"] == "user"
    assert "JSON schema" in system_only["messages"][-1]["content"]
    plain: dict = {"messages": [{"role": "user", "content": "hi"}],
                   "response_format": {"type": "json_object"}}
    downgrade_json_schema(plain)
    assert plain["messages"][0]["content"] == "hi"           # json_object: no-op


def test_deepseek_leaves_json_object_and_no_format_alone():
    assert _prepared("deepseek", response_format={"type": "json_object"})[
        "response_format"] == {"type": "json_object"}
    assert "response_format" not in _prepared("deepseek")


def test_deepseek_v4_disables_thinking():
    """2026-07-17: v4 models default to THINKING mode — hidden reasoning ate
    the max_tokens budget and truncated short JSON replies to empty (the
    07-10 'v4-flash regression' was this default, not the model). The adapter
    sets the documented body param on every v4-* call."""
    kwargs: dict = {}
    adapter_for("deepseek").prepare("deepseek/deepseek-v4-flash", kwargs)
    assert kwargs["extra_body"]["thinking"] == {"type": "disabled"}
    kwargs = {}
    adapter_for("deepseek").prepare("deepseek/deepseek-v4-pro", kwargs)
    assert kwargs["extra_body"]["thinking"] == {"type": "disabled"}


def test_deepseek_flash_v41_is_treated_as_the_same_hybrid_family():
    """2026-09-12: DeepSeek-V4.1-Flash ships under the NEW name deepseek-flash
    (no "v4" in it). It is the same hybrid thinking-by-default family, so the
    thinking knob must apply — a prefix check on "deepseek-v4" alone would have
    silently re-enabled thinking on every call after the rename and brought
    back the 2026-07-10 empty-JSON regression. Both the disable path and the
    thinking-keep path must match the v4 behaviour exactly."""
    kwargs: dict = {}
    adapter_for("deepseek").prepare("deepseek/deepseek-flash", kwargs)
    assert kwargs["extra_body"]["thinking"] == {"type": "disabled"}
    keep: dict = {"messages": [{"role": "system", "content": "x" * 25_000},
                               {"role": "user", "content": "hi"}],
                  "max_tokens": 2000, "response_format": {"type": "json_object"}}
    adapter_for("deepseek").prepare("deepseek/deepseek-flash", keep)
    assert "extra_body" not in keep          # thinking kept, like v4-flash
    assert keep["max_tokens"] == 3000        # headroom raised, like v4-flash


def _v4_kwargs(*, sys_chars: int, rf_type: str | None, mt: int) -> dict:
    kwargs: dict = {"messages": [{"role": "system", "content": "x" * sys_chars},
                                 {"role": "user", "content": "hi"}],
                    "max_tokens": mt}
    if rf_type:
        kwargs["response_format"] = {"type": rf_type}
    adapter_for("deepseek").prepare("deepseek/deepseek-v4-flash", kwargs)
    return kwargs


def test_deepseek_v4_keeps_thinking_for_huge_json_prompts():
    """REGRESSION (2026-07-17, minutes after the migration): non-thinking v4
    == deepseek-chat, which returns a deterministically EMPTY json_object body
    on ~30k-char prompts (8 EmptyBody on Stepan followups, input billed for
    nothing). Thinking mode demonstrably works there (482 prod calls, 0 empty)
    — so for json + huge prompt + roomy max_tokens the adapter must NOT
    disable it."""
    out = _v4_kwargs(sys_chars=25_000, rf_type="json_object", mt=2000)
    assert "extra_body" not in out  # thinking left at its (enabled) default


def test_deepseek_v4_thinking_bumps_max_tokens_for_headroom():
    """REGRESSION (2026-07-21, same day as the pro-keeps-thinking fix): at
    Stepan's real max_tokens=2000, reasoning_content routinely ate nearly the
    whole budget (usage_log: EmptyBody/InvalidJSON calls averaged 1662-1936
    output tokens, right up against the 2000 cap; clean successes averaged
    only 1202) — reasoning starves the visible JSON body of room to complete.
    Live A/B on a real prompt (N=6, thinking enabled): mt=2000 → 1/6 bad;
    mt=3000 → 0/6 bad AND faster (18s vs 28s avg — no truncation dead-end).
    So the thinking-keep path raises max_tokens to the verified floor."""
    out = _v4_kwargs(sys_chars=25_000, rf_type="json_object", mt=2000)
    assert out["max_tokens"] == 3000
    # a caller who already asks for MORE than the floor keeps their own value
    out = _v4_kwargs(sys_chars=25_000, rf_type="json_object", mt=5000)
    assert out["max_tokens"] == 5000
    # thinking disabled (below the size/mt gates) → max_tokens left untouched
    out = _v4_kwargs(sys_chars=5_000, rf_type="json_object", mt=2000)
    assert out["max_tokens"] == 2000


def test_deepseek_v4_disables_thinking_below_the_size_or_mt_gates():
    # small prompt → disabled even for json
    assert _v4_kwargs(sys_chars=5_000, rf_type="json_object", mt=2000)[
        "extra_body"]["thinking"] == {"type": "disabled"}
    # huge prompt but NO json → disabled (plain text has no empty-body bug)
    assert _v4_kwargs(sys_chars=25_000, rf_type=None, mt=2000)[
        "extra_body"]["thinking"] == {"type": "disabled"}
    # huge json prompt but tiny max_tokens → thinking would starve the content
    # itself (the 07-10 mt=120 failure) → disabled
    assert _v4_kwargs(sys_chars=25_000, rf_type="json_object", mt=120)[
        "extra_body"]["thinking"] == {"type": "disabled"}


def test_deepseek_v4_pro_keeps_thinking_for_huge_json_prompts():
    """REGRESSION (shipped for a few hours 2026-07-21): pro was first wired to
    always run no-thinking, based on an N=3 test on one flat-prompt shape that
    showed 0/3 empty. Re-tested at N=6 against a REAL multi-turn (19-message)
    reply prompt and got the opposite result: pro no-thinking empties 6/6
    (100% — worse than pre-upgrade flash), pro WITH thinking empties only 2/6.
    So pro gets the same thinking-keep condition as flash, not a special-cased
    override — the reasoning pass is what makes DeepSeek actually emit the body
    on multi-turn dialogs, for either v4 variant."""
    kwargs: dict = {"messages": [{"role": "system", "content": "x" * 25_000},
                                 {"role": "user", "content": "hi"}],
                    "max_tokens": 2000, "response_format": {"type": "json_object"}}
    adapter_for("deepseek").prepare("deepseek/deepseek-v4-pro", kwargs)
    assert "extra_body" not in kwargs  # thinking left at its (enabled) default


def test_deepseek_non_v4_models_get_no_thinking_param():
    """deepseek-reasoner IS the thinking mode and legacy names pre-date the
    param — sending it there risks a 400."""
    for model in ("deepseek/deepseek-chat", "deepseek/deepseek-reasoner"):
        kwargs: dict = {}
        adapter_for("deepseek").prepare(model, kwargs)
        assert "extra_body" not in kwargs, model


def test_deepseek_v4_thinking_respects_caller_extra_body():
    """setdefault semantics: a caller-set thinking value wins, and unrelated
    extra_body keys survive."""
    kwargs: dict = {"extra_body": {"thinking": {"type": "enabled"}, "x": 1}}
    adapter_for("deepseek").prepare("deepseek/deepseek-v4-flash", kwargs)
    assert kwargs["extra_body"] == {"thinking": {"type": "enabled"}, "x": 1}


_IMG = [{"role": "user", "content": [
    {"type": "text", "text": "Describe this image."},
    {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,/9j/4AAQ"}}]}]


def test_sambanova_image_requests_bypass_litellms_native_provider():
    """REGRESSION (2026-09-12, caught minutes after deploy): litellm's
    `sambanova/` provider flattens content lists to strings, so the image never
    reached gemma-4-31B-it and it answered "please provide the image" with a
    200 — a non-answer the vision chain would have shipped to callers as a
    description. Image requests must go through the OpenAI-compatible client
    with SambaNova's base URL, where the same model describes the image."""
    kwargs: dict = {"messages": _IMG, "api_key": "k"}
    adapter_for("sambanova").prepare("sambanova/gemma-4-31B-it", kwargs, "vision")
    assert kwargs["model"] == "openai/gemma-4-31B-it"
    assert kwargs["api_base"] == "https://api.sambanova.ai/v1"
    assert kwargs["api_key"] == "k"
    assert kwargs["messages"] is _IMG          # content untouched, image kept


def test_sambanova_text_requests_stay_on_the_native_provider():
    """Text (incl. JSON) is proven on the native provider — do not reroute it.
    A content LIST without an image is also text (litellm flattens it fine)."""
    for msgs in ([{"role": "user", "content": "hi"}],
                 [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]):
        kwargs: dict = {"messages": msgs, "response_format": {"type": "json_object"}}
        adapter_for("sambanova").prepare("sambanova/gemma-4-31B-it", kwargs, "prefilter")
        assert "model" not in kwargs and "api_base" not in kwargs, msgs


_BIG = [{"role": "system", "content": "x" * 25_000}, {"role": "user", "content": "hi"}]
_SMALL = [{"role": "system", "content": "x" * 5_000}, {"role": "user", "content": "hi"}]


def test_is_deepseek_big_json_prompt_matches_the_model_upgrade_threshold():
    """Shared with run_chat's savings-side chain reorder
    (deprioritize_deepseek_for_savings) — must agree with
    deepseek_model_for_json's own threshold check, not drift as a second
    copy."""
    assert is_deepseek_big_json_prompt({"type": "json_object"}, _BIG) is True
    assert is_deepseek_big_json_prompt(dict(_SCHEMA), _BIG) is True
    assert is_deepseek_big_json_prompt({"type": "json_object"}, _SMALL) is False
    assert is_deepseek_big_json_prompt(None, _BIG) is False


def test_deepseek_gray_zone_prompts_count_as_big():
    """REGRESSION (2026-07-23): the threshold was 24k, chosen as a "safe
    margin" below a 30k failure probe — but the 16k-30k range was never
    measured and flash actually empties well below 24k. Live Stepan traffic:
    median prompt 23515 chars with 11/30 sitting in the 16k-24k band, all
    landing on flash and emptying (~27 consecutive empties at tokens_in
    7036-7084). Anything at/above the verified-good 16k point must count as
    big (it drives the savings-side reorder and the thinking-keep path), so
    this band can't silently regress. (The v4-pro escalation this once also
    gated was removed 2026-09-12 — pro is retired 09-14 and measured no
    better on V4.1.)"""
    for size in (16_000, 20_000, 23_500):
        msgs = [{"role": "system", "content": "x" * size}]
        assert is_deepseek_big_json_prompt({"type": "json_object"}, msgs) is True, size
    # just under the verified-good point still rides cheap flash
    just_under = [{"role": "system", "content": "x" * 15_999}]
    assert is_deepseek_big_json_prompt({"type": "json_object"}, just_under) is False


def test_no_deepseek_model_rewrite_survives():
    """2026-09-12: the v4-pro escalation is gone — nothing in adapters may
    rewrite a deepseek model name any more. DeepSeek routes v4-pro to
    V4.1-Flash from 09-14, so a rewrite would have booked pro's 4x price for
    a flash answer; and on the real 112k-char prompt pro emptied just like
    flash. Guard against it quietly coming back under the old name."""
    import aibroker.providers.adapters as adapters
    assert not hasattr(adapters, "deepseek_model_for_json")


def test_cerebras_downgrades_json_schema_to_json_object():
    """REGRESSION (2026-07-11): cerebras 400s on json_schema whose array fields
    carry keywords it doesn't implement ('Invalid fields for schema with types
    ['array']: {'maxItems'}', ~194 BadRequests/45min on Stepan's chat:smart).
    Drop to json_object — the JSON gate + caller validation cover grammar."""
    schema = {"type": "json_schema", "json_schema": {"name": "r", "schema": {
        "type": "object", "properties": {"tags": {"type": "array",
        "maxItems": 5, "items": {"type": "string"}}}}}}
    out = _prepared("cerebras", response_format=schema)
    assert out["response_format"] == {"type": "json_object"}


def test_cerebras_leaves_json_object_and_no_format_alone():
    assert _prepared("cerebras", response_format={"type": "json_object"})[
        "response_format"] == {"type": "json_object"}
    assert "response_format" not in _prepared("cerebras")


def test_anthropic_upgrades_json_object_to_permissive_schema():
    """REGRESSION (2026-07-10): Claude ignores response_format=json_object
    (litellm drops it) and sometimes replies in plain text on follow-ups →
    InvalidJSON. The anthropic adapter upgrades json_object to a PERMISSIVE
    json_schema so litellm uses Claude's native tool-use (guaranteed JSON),
    while additionalProperties keeps the caller's own fields."""
    out = _prepared("anthropic", response_format={"type": "json_object"})
    rf = out["response_format"]
    assert rf["type"] == "json_schema"
    assert rf["json_schema"]["schema"] == {"type": "object", "additionalProperties": True}


def test_anthropic_leaves_real_json_schema_and_no_format_alone():
    # a caller-supplied json_schema is already tool-use-capable — don't touch it
    assert _prepared("anthropic", response_format=dict(_SCHEMA))["response_format"] == _SCHEMA
    assert "response_format" not in _prepared("anthropic")


def test_anthropic_forces_json_on_every_lane_including_sales():
    """REGRESSION (shipped ~2h, 2026-07-26): chat:sales was exempted from the
    forced-JSON rewrite so Sonnet would keep reasoning (forced tool-use
    suppresses thinking — the two are mutually exclusive on this model).
    Production killed the trade-off: 44% InvalidJSON (19 of 43 billed calls
    unusable). Sampling the bodies showed Claude doesn't *almost* produce JSON
    there — it ignores the instruction entirely and answers in prose (3/3 plain
    Bahasa replies on the real 81k-char sales prompt). The earlier 3/3-valid
    measurement used a short prompt literally saying "reply ONLY with JSON",
    which does not survive the real one. The guarantee wins on EVERY lane."""
    for cap in ("chat:sales", "chat:smart", "chat:code", "chat:edit", None):
        kwargs = {"response_format": {"type": "json_object"}}
        adapter_for("anthropic").prepare("anthropic/claude-sonnet-5", kwargs, cap)
        assert kwargs["response_format"]["type"] == "json_schema", cap
        assert "reasoning_effort" not in kwargs, cap


def test_anthropic_unwraps_the_forced_tool_envelope():
    """Claude has no native json_object mode, so we upgrade to json_schema and
    LiteLLM serves it via a FORCED TOOL CALL. Sonnet intermittently returns the
    tool *input* inside a generic envelope — {"parameters": {…}} — and LiteLLM
    forwards it verbatim (it unwraps only a "values" wrapper, litellm#6741).
    Measured live: ~half of chat:sales replies arrived enveloped, forcing the
    client to unwrap. The broker unwraps ONE level so callers get the real
    object."""
    a = adapter_for("anthropic")
    rf = {"type": "json_object"}
    for key in ("parameters", "arguments", "input"):
        body = json.dumps({key: {"reply": "halo", "move": "ask_budget"}})
        assert json.loads(a.normalize_json_text(body, rf)) == {
            "reply": "halo", "move": "ask_budget"}
    # unicode survives the round-trip un-escaped
    out = a.normalize_json_text(json.dumps({"parameters": {"reply": "привет"}}), rf)
    assert json.loads(out) == {"reply": "привет"}


def test_anthropic_envelope_unwrap_only_on_an_unambiguous_shape():
    """Guards — anything that could legitimately BE the caller's own object is
    returned byte-identical, so the unwrap can never eat real data."""
    a = adapter_for("anthropic")
    rf = {"type": "json_object"}
    clean = json.dumps({"reply": "halo"})
    assert a.normalize_json_text(clean, rf) == clean          # already clean
    sibling = json.dumps({"parameters": {"a": 1}, "other": 2})
    assert a.normalize_json_text(sibling, rf) == sibling      # sibling keys
    inner_scalar = json.dumps({"parameters": "not-an-object"})
    assert a.normalize_json_text(inner_scalar, rf) == inner_scalar
    assert a.normalize_json_text("[1, 2]", rf) == "[1, 2]"    # array body
    assert a.normalize_json_text("plain prose", rf) == "plain prose"
    # plain-text request → never touched, even if it happens to look enveloped
    assert a.normalize_json_text(json.dumps({"input": {"x": 1}}), None) ==         json.dumps({"input": {"x": 1}})
    # a schema that legitimately declares the key keeps it
    schema_rf = {"type": "json_schema", "json_schema": {"schema": {
        "type": "object", "properties": {"input": {"type": "object"}}}}}
    declared = json.dumps({"input": {"x": 1}})
    assert a.normalize_json_text(declared, schema_rf) == declared


def test_default_adapter_never_touches_the_body():
    """The hook is opt-in: every non-anthropic provider returns the body
    unchanged, so this can't silently reshape another provider's JSON."""
    for provider in ("deepseek", "gemini", "cerebras", "unknown-provider"):
        body = json.dumps({"parameters": {"reply": "x"}})
        assert adapter_for(provider).normalize_json_text(
            body, {"type": "json_object"}) == body, provider


def test_schema_capable_providers_keep_json_schema():
    """openai/gemini support json_schema — the adapter must NOT downgrade it."""
    assert _prepared("openai", response_format=dict(_SCHEMA))["response_format"] == _SCHEMA
    assert _prepared("gemini", response_format=dict(_SCHEMA))["response_format"] == _SCHEMA


def test_gemini_disables_thinking_unconditionally():
    """2026-07-10: gemini thinking is disabled on EVERY call, not just JSON —
    its thinking truncated JSON AND added latency that overran the call timeout.
    The broker never wants gemini to deep-reason (that's chat:deep/nvidia)."""
    assert _prepared("gemini", response_format={"type": "json_object"}
                     )["reasoning_effort"] == "disable"
    assert _prepared("gemini", response_format=dict(_SCHEMA)
                     )["reasoning_effort"] == "disable"
    # non-JSON and no-format calls also get thinking disabled now
    assert _prepared("gemini")["reasoning_effort"] == "disable"
    assert _prepared("gemini", response_format=None)["reasoning_effort"] == "disable"


def test_non_special_provider_is_noop():
    """A provider with no adapter (e.g. groq) gets the default no-op — kwargs
    pass through untouched."""
    out = _prepared("groq", response_format=dict(_SCHEMA), temperature=0.7)
    assert out["response_format"] == _SCHEMA
    assert "reasoning_effort" not in out


def test_cloudflare_key_extra_builds_api_base():
    extra = adapter_for("cloudflare").key_extra("865824c3e1d2ced02b16adb355616363")
    assert extra == {"api_base":
                     "https://api.cloudflare.com/client/v4/accounts/"
                     "865824c3e1d2ced02b16adb355616363/ai/run/"}


def test_cloudflare_key_extra_none_without_account_id():
    assert adapter_for("cloudflare").key_extra(None) is None
    assert adapter_for("cloudflare").key_extra("") is None


def _prepared_model(provider: str, model: str, **kwargs) -> dict:
    """Like _prepared, but for quirks that branch on the MODEL, not the provider."""
    adapter_for(provider).prepare(model, kwargs)
    return kwargs


def test_gemini_thinking_value_is_model_aware():
    """gemini-3.7+ hard-400s on the MINIMAL level that reasoning_effort='disable'
    maps to ('Thinking level MINIMAL is not supported for this model', measured
    live 2026-08-16), so it must get 'low' instead. Older models keep 'disable';
    both spend ~nothing on reasoning (out=1 on a trivial prompt either way)."""
    assert _prepared_model("gemini", "gemini/gemini-3.7-flash"
                           )["reasoning_effort"] == "low"
    assert _prepared_model("gemini", "gemini/gemini-2.5-flash"
                           )["reasoning_effort"] == "disable"
    assert _prepared_model("gemini", "gemini/gemini-3.6-flash"
                           )["reasoning_effort"] == "disable"


def test_zai_disables_thinking():
    """GLM defaults to thinking mode and burns the entire max_tokens budget on
    hidden reasoning, returning an empty body (measured 2026-08-16: out=64 /
    text='' as-is vs out=2 / text='ok' with thinking off, on BOTH 4.5 and 4.7).
    That silently starved 7 live zai keys down to ~15 calls a week."""
    assert _prepared("zai")["extra_body"]["thinking"] == {"type": "disabled"}
    # An explicit caller value wins — the adapter only supplies a default.
    kept = _prepared("zai", extra_body={"thinking": {"type": "enabled"}})
    assert kept["extra_body"]["thinking"] == {"type": "enabled"}


def test_default_adapter_key_extra_is_none():
    assert adapter_for("cerebras").key_extra("anything") is None
    assert isinstance(adapter_for("nonexistent"), ProviderAdapter)
