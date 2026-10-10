"""capability -> adapter factory. One adapter instance per run (cancel() targets that run).
Nothing is registered by default: with no factory a run fails `adapter_unavailable`."""
from __future__ import annotations

from collections.abc import Callable

from aibroker.agent.adapter import AgentCLI

_FACTORIES: dict[str, Callable[[], AgentCLI]] = {}


def register_adapter(capability: str, factory: Callable[[], AgentCLI]) -> None:
    _FACTORIES[capability] = factory


def clear_adapters() -> None:
    _FACTORIES.clear()


def make_adapter(capability: str) -> AgentCLI | None:
    factory = _FACTORIES.get(capability)
    return factory() if factory else None
