"""Per-provider adapters — request-shape quirks + per-key extras."""
from __future__ import annotations

from aibroker.providers.adapters import ProviderAdapter, adapter_for

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
