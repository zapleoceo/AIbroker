"""routing.cooldown — adaptive cooldown table + exponential backoff math."""
from __future__ import annotations

from aibroker.routing.cooldown import (
    COOLDOWN_BASE_S,
    DEFAULT_COOLDOWN_S,
    MAX_COOLDOWN_S,
    cooldown_seconds,
)


def test_first_cooldown_is_provider_base():
    for prov, base in COOLDOWN_BASE_S.items():
        assert cooldown_seconds(prov, 0) == base, f"{prov} should start at base"


def test_unknown_provider_uses_default():
    assert cooldown_seconds("brand-new-llm", 0) == DEFAULT_COOLDOWN_S


def test_exponential_backoff_doubles():
    """Each consecutive cooldown within the window doubles the wait."""
    # gemini base = 60s
    assert cooldown_seconds("gemini", 0) == 60
    assert cooldown_seconds("gemini", 1) == 120
    assert cooldown_seconds("gemini", 2) == 240
    assert cooldown_seconds("gemini", 3) == 480
    # mistral base = 10s, doubles too
    assert cooldown_seconds("mistral", 0) == 10
    assert cooldown_seconds("mistral", 1) == 20
    assert cooldown_seconds("mistral", 2) == 40


def test_backoff_caps_at_max():
    """Never wait longer than MAX_COOLDOWN_S no matter how many failures."""
    assert cooldown_seconds("gemini", 20) == MAX_COOLDOWN_S
    assert cooldown_seconds("openrouter", 20) == MAX_COOLDOWN_S
    assert cooldown_seconds("anything", 50) == MAX_COOLDOWN_S


def test_gemini_recovers_in_one_minute_first_try():
    """Regression: Gemini's RPM window is 60s — base must match.

    Old flat 5min wasted 4 minutes per Gemini cooldown.
    """
    assert COOLDOWN_BASE_S["gemini"] == 60


def test_openrouter_stays_conservative():
    """Regression: :free pool overload can last minutes — don't re-spam."""
    assert COOLDOWN_BASE_S["openrouter"] >= 120


def test_paid_providers_get_long_cooldown():
    """Don't burn paid credits by retrying every few seconds on 429."""
    for p in ("anthropic", "openai"):
        assert COOLDOWN_BASE_S[p] >= 60, f"{p} base too short"
