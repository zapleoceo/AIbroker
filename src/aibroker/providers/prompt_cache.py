"""Prompt-cache helpers — anthropic cache_control marks, cache-token parsing and
the stable cache-key request parameter other providers document.

Pure functions, no network. Which providers get explicit marks / which param
carries the stable key is ProviderSpec data (explicit_cache, cache_key_param).
"""
from __future__ import annotations

import hashlib
from typing import Any

from aibroker.providers.registry import spec_or_default

# Anthropic allows at most 4 cache_control breakpoints per request.
_MAX_CACHE_MARKS = 4

# Cache lifetime. anthropic's default `ephemeral` entry lives 5 minutes, with
# the timer REFRESHED on every hit; "1h" buys a 12x wider window per entry.
#
# Justified on live 24h anthropic traffic (2026-07-24), by decomposing what we
# actually PAY to write:
#   full-prefix rewrites (>=15k tok): 38 calls, 949,559 tok  <- 90% of write cost
#   history increments   (<15k tok): 138 calls,  99,595 tok
# i.e. the dominant cost was NOT the per-turn increments but the shared system
# prefix going cold and being re-written 38x/day. A 1h entry removes most of
# those expiries. Break-even for the pricier extended write is a ~37% drop in
# written tokens; with 90% of the volume being expiry-driven that clears
# comfortably. Secondary gain: a lead's follow-up turn lands inside 5 min only
# 73% of the time vs 89% within an hour, so the per-dialogue history breakpoint
# hits more often too (median inter-call gap overall is 19s, per-dialogue 31s).
#
# Cost: an extended-TTL write bills higher than a 5-minute one, and litellm
# CANNOT price that (cost_per_token has no ttl parameter) — the premium is
# applied by _extended_ttl_write_premium so the recorded cost stays truthful and
# the daily caps keep working. Set to None to go back to the 5-minute default;
# the pricing correction disables itself with it.
_CACHE_TTL: str | None = "1h"
_CACHE_TTL_RATE_FIELD = "cache_creation_input_token_cost_above_1hr"


def apply_prompt_cache(
    model: str, messages: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Place anthropic `cache_control` breakpoints on the two stable, repeated
    prefixes of a (multi-turn) request. A breakpoint prefix-caches EVERYTHING
    up to and including it, so:

      1) ONE breakpoint on the END of the leading system run caches the whole
         static system prefix (Stepan's ~22k-char persona), however many
         system messages it spans. Was one-per-system-message (up to 4) — but
         since a breakpoint already caches everything before it, those extra
         marks were wasted slots that cached nothing new.

      2) A second, ROLLING breakpoint on the END of the conversation so far
         caches the growing dialogue history incrementally: next turn the whole
         `[system + prior history]` prefix is a cache read and only the new
         message is fresh. THIS is the multi-turn win — without it ONLY the
         system prefix was ever cached and the whole history was re-billed at
         full input price on every single turn.

         Measured live (Sonnet, real 51k-char Stepan system prefix, each arm
         warmed independently, cache_read tokens on the following turn):
           history  1k chars: old 21205 → new 21696  (+491)   cost -14%
           history 10k chars: old 21205 → new 26016  (+4811)  cost -58%
         The gain scales with history length, which is exactly the long
         multi-turn sales chat / backlog-reprocessing case. Note the small
         increment still caches (491 tokens < anthropic's ~2048 minimum)
         because it extends the already-cached system prefix rather than
         standing alone.

    Anthropic allows `_MAX_CACHE_MARKS` (4) breakpoints; we use at most 2, both
    only on non-empty str content (a marker on a too-small increment or a
    non-byte-stable prefix is silently not cached — harmless). No-op for
    non-caching providers."""
    if not spec_or_default(model.split("/", 1)[0]).explicit_cache:
        return messages

    def _markable(i: int) -> bool:
        c = messages[i].get("content")
        return isinstance(c, str) and bool(c.strip())

    # EVERY leading system message gets its own breakpoint — not just the end
    # of the run. A breakpoint caches everything up to itself, so one terminal
    # mark looks equivalent, but only while the whole run is stable. It isn't:
    # Stepan sends [78,938-char stable persona][~700-char per-lead DOSSIER], so
    # a single terminal mark puts the VARIABLE dossier inside the cache key and
    # no two leads ever share an entry. Measured in production after exactly
    # that mistake shipped: 5.5% hit rate, ~29k tokens re-written on EVERY call,
    # $0.12/call — it drained the $5/day Sonnet cap in hours. Marking each
    # system message restores a breakpoint at the stable/variable boundary
    # wherever it happens to fall; anthropic matches the LONGEST cached prefix,
    # so the extra marks cost nothing and the 78,938-char prefix always hits.
    sys_ends: list[int] = []
    for i, m in enumerate(messages):
        if m.get("role") != "system":
            break
        if _markable(i):
            sys_ends.append(i)
    # End of the whole conversation so far (the rolling history breakpoint).
    hist_end = next((i for i in range(len(messages) - 1, -1, -1) if _markable(i)), -1)

    # Reserve one slot for the history mark, spend the rest on the system run.
    marks = set(sys_ends[:max(_MAX_CACHE_MARKS - 1, 1)])
    if hist_end >= 0:
        marks.add(hist_end)
    marks = set(sorted(marks)[:_MAX_CACHE_MARKS])

    cache_control: dict[str, str] = {"type": "ephemeral"}
    if _CACHE_TTL:
        cache_control["ttl"] = _CACHE_TTL

    out: list[dict[str, Any]] = []
    for i, m in enumerate(messages):
        if i in marks:
            m = {**m, "content": [{
                "type": "text", "text": m["content"],
                "cache_control": dict(cache_control),
            }]}
        out.append(m)
    return out


def _usage_field(usage: Any, name: str) -> int:
    if isinstance(usage, dict):
        return int(usage.get(name) or 0)
    return int(getattr(usage, name, 0) or 0)


def _cache_tokens(usage: Any) -> tuple[int, int]:
    """(read, write) prompt-cache tokens from a LiteLLM usage object. anthropic
    reports cache_read_input_tokens / cache_creation_input_tokens; OpenAI-shape
    providers nest cached reads under prompt_tokens_details.cached_tokens."""
    read = _usage_field(usage, "cache_read_input_tokens")
    write = _usage_field(usage, "cache_creation_input_tokens")
    if not read:
        details = usage.get("prompt_tokens_details") if isinstance(usage, dict) \
            else getattr(usage, "prompt_tokens_details", None)
        if details is not None:
            read = _usage_field(details, "cached_tokens")
    return read, write


def cache_key_for(*parts: object) -> str:
    """Stable, opaque, short key derived from the affinity key — the same
    (project, workflow, capability, pin) always yields the same string, so a
    provider's cache router sends the request family to the same cache."""
    raw = "".join("" if p is None else str(p) for p in parts)
    return "aib-" + hashlib.sha256(raw.encode()).hexdigest()[:32]


def apply_cache_key(provider: str, cache_key: str | None, kwargs: dict[str, Any]) -> None:
    """Add the provider's DOCUMENTED stable-cache-key parameter to `kwargs`, and
    only for providers whose spec names one (cache_key_param). Sent through
    `extra_body` so it reaches the wire verbatim whatever litellm's own param map
    says; a provider with no documented param gets nothing."""
    if not cache_key:
        return
    param = spec_or_default(provider).cache_key_param
    if param:
        kwargs.setdefault("extra_body", {}).setdefault(param, cache_key)
