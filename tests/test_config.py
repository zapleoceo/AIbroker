"""Settings validation — SESSION_SECRET strength without breaking secret-less
services (the monitor container sets no SESSION_SECRET)."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from aibroker.config import Settings

_BASE = {
    "TOKEN_SECRET": "x" * 20, "ADMIN_KEY": "x" * 20, "INTERNAL_SECRET": "x" * 20,
    "DATABASE_URL": "sqlite+aiosqlite:///:memory:",
}


def _settings(**over) -> Settings:
    return Settings(_env_file=None, **{**_BASE, **over})


def test_session_secret_empty_is_allowed():
    """REGRESSION (2026-07-10): a plain Field(min_length=32) validated even the
    empty default under pydantic-settings, so the monitor container — which sets
    no SESSION_SECRET — crash-looped on startup, pinning a CPU core. Empty must
    load fine (the dashboard fails closed at runtime if it needs a cookie)."""
    assert _settings(SESSION_SECRET="").SESSION_SECRET == ""


def test_session_secret_short_nonempty_is_rejected():
    """A weak (non-empty) secret makes admin cookies forgeable — still rejected."""
    with pytest.raises(ValidationError):
        _settings(SESSION_SECRET="too-short")


def test_session_secret_strong_is_accepted():
    assert _settings(SESSION_SECRET="y" * 40).SESSION_SECRET == "y" * 40
