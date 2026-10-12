"""Shared constants for provider definitions."""
from __future__ import annotations

from aibroker.providers.registry import ProbeSpec

OSS = "gpt-oss-120b"
CHAT_USER_PING = [{"role": "user", "content": "."}]


def chat_probe(url: str, model: str, **kw) -> ProbeSpec:
    return ProbeSpec(url, {"model": model, "messages": CHAT_USER_PING, "max_tokens": 1}, **kw)
