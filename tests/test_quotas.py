"""providers.quotas — 4-axis quota resolution (manual > discovered > default)."""
from __future__ import annotations

from types import SimpleNamespace

from aibroker.providers.quotas import (
    PROVIDER_QUOTAS,
    quota_for,
    quota_for_key,
    severity_class,
)


def _key(**kw):
    """Build a fake key row with all quota attrs defaulting to None."""
    base = {
        "provider": "cerebras",
        "discovered_req_limit": None, "discovered_tok_limit": None,
        "manual_req_limit": None, "manual_tok_limit": None,
        "manual_tok_in_limit": None, "manual_tok_out_limit": None,
    }
    base.update(kw)
    return SimpleNamespace(**base)


# ─── Static provider defaults ────────────────────────────────────────────────


def test_quota_for_known_provider():
    q = quota_for("cerebras")
    assert q.req_per_day is None          # cerebras is token-metered only
    assert q.tok_per_day == 1_000_000
    assert q.doc.startswith("https://")
    assert quota_for("groq").req_per_day == 14_400   # a req-metered provider


def test_quota_for_paid_returns_empty():
    for p in ("deepseek", "anthropic", "openai"):
        q = quota_for(p)
        assert q.req_per_day is None and q.tok_per_day is None


def test_quota_for_mistral_has_no_daily_axes():
    """REGRESSION (2026-07-02): mistral publishes only PER-MINUTE rate-limit
    headers (x-ratelimit-limit-req-minute=50, -tokens-minute=50000) — no daily
    cap. The old req/tok_per_day=86_400/500_000 seed was an invented daily
    figure never backed by evidence; real keys sustained 1.3-1.7M tok/day
    (99.96% ok) at ~260% of the fake 500k cap, showing fully red on the
    dashboard while genuinely alive and healthy."""
    q = quota_for("mistral")
    assert q.req_per_day is None
    assert q.tok_per_day is None
    assert q.doc.startswith("https://")


def test_every_routed_provider_has_a_quota_entry():
    from aibroker.routing.chains import CAPABILITY_CHAINS
    routed = {p for chain in CAPABILITY_CHAINS.values() for p in chain}
    for p in routed:
        assert p in PROVIDER_QUOTAS, f"{p!r} routed but missing from PROVIDER_QUOTAS"


# ─── Resolution priority: manual > discovered > default ──────────────────────


def test_resolution_default_when_nothing_set():
    q = quota_for_key(_key())
    assert q.req_per_day is None          # cerebras: token-metered only
    assert q.tok_per_day == 1_000_000     # from PROVIDER_QUOTAS default


def test_resolution_discovered_overrides_default():
    # groq has both axes: discovered tok overrides its default, req untouched.
    q = quota_for_key(_key(provider="groq", discovered_tok_limit=250_000))
    assert q.tok_per_day == 250_000        # discovered wins
    assert q.req_per_day == 14_400         # req still default (untouched)


def test_resolution_manual_overrides_discovered():
    q = quota_for_key(_key(discovered_tok_limit=500_000, manual_tok_limit=2_000_000))
    assert q.tok_per_day == 2_000_000      # manual beats discovered


def test_resolution_manual_in_out_axes():
    """Corp Gemini: 3M in / 80k out — separate axes."""
    q = quota_for_key(_key(provider="gemini",
                            manual_tok_in_limit=3_000_000,
                            manual_tok_out_limit=80_000))
    assert q.tok_in_per_day == 3_000_000
    assert q.tok_out_per_day == 80_000
    assert q.req_per_day == 1_500          # gemini default still applies


# ─── Paid keys ignore free-tier seeds ────────────────────────────────────────


def test_paid_key_skips_free_tier_seed():
    """A paid key isn't bound by free-tier seeds — no quota axis at all unless
    it carries an explicit manual/discovered cap (the $/day cost cap is a
    separate column). Fixes the paid gemini key reading 212% of 1,500 free RPD."""
    q = quota_for_key(_key(provider="gemini", tier="paid"))
    assert q.req_per_day is None and q.tok_per_day is None


def test_paid_key_keeps_explicit_manual_limit():
    q = quota_for_key(_key(provider="gemini", tier="paid", manual_req_limit=500_000))
    assert q.req_per_day == 500_000


def test_free_key_still_gets_seed():
    q = quota_for_key(_key(provider="gemini", tier="free"))
    assert q.req_per_day == 1_500


# ─── severity ────────────────────────────────────────────────────────────────


def test_severity_class_thresholds():
    assert severity_class(0) == ""
    assert severity_class(69) == ""
    assert severity_class(70) == "warn"
    assert severity_class(89) == "warn"
    assert severity_class(90) == "bad"
    assert severity_class(None) == ""


def test_doc_url_present_for_every_quota():
    for p, q in PROVIDER_QUOTAS.items():
        assert q.doc.startswith("https://"), f"{p} missing doc URL"
