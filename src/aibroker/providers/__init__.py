"""Provider layer. Heavy modules (litellm) load lazily on first attribute access
so `aibroker.providers.registry` stays a cheap leaf import."""
from __future__ import annotations

from typing import Any

__all__ = ["call_llm", "estimate_llm_cost", "transcribe"]


def __getattr__(name: str) -> Any:
    if name in ("call_llm", "transcribe"):
        from aibroker.providers import transport
        return getattr(transport, name)
    if name == "estimate_llm_cost":
        from aibroker.providers.cost import estimate_llm_cost
        return estimate_llm_cost
    raise AttributeError(name)
