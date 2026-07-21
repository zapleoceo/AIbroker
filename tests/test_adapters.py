"""Per-provider adapters — request-shape quirks + per-key extras."""
from __future__ import annotations

from aibroker.providers.adapters import (
    ProviderAdapter,
    adapter_for,
    deepseek_model_for_json,
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


def test_deepseek_v4_pro_disables_thinking_even_for_huge_json():
    """v4-pro is chosen ONLY for the big JSON prompts that empty flash, and it
    emits valid JSON there without thinking in ~4.6s (vs ~18.5s with) — so the
    huge-json thinking net is scoped to flash; pro always runs no-thinking."""
    kwargs: dict = {"messages": [{"role": "system", "content": "x" * 25_000},
                                 {"role": "user", "content": "hi"}],
                    "max_tokens": 2000, "response_format": {"type": "json_object"}}
    adapter_for("deepseek").prepare("deepseek/deepseek-v4-pro", kwargs)
    assert kwargs["extra_body"]["thinking"] == {"type": "disabled"}


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


_BIG = [{"role": "system", "content": "x" * 25_000}, {"role": "user", "content": "hi"}]
_SMALL = [{"role": "system", "content": "x" * 5_000}, {"role": "user", "content": "hi"}]


def test_deepseek_model_upgrades_big_json_to_pro():
    """Big JSON prompt empties v4-flash's json_object body (DeepSeek bug); v4-pro
    handles it. The picker upgrades ONLY that case so cost is booked as pro."""
    assert deepseek_model_for_json(
        "deepseek/deepseek-v4-flash", {"type": "json_object"}, _BIG
    ) == "deepseek/deepseek-v4-pro"
    assert deepseek_model_for_json(
        "deepseek/deepseek-v4-flash", dict(_SCHEMA), _BIG
    ) == "deepseek/deepseek-v4-pro"


def test_deepseek_model_keeps_flash_when_upgrade_unwarranted():
    # small JSON prompt → flash works, stays cheap
    assert deepseek_model_for_json(
        "deepseek/deepseek-v4-flash", {"type": "json_object"}, _SMALL
    ) == "deepseek/deepseek-v4-flash"
    # big but NOT json → no empty-body bug, stays flash
    assert deepseek_model_for_json(
        "deepseek/deepseek-v4-flash", None, _BIG
    ) == "deepseek/deepseek-v4-flash"
    # already pinned to a non-flash model → untouched
    assert deepseek_model_for_json(
        "deepseek/deepseek-v4-pro", {"type": "json_object"}, _BIG
    ) == "deepseek/deepseek-v4-pro"
    # non-deepseek / unset model → returned as-is
    assert deepseek_model_for_json(
        "gemini/gemini-2.5-flash", {"type": "json_object"}, _BIG
    ) == "gemini/gemini-2.5-flash"
    assert deepseek_model_for_json(None, {"type": "json_object"}, _BIG) is None


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


def test_default_adapter_key_extra_is_none():
    assert adapter_for("cerebras").key_extra("anything") is None
    assert isinstance(adapter_for("nonexistent"), ProviderAdapter)
