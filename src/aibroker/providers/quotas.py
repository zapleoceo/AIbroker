"""Per-key daily quota resolution + saturation math.

A key can be capped on four independent axes per day:
  - requests
  - total tokens (in + out)
  - input tokens
  - output tokens

For each axis the effective limit is resolved by priority:
  1. manual_*      — operator-set override (e.g. corporate Gemini 3M in / 80k out)
  2. discovered_*  — parsed from provider response headers at key creation
  3. ProviderSpec.quota default — static guess from provider docs

Whichever axis is closest to its cap drives the dashboard bar and the
selector's saturation skip. Sourced from provider docs as of 2026-06-28;
verify in the provider console — defaults drift, manual override is exact.
"""
from __future__ import annotations

from aibroker.providers.registry import Quota, spec_or_default

# Static provider defaults live on ProviderSpec.quota (providers/specs.py).


def quota_for(provider: str) -> Quota:
    """Static provider default; empty Quota for unknown providers."""
    return spec_or_default(provider).quota


def quota_for_key(key) -> Quota:
    """Effective per-key Quota, resolving manual > discovered > default per axis.
    `key` is any object exposing the column attrs (ApiKeyRow in prod;
    SimpleNamespace in tests)."""
    base = quota_for(getattr(key, "provider", ""))
    # ProviderSpec.quota seeds are FREE-tier limits; a paid key isn't bound by them
    # (its real caps are orders higher), so a paid gemini key must not read as
    # 212% of the 1,500 free RPD. Drop the seed for paid keys — only explicit
    # manual/discovered axes remain; the $/day cost cap is a separate column.
    if getattr(key, "tier", "") == "paid":
        base = Quota(doc=base.doc)

    def pick(*vals: int | None) -> int | None:
        for v in vals:
            if v is not None:
                return v
        return None

    return Quota(
        req_per_day=pick(
            getattr(key, "manual_req_limit", None),
            getattr(key, "discovered_req_limit", None),
            base.req_per_day,
        ),
        tok_per_day=pick(
            getattr(key, "manual_tok_limit", None),
            getattr(key, "discovered_tok_limit", None),
            base.tok_per_day,
        ),
        tok_in_per_day=pick(getattr(key, "manual_tok_in_limit", None),
                             base.tok_in_per_day),
        tok_out_per_day=pick(getattr(key, "manual_tok_out_limit", None),
                              base.tok_out_per_day),
        doc=base.doc,
    )


def axes_for_key(
    reqs: int, toks: int, key, *, toks_in: int = 0, toks_out: int = 0
) -> list[dict]:
    """Per-axis breakdown for the dashboard so the operator sees every cap
    that applies (not just the dominant one) — makes clear that, e.g., all
    groq keys share the SAME 14.4k req / 500k tok caps and only the fill
    differs. Returns [{name, short, used, cap, pct}] for each capped axis,
    sorted by pct desc (dominant axis first)."""
    q = quota_for_key(key)
    rows: list[dict] = []
    if q.req_per_day:
        rows.append({"name": "requests", "short": "req",
                     "used": reqs, "cap": q.req_per_day,
                     "pct": min(100, int(reqs / q.req_per_day * 100))})
    if q.tok_per_day:
        rows.append({"name": "tokens", "short": "tok",
                     "used": toks, "cap": q.tok_per_day,
                     "pct": min(100, int(toks / q.tok_per_day * 100))})
    if q.tok_in_per_day:
        rows.append({"name": "input", "short": "in",
                     "used": toks_in, "cap": q.tok_in_per_day,
                     "pct": min(100, int(toks_in / q.tok_in_per_day * 100))})
    if q.tok_out_per_day:
        rows.append({"name": "output", "short": "out",
                     "used": toks_out, "cap": q.tok_out_per_day,
                     "pct": min(100, int(toks_out / q.tok_out_per_day * 100))})
    rows.sort(key=lambda r: r["pct"], reverse=True)
    return rows


def severity_class(pct: int | None) -> str:
    """Bar fill class: blue < 70 → yellow < 90 → red ≥ 90."""
    if pct is None:
        return ""
    if pct >= 90:
        return "bad"
    if pct >= 70:
        return "warn"
    return ""
