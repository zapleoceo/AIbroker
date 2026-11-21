"""Definition for the anthropic provider."""
from __future__ import annotations

from aibroker.providers.adapters import _AnthropicAdapter
from aibroker.providers.provider_specs.common import CHAT_USER_PING
from aibroker.providers.registry import ProbeSpec, Quota, build_provider

_CHAT_LANES = ("chat:fast", "chat:smart", "chat:sales", "chat:code", "chat:edit", "structured")


def _pin_only(note: str) -> dict:
    """A text model callers may pin on chat:* / structured; never walked unpinned.
    Prices come from litellm (verified equal to the official table 2026-11-21).
    Native JSON schema (output_config.format json_schema) is documented for each."""
    return {"capabilities": _CHAT_LANES, "pricing": "litellm",
            "litellm_extra": {"supports_response_schema": True}, "note": note}


# Newest generation, pin-only. Source: platform.claude.com/docs/en/about-claude/models/overview
# and /pricing, 2026-11-21 (per 1M tokens: fable 8/40, opus 4/20, sonnet 2/10,
# haiku 0.10/0.50 for prompts <=100k and 0.50/2.50 above). All read images.
_PINNED_TEXT_MODELS = {
    "anthropic/claude-fable-5-2": _pin_only("Claude Fable 5.2; $8/$40; cache read $0.20"),
    "anthropic/claude-opus-5-5": _pin_only("Claude Opus 5.5; $4/$20; cache read $0.20"),
    "anthropic/claude-sonnet-5-5": _pin_only("Claude Sonnet 5.5; $2/$10; cache read $0.10"),
    "anthropic/claude-haiku-5-5": _pin_only(
        "Claude Haiku 5.5; $0.10/$0.50 up to 100k prompt tokens, $0.50/$2.50 above"),
}

SPEC = build_provider(
    "anthropic", rank=210, paid=True, cache_sticky=True, explicit_cache=True, cooldown_base_s=120,
    native_tools=True,
    adapter=_AnthropicAdapter(), quota_headers="anthropic",
    quota=Quota(doc="https://docs.claude.com/en/docs/about-claude/usage-limits"),
    probe=ProbeSpec("https://api.anthropic.com/v1/messages",
                    {"model": "claude-haiku-4-5", "max_tokens": 1,
                     "messages": CHAT_USER_PING}, auth="anthropic"),
    defaults={"chat:fast": "anthropic/claude-haiku-4-5",
              "chat:smart": "anthropic/claude-sonnet-5",
              "chat:sales": "anthropic/claude-sonnet-5",
              "chat:code": "anthropic/claude-sonnet-5",
              "chat:edit": "anthropic/claude-sonnet-5",
              "structured": "anthropic/claude-haiku-4-5",
              "vision": "anthropic/claude-sonnet-5"},
    model_meta=_PINNED_TEXT_MODELS,
)
