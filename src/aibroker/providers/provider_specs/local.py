"""Definition for the local provider."""
from __future__ import annotations

from aibroker.providers.registry import Quota, build_provider

SPEC = build_provider(
    "local", rank=5, cooldown_base_s=30,   # a timeout = busy decode lock, not a dead credential
    transport_factories={"local_vision": "aibroker.providers.local_vision:LocalVisionTransport"},
    empty_is_failure=True,
    quota=Quota(doc="https://github.com/zapleoceo/muai/blob/master/vera3/docs/asr-local.md"),
    defaults={"vision": "local/qwen3vl"},
    model_meta={
        "local/qwen3vl": {"transport": "local_vision", "pricing": "local"},
    },
)
