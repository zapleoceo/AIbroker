"""Definition for the anthropic provider."""
from __future__ import annotations

from aibroker.providers.adapters import _AnthropicAdapter
from aibroker.providers.provider_specs.common import CHAT_USER_PING
from aibroker.providers.registry import ProbeSpec, Quota, build_provider

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
)
