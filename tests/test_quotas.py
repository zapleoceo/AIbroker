"""providers.quotas — per-provider daily quotas (request- or token-metered)."""
from __future__ import annotations

from aibroker.providers.quotas import (
    PROVIDER_QUOTAS,
    bar_label,
    percent_used,
    quota_for,
    severity_class,
)


def test_quota_for_known_provider():
    q = quota_for("cerebras")
    assert q.req_per_day == 14_400
    # 2026-06-28: verified against user's '90% free tokens' alert
    assert q.tok_per_day == 1_000_000
    assert q.doc.startswith("https://")


def test_quota_for_paid_returns_empty():
    """Paid providers have empty Quota — no bar drawn."""
    for p in ("deepseek", "anthropic", "openai"):
        q = quota_for(p)
        assert q.req_per_day is None
        assert q.tok_per_day is None


def test_quota_for_unknown_returns_empty():
    assert quota_for("brand-new-llm").req_per_day is None


def test_every_routed_provider_has_a_quota_entry():
    """Drift guard: every provider routed in chains MUST have a Quota."""
    from aibroker.routing.chains import CAPABILITY_CHAINS
    routed = {p for chain in CAPABILITY_CHAINS.values() for p in chain}
    for p in routed:
        assert p in PROVIDER_QUOTAS, (
            f"{p!r} routed in chains but missing from PROVIDER_QUOTAS"
        )


def test_percent_used_returns_max_axis():
    """When both axes apply, the bar shows whichever is closer to its cap."""
    # cerebras: 14400 req/day, 1M tok/day.
    # 0 req, 500k tok = 50% on token axis → bar shows 50.
    assert percent_used(0, 500_000, "cerebras") == 50
    # 7200 req (50% req), 100k tok (10% tok) → bar shows 50.
    assert percent_used(7_200, 100_000, "cerebras") == 50
    # both maxed
    assert percent_used(14_400, 1_000_000, "cerebras") == 100


def test_percent_used_caps_at_100():
    """Over-quota doesn't blow past 100 — clamp guards UI width."""
    assert percent_used(0, 5_000_000, "cerebras") == 100   # 5× tokens cap
    assert percent_used(100_000, 0, "cerebras") == 100


def test_percent_used_returns_none_for_paid():
    assert percent_used(100, 1_000_000, "anthropic") is None
    assert percent_used(100, 1_000_000, "openai") is None
    assert percent_used(100, 1_000_000, "unknown-provider") is None


def test_gemini_is_request_metered_only():
    """Gemini has per-day request limit but no per-day token cap."""
    # 750 req on 1500 RPD → 50%; token axis ignored
    assert percent_used(750, 99_999_999, "gemini") == 50


def test_bar_label_picks_dominant_axis():
    """When token usage is higher %, show 'tok' label; otherwise show req."""
    # cerebras: 500k tok (50%) vs 100 req (0%) → show token label
    label = bar_label(100, 500_000, "cerebras")
    assert "tok" in label
    assert "500k" in label
    # cerebras: 7200 req (50%) vs 0 tok (0%) → show req label
    label = bar_label(7_200, 0, "cerebras")
    assert label == "7200/14400"
    # gemini: token-axis disabled, always show req
    label = bar_label(100, 999_999, "gemini")
    assert "100/1500" == label


def test_severity_class_thresholds():
    assert severity_class(0) == ""
    assert severity_class(69) == ""
    assert severity_class(70) == "warn"
    assert severity_class(89) == "warn"
    assert severity_class(90) == "bad"
    assert severity_class(100) == "bad"
    assert severity_class(None) == ""


def test_quota_for_key_uses_discovered_when_set():
    """Per-key discovered limits override PROVIDER_QUOTAS defaults."""
    from types import SimpleNamespace
    from aibroker.providers.quotas import quota_for_key
    # cerebras default: req=14400, tok=1M. Key was probed → real cap is half.
    k = SimpleNamespace(provider="cerebras",
                         discovered_req_limit=7200,
                         discovered_tok_limit=500_000)
    q = quota_for_key(k)
    assert q.req_per_day == 7200
    assert q.tok_per_day == 500_000


def test_quota_for_key_falls_back_to_defaults():
    """When discovery hasn't happened yet, defaults still kick in."""
    from types import SimpleNamespace
    from aibroker.providers.quotas import quota_for_key
    k = SimpleNamespace(provider="cerebras",
                         discovered_req_limit=None,
                         discovered_tok_limit=None)
    q = quota_for_key(k)
    assert q.req_per_day == 14_400
    assert q.tok_per_day == 1_000_000


def test_quota_for_key_one_sided_discovery():
    """If only one axis was discovered, the other still uses the default."""
    from types import SimpleNamespace
    from aibroker.providers.quotas import quota_for_key
    # Only token quota came back in headers; req still uses default
    k = SimpleNamespace(provider="cerebras",
                         discovered_req_limit=None,
                         discovered_tok_limit=2_000_000)
    q = quota_for_key(k)
    assert q.req_per_day == 14_400      # default
    assert q.tok_per_day == 2_000_000   # discovered


def test_percent_used_for_key_uses_discovered():
    from types import SimpleNamespace
    from aibroker.providers.quotas import percent_used_for_key
    k = SimpleNamespace(provider="cerebras",
                         discovered_req_limit=1000,   # tiny, easier to hit
                         discovered_tok_limit=None)
    # 500 req on discovered 1000 = 50%; default cerebras would give 3%
    assert percent_used_for_key(500, 0, k) == 50


def test_doc_url_present_for_every_quota():
    """Each provider entry must link to its rate-limit docs for verification."""
    for p, q in PROVIDER_QUOTAS.items():
        assert q.doc.startswith("https://"), f"{p} missing doc URL"
