"""Per-key daily quota resolution + saturation math.

A key can be capped on four independent axes per day:
  - requests
  - total tokens (in + out)
  - input tokens
  - output tokens

For each axis the effective limit is resolved by priority:
  1. manual_*      — operator-set override (e.g. corporate Gemini 3M in / 80k out)
  2. discovered_*  — parsed from provider response headers at key creation
  3. PROVIDER_QUOTAS default — static guess from provider docs

Whichever axis is closest to its cap drives the dashboard bar and the
selector's saturation skip. Sourced from provider docs as of 2026-06-28;
verify in the provider console — defaults drift, manual override is exact.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Quota:
    """Effective per-day caps. None on an axis ⇒ that axis is uncapped."""
    req_per_day: int | None = None
    tok_per_day: int | None = None       # total in+out
    tok_in_per_day: int | None = None
    tok_out_per_day: int | None = None
    doc: str = ""


# Static provider defaults (req + total-tokens only; in/out split is rare in
# free-tier docs, so left None — manual override fills it for corp keys).
PROVIDER_QUOTAS: dict[str, Quota] = {
    "cerebras": Quota(req_per_day=14_400, tok_per_day=1_000_000,
                       doc="https://inference-docs.cerebras.ai/support/rate-limits"),
    "groq": Quota(req_per_day=14_400, tok_per_day=500_000,
                   doc="https://console.groq.com/docs/rate-limits"),
    "gemini": Quota(req_per_day=1_500,
                     doc="https://ai.google.dev/gemini-api/docs/rate-limits"),
    "mistral": Quota(req_per_day=86_400, tok_per_day=500_000,
                      doc="https://docs.mistral.ai/deployment/laplateforme/tier/"),
    "cohere": Quota(req_per_day=1_000,
                     doc="https://docs.cohere.com/v2/docs/rate-limits"),
    "openrouter": Quota(req_per_day=200,
                         doc="https://openrouter.ai/docs/api-reference/limits"),
    "voyage": Quota(tok_per_day=200_000_000,
                     doc="https://docs.voyageai.com/docs/pricing"),
    "deepseek":  Quota(doc="https://api-docs.deepseek.com/quick_start/pricing"),
    "anthropic": Quota(doc="https://docs.claude.com/en/docs/about-claude/usage-limits"),
    "openai":    Quota(doc="https://platform.openai.com/docs/guides/rate-limits"),
}


def quota_for(provider: str) -> Quota:
    """Static provider default; empty Quota for unknown providers."""
    return PROVIDER_QUOTAS.get(provider, Quota())


def quota_for_key(key) -> Quota:
    """Effective per-key Quota, resolving manual > discovered > default per axis.
    `key` is any object exposing the column attrs (ApiKeyRow in prod;
    SimpleNamespace in tests)."""
    base = quota_for(getattr(key, "provider", ""))

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


def _axis_pcts(q: Quota, reqs: int, toks: int, toks_in: int, toks_out: int) -> list[int]:
    out: list[int] = []
    if q.req_per_day:
        out.append(min(100, int(reqs / q.req_per_day * 100)))
    if q.tok_per_day:
        out.append(min(100, int(toks / q.tok_per_day * 100)))
    if q.tok_in_per_day:
        out.append(min(100, int(toks_in / q.tok_in_per_day * 100)))
    if q.tok_out_per_day:
        out.append(min(100, int(toks_out / q.tok_out_per_day * 100)))
    return out


def percent_used_for_key(
    reqs: int, toks: int, key, *, toks_in: int = 0, toks_out: int = 0
) -> int | None:
    """Highest axis usage % for this key today. None when no axis is capped."""
    pcts = _axis_pcts(quota_for_key(key), reqs, toks, toks_in, toks_out)
    return max(pcts) if pcts else None


def bar_label_for_key(
    reqs: int, toks: int, key, *, toks_in: int = 0, toks_out: int = 0
) -> str:
    """'X/Y' label for whichever axis is closest to its cap."""
    q = quota_for_key(key)
    candidates: list[tuple[float, str]] = []
    if q.req_per_day:
        candidates.append((reqs / q.req_per_day, f"{reqs}/{q.req_per_day}"))
    if q.tok_per_day:
        candidates.append((toks / q.tok_per_day,
                            f"{_humanize(toks)}/{_humanize(q.tok_per_day)} tok"))
    if q.tok_in_per_day:
        candidates.append((toks_in / q.tok_in_per_day,
                            f"{_humanize(toks_in)}/{_humanize(q.tok_in_per_day)} in"))
    if q.tok_out_per_day:
        candidates.append((toks_out / q.tok_out_per_day,
                            f"{_humanize(toks_out)}/{_humanize(q.tok_out_per_day)} out"))
    if not candidates:
        return str(reqs)
    candidates.sort(key=lambda c: c[0], reverse=True)
    return candidates[0][1]


def axes_for_key(
    reqs: int, toks: int, key, *, toks_in: int = 0, toks_out: int = 0
) -> list[dict]:
    """Per-axis breakdown for the dashboard so the operator sees every cap
    that applies (not just the dominant one) — makes clear that, e.g., all
    cerebras keys share the SAME 2400 req / 1M tok caps and only the fill
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


def _humanize(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}k"
    return str(n)
