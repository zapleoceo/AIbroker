"""In-process exact-match response cache for deterministic capabilities.

Scoped to `translate`, `prefilter` and (since 2026-09-12) `vision`: the same
inputs recur verbatim and the answer for a fixed input is stable — returning
a cached answer is correct and skips a whole LLM round-trip. NOT used for
chat/*: those aren't deterministic, so a stale cached answer would be wrong.

Per-process (each broker replica keeps its own copy). Volumes are low enough
that a small LRU+TTL is proportionate — no shared store or migration needed;
cross-replica misses are acceptable for cheap capabilities.
"""
from __future__ import annotations

import hashlib
import json
import time
from collections import OrderedDict
from typing import Any

_MAX_ENTRIES = 2_000

# Only deterministic capabilities may be cached — chat/* etc. must never be
# here. Per-capability TTL:
#   - translate: a phrase's translation is stable for a day.
#   - prefilter (2026-07-12): classifies inbound lead messages; identical
#     short messages ('ok', 'thanks', emoji) recur heavily and the verdict is
#     deterministic-enough at temperature 0 — but kept SHORT (10 min) so a
#     prompt/threshold change rolls through quickly.
#   - vision (2026-09-12): a description of the SAME bytes with the SAME
#     prompt is stable enough, and vera resubmits identical images after its
#     15-minute poll deadline or on its periodic re-queue — 349 jobs for 273
#     distinct payloads in one day, ~76 local-slot runs (≈3.3h of CPU) spent
#     re-describing pictures already described. The job-level dedup only
#     covers in-flight duplicates inside 30 minutes; this covers finished
#     ones for a day. Keyed on the full payload hash, so a different prompt
#     or image is a different entry.
_TTL_S: dict[str, int] = {
    "translate": 24 * 60 * 60,
    "prefilter": 10 * 60,
    "vision": 24 * 60 * 60,
}
_CACHEABLE: frozenset[str] = frozenset(_TTL_S)

# key -> (stored_at_epoch, response_text)
_store: OrderedDict[str, tuple[float, str]] = OrderedDict()


def is_cacheable(capability: str) -> bool:
    return capability in _CACHEABLE


def _key(
    capability: str, messages: list[dict[str, Any]],
    model: str | None, max_tokens: int, temperature: float, project_id: int,
) -> str:
    """Hash the full request signature — same inputs must map to the same key,
    different sampling params must not collide.

    `project_id` is part of the key (2026-09-07 review). Without it, two
    projects sending the same text shared one cached answer — and `prefilter`
    classifies inbound lead messages, so that was one tenant's answer about
    its lead being served to another tenant for the 10-minute TTL. Every
    other layer of this broker is project-scoped; the cache was the exception."""
    payload = json.dumps(
        [project_id, capability, messages, model, max_tokens, temperature],
        sort_keys=True, ensure_ascii=False, default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def get(
    capability: str, messages: list[dict[str, Any]], *,
    model: str | None, max_tokens: int, temperature: float, project_id: int,
) -> str | None:
    """Cached response text for this exact request, or None (miss/expired/
    not-cacheable)."""
    if not is_cacheable(capability):
        return None
    k = _key(capability, messages, model, max_tokens, temperature, project_id)
    hit = _store.get(k)
    if hit is None:
        return None
    stored_at, text = hit
    if time.time() - stored_at > _TTL_S[capability]:
        _store.pop(k, None)
        return None
    _store.move_to_end(k)  # LRU touch
    return text


def put(
    capability: str, messages: list[dict[str, Any]], text: str, *,
    model: str | None, max_tokens: int, temperature: float, project_id: int,
) -> None:
    """Store a successful response. No-op for non-cacheable capabilities or
    empty output."""
    if not is_cacheable(capability) or not text:
        return
    k = _key(capability, messages, model, max_tokens, temperature, project_id)
    _store[k] = (time.time(), text)
    _store.move_to_end(k)
    while len(_store) > _MAX_ENTRIES:
        _store.popitem(last=False)  # evict oldest


def clear() -> None:
    """Drop everything — for tests."""
    _store.clear()
