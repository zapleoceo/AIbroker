"""telemetry — audit log + alert throttle."""
from __future__ import annotations

import time
from unittest.mock import AsyncMock, patch

import pytest

from aibroker.telemetry import audit
from aibroker.telemetry.notifier import _state_file, alert, recover


@pytest.mark.skipif(
    True,
    reason="audit_log.id uses BIGSERIAL → needs Postgres; SQLite BIGINT doesn't autoincrement",
)
async def test_audit_writes_row():
    """audit() inserts a row in audit_log table."""
    await audit(actor="dashboard", action="key.disable", target="id=42",
                 metadata={"label": "test"}, ip="1.2.3.4")
    from sqlalchemy import text
    from aibroker.db import get_session
    async with get_session() as s:
        rows = (await s.execute(
            text("SELECT actor, action, target FROM audit_log")
        )).all()
    assert any(r.actor == "dashboard" and r.action == "key.disable" for r in rows)


async def test_audit_silently_ignores_failures(monkeypatch):
    """audit failures must never break the calling op."""
    with patch("aibroker.telemetry.audit.get_session", side_effect=RuntimeError):
        # No exception escapes
        await audit(actor="x", action="y")


def test_state_file_path_is_safe():
    """state_file sanitizes key strings — no path traversal."""
    p = _state_file("../../../etc/passwd")
    # Result should not break out of STATE_DIR
    assert "etc" not in str(p.parent)
    assert "passwd" in p.name or "_" in p.name


def test_state_file_keeps_alphanumeric():
    p = _state_file("key_abc-123")
    assert "key_abc-123" in p.name


async def test_alert_skips_when_alerts_disabled(monkeypatch, tmp_path):
    """No bot token → no Telegram POST attempt."""
    monkeypatch.setattr("aibroker.telemetry.notifier.STATE_DIR", tmp_path)
    fake_settings = type("S", (), {"alerts_enabled": False, "TELEGRAM_BOT_TOKEN": "",
                                     "OWNER_TELEGRAM_ID": 0})()
    with patch("aibroker.telemetry.notifier.get_settings", return_value=fake_settings):
        # Should not raise even though no HTTP client is mocked
        await alert("test-key", "test message")


async def test_alert_throttles(monkeypatch, tmp_path):
    """Second alert within throttle window is suppressed."""
    monkeypatch.setattr("aibroker.telemetry.notifier.STATE_DIR", tmp_path)
    fake_settings = type("S", (), {"alerts_enabled": False, "TELEGRAM_BOT_TOKEN": "",
                                     "OWNER_TELEGRAM_ID": 0})()
    with patch("aibroker.telemetry.notifier.get_settings", return_value=fake_settings):
        await alert("xyz", "first")
        sf = _state_file("xyz")
        # Move mtime backwards by 5 min — still inside 30-min throttle
        fresh_mtime = time.time() - 300
        import os
        os.utime(sf, (fresh_mtime, fresh_mtime))
        await alert("xyz", "second")
        assert (time.time() - sf.stat().st_mtime) > 200, "state file should NOT be touched"


async def test_recover_clears_state(monkeypatch, tmp_path):
    monkeypatch.setattr("aibroker.telemetry.notifier.STATE_DIR", tmp_path)
    fake_settings = type("S", (), {"alerts_enabled": False, "TELEGRAM_BOT_TOKEN": "",
                                     "OWNER_TELEGRAM_ID": 0})()
    with patch("aibroker.telemetry.notifier.get_settings", return_value=fake_settings):
        sf = _state_file("xyz")
        sf.touch()
        await recover("xyz", "back to normal")
        assert not sf.exists()


async def test_recover_noop_when_no_state(monkeypatch, tmp_path):
    """recover when no prior alert → no message sent."""
    monkeypatch.setattr("aibroker.telemetry.notifier.STATE_DIR", tmp_path)
    fake_settings = type("S", (), {"alerts_enabled": False, "TELEGRAM_BOT_TOKEN": "",
                                     "OWNER_TELEGRAM_ID": 0})()
    with patch("aibroker.telemetry.notifier.get_settings", return_value=fake_settings):
        # No prior state file — recover should be a no-op
        await recover("nonexistent-key", "fake recovery")
