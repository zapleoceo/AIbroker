"""Health monitor — runs as separate container. Loops forever.

Every MONITOR_INTERVAL_S seconds:
1. probes each key (cheapest call per provider)
2. on 401/403 → mark is_alive=False, alert
3. on 429 → set cooldown
4. on success → clear error_count
5. emits ✅/⚠️ Telegram messages on state changes
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update

from aibroker.config import get_settings
from aibroker.crypto import decrypt
from aibroker.db import close_engine, init_engine, get_session
from aibroker.db.models import ApiKeyRow
from aibroker.providers.health_probes import probe_all
from aibroker.telemetry import alert, recover

log = logging.getLogger(__name__)


INTERVAL_S = int(os.environ.get("MONITOR_INTERVAL_S", "600"))


async def tick() -> None:
    async with get_session() as s:
        rows = (
            await s.execute(
                select(ApiKeyRow).where(ApiKeyRow.is_active.is_(True))
            )
        ).scalars().all()

    if not rows:
        log.info("no active keys")
        return

    plain_keys = []
    for r in rows:
        try:
            plain_keys.append((r.id, r.provider, decrypt(r.token_encrypted)))
        except Exception as e:
            log.warning("decrypt %s/%s failed: %s", r.provider, r.label, e)

    results = await probe_all(plain_keys)

    alive_count, cooldown_count, dead_count = 0, 0, 0
    async with get_session() as s:
        for r in rows:
            res = results.get(r.id)
            if res is None:
                continue
            verdict, http_code, hint = res
            was_alive = r.is_alive

            if verdict == "alive":
                alive_count += 1
                await s.execute(
                    update(ApiKeyRow).where(ApiKeyRow.id == r.id).values(
                        is_alive=True, error_count=0,
                        last_alive_check_at=datetime.now(timezone.utc).replace(tzinfo=None),
                    )
                )
                if not was_alive:
                    await recover(f"key:{r.id}", f"{r.provider}/{r.label} back alive")
            elif verdict == "cooldown":
                cooldown_count += 1
                await s.execute(
                    update(ApiKeyRow).where(ApiKeyRow.id == r.id).values(
                        cooldown_until=datetime.now(timezone.utc).replace(tzinfo=None)
                                       + timedelta(minutes=5),
                        last_alive_check_at=datetime.now(timezone.utc).replace(tzinfo=None),
                    )
                )
            elif verdict == "dead":
                dead_count += 1
                await s.execute(
                    update(ApiKeyRow).where(ApiKeyRow.id == r.id).values(
                        is_alive=False, error_count=ApiKeyRow.error_count + 1,
                        last_alive_check_at=datetime.now(timezone.utc).replace(tzinfo=None),
                    )
                )
                if was_alive:
                    await alert(
                        f"key:{r.id}",
                        f"{r.provider}/{r.label} died: HTTP {http_code} ({hint})",
                    )

    log.info("monitor tick: alive=%d cooldown=%d dead=%d total=%d",
             alive_count, cooldown_count, dead_count, len(rows))


async def main() -> None:
    logging.basicConfig(level=getattr(logging, get_settings().LOG_LEVEL.upper(), logging.INFO),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    await init_engine()
    log.info("monitor started, interval=%ss", INTERVAL_S)
    try:
        while True:
            try:
                await tick()
            except Exception as e:
                log.exception("monitor tick failed: %s", e)
            await asyncio.sleep(INTERVAL_S)
    finally:
        await close_engine()


if __name__ == "__main__":
    asyncio.run(main())
