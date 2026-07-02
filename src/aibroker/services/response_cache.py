"""In-process exact-match response cache for deterministic capabilities.

Scoped to `translate`: the same short phrases recur verbatim, and the
translation of a fixed (text, target-language) input is stable — returning a
cached answer is correct and skips a whole LLM round-trip. NOT used for chat/*:
those aren't deterministic, so a stale cached answer would be wrong.

Per-process (each broker replica keeps its own copy). translate volume is low
(~150/day) so a small LRU+TTL is proportionate — no shared store or migration
needed; cross-replica misses are acceptable for a cheap capability.
"""
from __future__ import annotations

import hashlib
import json
import time
from collections import OrderedDict
from typing import Any

_MAX_ENTRIES = 2_000
_TTL_S = 24 * 60 * 60  # a phrase's translation is stable for a day

# Only deterministic capabilities may be cached. chat/* etc. must never be here.
_CACHEABLE: frozenset[str] = frozenset({"translate"})

# key -> (stored_at_epoch, response_text)
_store: OrderedDict[str, tuple[float, str]] = OrderedDict()


def is_cacheable(capability: str) -> bool:
    return capability in _CACHEABLE


def _key(
    capability: str, messages: list[dict[str, Any]],
    model: str | None, max_tokens: int, temperature: float,
) -> str:
    """Hash the full request signature — same inputs must map to the same key,
    different sampling params must not collide."""
    payload = json.dumps(
        [capability, messages, model, max_tokens, temperature],
        sort_keys=True, ensure_ascii=False, default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def get(
    capability: str, messages: list[dict[str, Any]], *,
    model: str | None, max_tokens: int, temperature: float,
) -> str | None:
    """Cached response text for this exact request, or None (miss/expired/
    not-cacheable)."""
    if not is_cacheable(capability):
        return None
    k = _key(capability, messages, model, max_tokens, temperature)
    hit = _store.get(k)
    if hit is None:
        return None
    stored_at, text = hit
    if time.time() - stored_at > _TTL_S:
        _store.pop(k, None)
        return None
    _store.move_to_end(k)  # LRU touch
    return text


def put(
    capability: str, messages: list[dict[str, Any]], text: str, *,
    model: str | None, max_tokens: int, temperature: float,
) -> None:
    """Store a successful response. No-op for non-cacheable capabilities or
    empty output."""
    if not is_cacheable(capability) or not text:
        return
    k = _key(capability, messages, model, max_tokens, temperature)
    _store[k] = (time.time(), text)
    _store.move_to_end(k)
    while len(_store) > _MAX_ENTRIES:
        _store.popitem(last=False)  # evict oldest


def clear() -> None:
    """Drop everything — for tests."""
    _store.clear()
