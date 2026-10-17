"""An abandoned Claude Code OAuth refresh lock is removed before a run (never credentials)."""
from __future__ import annotations

import os
import time

from aibroker.agent.real_cli import (
    OAUTH_LOCK,
    OAUTH_LOCK_STALE_S,
    ClaudeCLI,
    clear_stale_oauth_lock,
)


def _lock(tmp_path, age_s: float, files=()):
    lock = tmp_path / OAUTH_LOCK
    lock.mkdir()
    for name in files:
        (lock / name).write_text("x")
    old = time.time() - age_s
    os.utime(lock, (old, old))
    return lock


def test_an_old_empty_lock_is_removed_and_credentials_are_untouched(tmp_path):
    creds = tmp_path / ".credentials.json"
    creds.write_text("{}")
    lock = _lock(tmp_path, OAUTH_LOCK_STALE_S + 5)
    assert clear_stale_oauth_lock(str(tmp_path)) is True
    assert not lock.exists() and creds.read_text() == "{}"


def test_a_fresh_or_non_empty_lock_or_no_dir_is_kept(tmp_path):
    fresh = _lock(tmp_path, 5)
    assert clear_stale_oauth_lock(str(tmp_path)) is False and fresh.exists()
    fresh.rmdir()
    busy = _lock(tmp_path, OAUTH_LOCK_STALE_S + 5, files=("owner",))
    assert clear_stale_oauth_lock(str(tmp_path)) is False and busy.exists()
    assert clear_stale_oauth_lock(None) is False
    assert clear_stale_oauth_lock(str(tmp_path / "missing")) is False


def test_claude_preflight_clears_the_abandoned_lock(tmp_path, monkeypatch):
    monkeypatch.setattr("aibroker.agent.real_cli.settings_problem", lambda *_a: None)
    lock = _lock(tmp_path, OAUTH_LOCK_STALE_S + 5)
    adapter = ClaudeCLI(env={"CLAUDE_CONFIG_DIR": str(tmp_path)})
    from types import SimpleNamespace
    assert adapter.preflight(SimpleNamespace(workspace=str(tmp_path))) is None
    assert not lock.exists()
