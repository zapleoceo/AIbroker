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


def test_direct_database_url_falls_back_to_database_url():
    """No pooler in front (DIRECT_DATABASE_URL unset) → the LISTEN connection
    uses the same URL as everything else."""
    s = _settings()
    assert s.direct_database_url == s.DATABASE_URL


def test_direct_database_url_overrides_when_set():
    """With PgBouncer in front, DATABASE_URL points at the pooler while the
    LISTEN connection must keep a pinned real backend (NOTIFY subscriptions
    silently die under transaction pooling)."""
    s = _settings(DATABASE_URL="postgresql+asyncpg://u:p@pgbouncer:6432/db",
                  DIRECT_DATABASE_URL="postgresql+asyncpg://u:p@postgres:5432/db")
    assert s.direct_database_url.endswith("@postgres:5432/db")
