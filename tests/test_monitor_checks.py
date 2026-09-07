"""monitor's stack checks (2026-09-07): local-service reachability, backup
freshness, queue backlog. Pure parts are tested directly; the network and DB
edges are patched at the boundary."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from aibroker import monitor
from aibroker.config import get_settings


def test_backup_is_fresh_decision():
    now = 1_000_000.0
    assert monitor.backup_is_fresh(now - 3600, now, max_age_h=36)
    assert not monitor.backup_is_fresh(now - 40 * 3600, now, max_age_h=36)
    assert not monitor.backup_is_fresh(None, now, max_age_h=36)


def test_newest_dump_mtime_picks_the_youngest(tmp_path):
    (tmp_path / "2026-09-05").mkdir()
    (tmp_path / "2026-09-07").mkdir()
    old = tmp_path / "2026-09-05" / "aibroker.dump"
    new = tmp_path / "2026-09-07" / "aibroker.dump"
    old.write_bytes(b"x")
    new.write_bytes(b"y")
    import os
    os.utime(old, (1_000, 1_000))
    os.utime(new, (2_000, 2_000))
    assert monitor._newest_dump_mtime(str(tmp_path)) == 2_000
    assert monitor._newest_dump_mtime(str(tmp_path / "missing")) is None


async def test_check_backup_freshness_alerts_when_stale(tmp_path, monkeypatch):
    monkeypatch.setenv("MONITOR_BACKUP_DIR", str(tmp_path))   # empty dir → no dump
    alert, recover = AsyncMock(), AsyncMock()
    with patch.object(monitor, "alert", alert), patch.object(monitor, "recover", recover):
        await monitor.check_backup_freshness()
    alert.assert_awaited_once()
    assert alert.await_args.args[0] == "backup:stale"
    recover.assert_not_awaited()


async def test_check_backup_freshness_disabled_when_unset(monkeypatch):
    monkeypatch.delenv("MONITOR_BACKUP_DIR", raising=False)
    alert = AsyncMock()
    with patch.object(monitor, "alert", alert):
        await monitor.check_backup_freshness()
    alert.assert_not_awaited()


async def test_check_local_services_recovers_on_200_and_alerts_on_error(monkeypatch):
    monkeypatch.setattr(get_settings(), "VISION_LOCAL_URL", "http://vision:8080")
    monkeypatch.setattr(get_settings(), "ASR_LOCAL_URL", "http://asr:8000")

    class _Client:
        def __init__(self, *a, **k): ...
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, url):
            if "vision" in url:
                return SimpleNamespace(status_code=200)
            raise httpx.ConnectError("refused")

    alert, recover = AsyncMock(), AsyncMock()
    with patch.object(monitor.httpx, "AsyncClient", _Client), \
         patch.object(monitor, "alert", alert), patch.object(monitor, "recover", recover):
        await monitor.check_local_services()
    recover.assert_awaited_once()
    assert recover.await_args.args[0] == "local:vision"
    alert.assert_awaited_once()
    assert alert.await_args.args[0] == "local:asr"
    assert "ConnectError" in alert.await_args.args[1]


async def test_check_local_services_skips_unconfigured(monkeypatch):
    monkeypatch.setattr(get_settings(), "VISION_LOCAL_URL", "")
    monkeypatch.setattr(get_settings(), "ASR_LOCAL_URL", "")
    alert, recover = AsyncMock(), AsyncMock()
    with patch.object(monitor, "alert", alert), patch.object(monitor, "recover", recover):
        await monitor.check_local_services()
    alert.assert_not_awaited()
    recover.assert_not_awaited()


@pytest.mark.parametrize("stuck,expect_alert", [(0, False), (3, True)])
async def test_check_queue_backlog(stuck, expect_alert):
    class _Session:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def execute(self, *a, **k):
            return SimpleNamespace(scalar=lambda: stuck)

    alert, recover = AsyncMock(), AsyncMock()
    with patch.object(monitor, "get_session", lambda: _Session()), \
         patch.object(monitor, "alert", alert), patch.object(monitor, "recover", recover):
        await monitor.check_queue_backlog()
    assert alert.await_count == (1 if expect_alert else 0)
    assert recover.await_count == (0 if expect_alert else 1)


def test_newest_dump_mtime_falls_back_to_child_dir_mtime_when_unreadable(tmp_path):
    """REGRESSION: the dump dirs are root:verabackup 750 and the monitor is
    unprivileged — it can list the root but not enter the date dirs, so the
    first live tick reported "none found" and paged a false backup:stale.
    A fresh date directory must count as a fresh backup."""
    import os
    d = tmp_path / "2026-09-07"
    d.mkdir()
    os.utime(d, (5_000, 5_000))
    # No readable *.dump anywhere → newest child mtime is the signal.
    assert monitor._newest_dump_mtime(str(tmp_path)) == 5_000
