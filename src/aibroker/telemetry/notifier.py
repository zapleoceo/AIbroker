"""Telegram alerts with state-file based throttle.

State directory: /var/lib/aibroker (container vol). One file per `key` —
file mtime = last alert sent. recover() clears file + sends ✅.
"""
from __future__ import annotations

import logging
import os
import time
from pathlib import Path

import httpx

from aibroker.config import get_settings

log = logging.getLogger(__name__)

STATE_DIR = Path(os.environ.get("ALERT_STATE_DIR", "/var/lib/aibroker"))
DEFAULT_THROTTLE_MIN = 30


def _state_file(key: str) -> Path:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in key)[:120]
    return STATE_DIR / safe


async def _post(text: str) -> None:
    s = get_settings()
    if not s.alerts_enabled:
        log.info("alerts disabled, would send: %s", text[:80])
        return
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            await c.post(
                f"https://api.telegram.org/bot{s.TELEGRAM_BOT_TOKEN}/sendMessage",
                data={
                    "chat_id": s.OWNER_TELEGRAM_ID,
                    "parse_mode": "HTML",
                    "text": text,
                },
            )
    except Exception as e:
        log.warning("telegram alert failed: %s", e)


async def alert(key: str, message: str, *, throttle_min: int = DEFAULT_THROTTLE_MIN) -> None:
    sf = _state_file(key)
    if sf.exists():
        age_min = (time.time() - sf.stat().st_mtime) / 60
        if age_min < throttle_min:
            log.info("alert throttled (%s, %.1f min ago): %s", key, age_min, message[:80])
            return
    sf.touch()
    await _post(f"⚠️ <b>aibroker</b>\n{message}")


async def recover(key: str, message: str) -> None:
    sf = _state_file(key)
    if sf.exists():
        sf.unlink(missing_ok=True)
        await _post(f"✅ <b>aibroker recovered</b>\n{message}")
