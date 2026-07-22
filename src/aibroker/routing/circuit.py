"""In-process per-provider timeout circuit-breaker (selection-side, fail-open).

A key that times out wasted ~60s of wall-clock and produced no answer. We track
recent timeouts so the selector can (a) soft-skip a free provider whose pool is
timing out in bulk — failing the chain over cheaply with NO call sent — and
(b) not pin cache-affinity to a key that just hung. Per-worker in-process ring:
no DB/Redis dependency, so a restart simply forgets (fail-open — worst case we
send a call we might have skipped). Cites the 2026-07-16 free-pool timeout storm
where hung keys were re-picked for 60s each until the whole chain 503'd.

Empty bodies get the same treatment for a DIFFERENT action — see
`note_empty_body` / `providers_in_empty_storm`: an empty body IS billed (input
tokens charged for no answer), so a provider emitting them in bulk should be
tried AFTER the free tier, not skipped outright (it still answers most calls).
"""
from __future__ import annotations

import time

# How long a timeout keeps a key "recently timed out" for selection. ≈ one
# provider-call timeout ceiling: long enough to keep a hung key out of the next
# few picks, short enough to re-probe once it may have recovered.
_TIMEOUT_MEMORY_S = 120.0

# Empty bodies are cheaper per event than a timeout (no 60s wall-clock burnt)
# but are BILLED, and a provider-side degradation lasts far longer than one
# call: the 2026-07-22 DeepSeek evening degradation ran hours (0% empty
# 11:00-19:00 UTC, then 34-46% from 20:00 on BOTH v4 models, while a trivial
# short prompt still answered fine — i.e. long-context generation degrading
# under provider load, not an outage). A longer memory keeps the deferral
# stable across that, instead of flapping back every 2 minutes.
_EMPTY_MEMORY_S = 600.0

_key_timeouts: dict[int, float] = {}
_provider_timeouts: dict[str, dict[int, float]] = {}
_provider_empties: dict[str, dict[int, float]] = {}


def note_timeout(provider: str, key_id: int) -> None:
    """Record that `key_id` (of `provider`) just timed out."""
    now = time.monotonic()
    _key_timeouts[key_id] = now
    _provider_timeouts.setdefault(provider, {})[key_id] = now


def recent_timeout_key_ids() -> frozenset[int]:
    """Keys that timed out within the memory window (prunes stale entries)."""
    now = time.monotonic()
    for kid in [k for k, ts in _key_timeouts.items() if now - ts >= _TIMEOUT_MEMORY_S]:
        del _key_timeouts[kid]
    return frozenset(_key_timeouts)


def providers_in_timeout_storm(min_keys: int) -> frozenset[str]:
    """Providers with ≥ `min_keys` distinct keys timed out inside the window —
    a degraded free provider the chain should fail over cheaply (prunes)."""
    now = time.monotonic()
    storm: set[str] = set()
    for provider in list(_provider_timeouts):
        fresh = {kid: ts for kid, ts in _provider_timeouts[provider].items()
                 if now - ts < _TIMEOUT_MEMORY_S}
        if fresh:
            _provider_timeouts[provider] = fresh
            if len(fresh) >= min_keys:
                storm.add(provider)
        else:
            del _provider_timeouts[provider]
    return frozenset(storm)


def note_empty_body(provider: str, key_id: int) -> None:
    """Record that `key_id` (of `provider`) just returned an empty body."""
    _provider_empties.setdefault(provider, {})[key_id] = time.monotonic()


def providers_in_empty_storm(min_keys: int) -> frozenset[str]:
    """Providers with ≥ `min_keys` distinct keys that recently returned an
    EMPTY body — a provider-side degradation the chain should route AROUND
    first rather than pay for (prunes stale entries).

    Deliberately NOT wired to the selector's hard skip (unlike a timeout
    storm): an empty-storming provider still answers the majority of calls
    (measured 54-66% during the 2026-07-22 DeepSeek degradation), and hard
    skipping it would push that traffic to the pricier paid tail — anthropic
    is ~7x deepseek per successful reply. The caller uses this to try the FREE
    tier first instead (routing.chains.deprioritize_deepseek_for_savings), so
    the degraded provider stays available as the fallback it still is."""
    now = time.monotonic()
    storm: set[str] = set()
    for provider in list(_provider_empties):
        fresh = {kid: ts for kid, ts in _provider_empties[provider].items()
                 if now - ts < _EMPTY_MEMORY_S}
        if fresh:
            _provider_empties[provider] = fresh
            if len(fresh) >= min_keys:
                storm.add(provider)
        else:
            del _provider_empties[provider]
    return frozenset(storm)


def reset() -> None:
    """Clear all tracked state — test hook."""
    _key_timeouts.clear()
    _provider_timeouts.clear()
    _provider_empties.clear()
