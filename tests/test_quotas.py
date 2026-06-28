"""providers.quotas — per-provider daily quota math + severity coloring."""
from __future__ import annotations

from aibroker.providers.quotas import (
    PROVIDER_DAILY_QUOTA,
    percent_used,
    quota_for,
    severity_class,
)


def test_quota_for_returns_known_providers():
    assert quota_for("cerebras") == 14_400
    assert quota_for("gemini") == 1_500
    assert quota_for("mistral") == 86_400


def test_quota_for_paid_returns_none():
    """Paid providers have no meaningful daily quota for a progress bar."""
    for p in ("deepseek", "anthropic", "openai"):
        assert quota_for(p) is None


def test_quota_for_unknown_returns_none():
    assert quota_for("brand-new-llm") is None


def test_every_routed_provider_has_a_quota_entry():
    """Regression guard: chains.py providers MUST be present in quota table.
    Either with a number (free-tier known) or None (paid). Missing entry =
    dashboard hides the bar silently — confusing."""
    from aibroker.routing.chains import CAPABILITY_CHAINS
    routed = {p for chain in CAPABILITY_CHAINS.values() for p in chain}
    for p in routed:
        assert p in PROVIDER_DAILY_QUOTA, (
            f"{p!r} routed in chains but missing from PROVIDER_DAILY_QUOTA"
        )


def test_percent_used_basic():
    assert percent_used(0, "cerebras") == 0
    assert percent_used(7_200, "cerebras") == 50    # half of 14_400
    assert percent_used(14_400, "cerebras") == 100


def test_percent_used_caps_at_100():
    """Overage doesn't blow past 100%."""
    assert percent_used(50_000, "cerebras") == 100
    assert percent_used(10_000, "gemini") == 100    # 10k on 1.5k quota


def test_percent_used_returns_none_for_paid():
    assert percent_used(100, "anthropic") is None
    assert percent_used(100, "openai") is None
    assert percent_used(100, "unknown-provider") is None


def test_severity_class_thresholds():
    """Blue (default empty class) <70%, yellow 70-89%, red 90+."""
    assert severity_class(0) == ""
    assert severity_class(69) == ""
    assert severity_class(70) == "warn"
    assert severity_class(89) == "warn"
    assert severity_class(90) == "bad"
    assert severity_class(100) == "bad"
    assert severity_class(None) == ""
