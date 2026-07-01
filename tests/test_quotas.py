"""providers.quotas — 4-axis quota resolution (manual > discovered > default)."""
from __future__ import annotations

from types import SimpleNamespace

from aibroker.providers.quotas import (
    PROVIDER_QUOTAS,
    bar_label_for_key,
    percent_used_for_key,
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


# ─── percent_used_for_key — max across 4 axes ────────────────────────────────


def test_percent_used_total_token_axis():
    k = _key()  # cerebras: 1M tok cap
    assert percent_used_for_key(0, 500_000, k) == 50
    assert percent_used_for_key(0, 1_000_000, k) == 100


def test_percent_used_output_axis_saturates_first():
    """Corp Gemini: 1.5M in (50% of 3M) but 76k out (95% of 80k) → 95%."""
    k = _key(provider="gemini",
             manual_tok_in_limit=3_000_000, manual_tok_out_limit=80_000)
    pct = percent_used_for_key(10, 1_576_000, k,
                                toks_in=1_500_000, toks_out=76_000)
    assert pct == 95   # output axis dominates


def test_percent_used_caps_at_100():
    k = _key()
    assert percent_used_for_key(0, 5_000_000, k) == 100


def test_percent_used_none_for_paid():
    assert percent_used_for_key(100, 1_000_000, _key(provider="anthropic")) is None


# ─── bar_label_for_key — shows the dominant axis ─────────────────────────────


def test_bar_label_picks_output_axis_when_dominant():
    k = _key(provider="gemini",
             manual_tok_in_limit=3_000_000, manual_tok_out_limit=80_000)
    label = bar_label_for_key(10, 1_576_000, k,
                               toks_in=1_500_000, toks_out=76_000)
    assert "out" in label
    assert "76k" in label and "80k" in label


def test_bar_label_picks_token_axis_for_cerebras():
    k = _key()
    label = bar_label_for_key(100, 500_000, k)
    assert "tok" in label and "500k" in label


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
