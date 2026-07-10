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
    """A provider with no adapter (e.g. cerebras) gets the default no-op — kwargs
    pass through untouched."""
    out = _prepared("cerebras", response_format=dict(_SCHEMA), temperature=0.7)
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
