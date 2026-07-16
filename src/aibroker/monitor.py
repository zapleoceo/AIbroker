"""Health monitor — runs as separate container. Loops forever.

Every MONITOR_INTERVAL_S seconds:
1. probes keys due this sweep (cheapest call per provider) — adaptive
   cadence, see _should_probe: dead/in-cooldown every sweep, alive every
   _ALIVE_PROBE_EVERY_N sweeps, micro-RPD alive keys never
2. on 401/403 → mark is_alive=False, alert
3. on 429 → set cooldown AND mark is_alive=True — a rate-limit response
   proves the credential is valid (auth passed), so a previously-dead key
   recovers here too, not just on a clean "alive" verdict. Without this, a
   key that flipped dead once could get stuck there forever: pick_and_reserve
   excludes is_alive=False keys from real traffic, so only this probe's own
   (tiny, infrequent) call can prove it's alive — and if THAT keeps landing
   on a 429 window, the key never gets a clean "alive" verdict to recover.
4. on success → clear error_count
5. emits ✅/⚠️ Telegram messages on state changes
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update

from aibroker.config import get_settings
from aibroker.crypto import decrypt
from aibroker.db import close_engine, get_session, init_engine
from aibroker.db.models import ApiKeyRow
from aibroker.providers.health_probes import probe_all
from aibroker.providers.quotas import quota_for_key
from aibroker.routing.chains import chain_for, scope_for
from aibroker.telemetry import alert, recover

log = logging.getLogger(__name__)

# Chat capabilities whose chains end in a paid "guaranteed-answer" tail (see
# routing/chains.py + test_chains.py's paid-tail invariant). Losing every
# usable paid key silently degrades them to free-only best-effort — worth an
# operator alert, not just a 503 spike later.
_PAID_TAIL_CAPS: tuple[str, ...] = ("chat:fast", "chat:smart")


INTERVAL_S = int(os.environ.get("MONITOR_INTERVAL_S", "600"))

# Probing every key every sweep was self-harm at scale: 144 sweeps/day × ~75
# keys ≈ 10.8k real completions/day spent on liveness alone. An ALIVE key's
# state rarely changes, so it's re-confirmed only every Nth sweep (once/hour at
# the default 600s interval); DEAD or in-cooldown keys are probed every sweep —
# they're the ones whose state needs re-confirmation (auto-revive depends on it).
_ALIVE_PROBE_EVERY_N = 6

# Never spend a real call confirming an ALIVE key of a micro-quota provider:
# sambanova's req_per_day=20 meant probes alone exceeded a key's entire daily
# budget; gemini free (~1500/day) lost ~10% to probing. Dead/cooldown keys of
# these providers still get probed every sweep — reviving is worth one call.
_MIN_RPD_FOR_LIVE_PROBE = 200


def _cooldown_end(hint: str) -> datetime:
    """Naive-UTC cooldown end for a monitor 'cooldown' verdict. A monthly-quota
    hint (mistral's monthly Vibe cap) parks the key until the billing cycle
    resets — anything shorter would re-cool it every probe all month; any other
    cooldown (a transient 429) is the usual short 5-min park."""
    from aibroker.routing.cooldown import next_utc_month_start
    if hint == "monthly quota":
        return next_utc_month_start().replace(tzinfo=None)
    return datetime.now(UTC).replace(tzinfo=None) + timedelta(minutes=5)


def _paid_key_usable(key, cap_providers: set[str], scope: str,
                     now: datetime) -> bool:
    """Can this paid key actually serve a capability's paid tail right now?
    Active, alive, not cooling, correctly scoped, and not over its daily cost
    cap — cost freshness follows FRESH_DAILY_COST_SQL's rule: a stale
    daily_reset_at means the counter belongs to a previous day and reads 0."""
    if key.provider not in cap_providers or not key.is_active or not key.is_alive:
        return False
    if key.cooldown_until is not None and key.cooldown_until > now:
        return False
    if scope not in (key.scopes or []):
        return False
    if key.daily_cost_cap_usd is None:
        return True
    used = key.daily_cost_used_usd if key.daily_reset_at == now.date() else 0.0
    return used < key.daily_cost_cap_usd


async def _check_paid_tail() -> None:
    """Alert (throttled) when a chat capability has NO usable paid key left —
    its guaranteed-answer tail is gone and the chain is silently free-only.
    Keyed 'paid_tail:<capability>' so notifier's recover() auto-clears the
    moment a paid key comes back."""
    now = datetime.now(UTC).replace(tzinfo=None)
    async with get_session() as s:
        paid = (await s.execute(
            select(ApiKeyRow).where(
                ApiKeyRow.is_active.is_(True), ApiKeyRow.tier == "paid",
            )
        )).scalars().all()
    for cap in _PAID_TAIL_CAPS:
        providers = set(chain_for(cap))
        scope = scope_for(cap)
        usable = sum(
            1 for k in paid if _paid_key_usable(k, providers, scope, now)
        )
        if usable:
            await recover(f"paid_tail:{cap}",
                          f"{cap}: paid tail restored ({usable} usable paid key(s))")
        else:
            # Owner's choice (2026-07-12): first alert immediately, reminders at
            # most once a DAY while the outage persists — recover() below still
            # fires the ✅ the moment a paid key returns, and clears the state
            # file so the NEXT outage alerts immediately again.
            await alert(f"paid_tail:{cap}",
                        f"{cap}: NO usable paid key — the guaranteed-answer "
                        "tail is gone; free-only until a paid key recovers",
                        throttle_min=24 * 60)


def _should_probe(key, sweep: int) -> bool:
    """Adaptive cadence: dead/in-cooldown keys every sweep (their state is the
    one in question); alive keys only every Nth sweep, and never for micro-RPD
    providers where the probe itself would eat the daily quota."""
    now = datetime.now(UTC).replace(tzinfo=None)
    in_cooldown = key.cooldown_until is not None and key.cooldown_until > now
    if not key.is_alive or in_cooldown:
        return True
    rpd = quota_for_key(key).req_per_day
    if rpd is not None and rpd < _MIN_RPD_FOR_LIVE_PROBE:
        return False
    return sweep % _ALIVE_PROBE_EVERY_N == 0


async def tick(sweep: int = 0) -> None:
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
    decrypt_failed: set[int] = set()  # pragma: no cover — needs a real key row (Postgres)
    for r in rows:
        try:
            plain = decrypt(r.token_encrypted)  # pragma: no cover — Postgres-only tick
        except Exception as e:
            log.warning("decrypt %s/%s failed: %s", r.provider, r.label, e)
            decrypt_failed.add(r.id)  # pragma: no cover — see test_tick_marks_undecryptable_key_dead_and_alerts
            continue  # pragma: no cover — Postgres-only tick
        if _should_probe(r, sweep):  # pragma: no cover — cadence logic unit-tested via _should_probe
            plain_keys.append((r.id, r.provider, plain, r.account_id))

    results = await probe_all(plain_keys)

    alive_count, cooldown_count, dead_count = 0, 0, 0
    async with get_session() as s:
        for r in rows:
            if r.id in decrypt_failed:  # pragma: no cover — Postgres-only tick
                # Can't decrypt → the key can't be used or probed. Mark it dead
                # and alert, rather than silently leaving it is_alive but never
                # health-checked (it just vanished from `results`).
                dead_count += 1
                await s.execute(
                    update(ApiKeyRow).where(ApiKeyRow.id == r.id).values(
                        is_alive=False,
                        last_alive_check_at=datetime.now(UTC).replace(tzinfo=None),
                        last_error="token decrypt failed",
                    )
                )
                if r.is_alive:
                    await alert(f"key:{r.id}",
                                f"{r.provider}/{r.label} unusable: token decrypt failed")
                continue
            res = results.get(r.id)
            if res is None:
                continue
            verdict, http_code, hint = res
            was_alive = r.is_alive

            if verdict == "skip":
                # Unprobeable (no probe configured / cloudflare key without an
                # account_id): leave the key's state exactly as real traffic
                # left it. The old default mapped this to "alive", which
                # force-revived a dead/revoked key every sweep — an eternal
                # pick→fail→dead→revive flap (2026-07-16).
                continue
            if verdict == "alive":
                alive_count += 1
                await s.execute(
                    update(ApiKeyRow).where(ApiKeyRow.id == r.id).values(
                        is_alive=True, error_count=0, last_error=None,
                        last_alive_check_at=datetime.now(UTC).replace(tzinfo=None),
                    )
                )
                if not was_alive:
                    await recover(f"key:{r.id}", f"{r.provider}/{r.label} back alive")
            elif verdict == "cooldown":
                cooldown_count += 1
                cd_until = _cooldown_end(hint)  # pragma: no cover — Postgres-only tick
                await s.execute(
                    update(ApiKeyRow).where(ApiKeyRow.id == r.id).values(
                        is_alive=True,
                        cooldown_until=cd_until,
                        last_alive_check_at=datetime.now(UTC).replace(tzinfo=None),
                        last_error=hint or None,
                    )
                )
                if not was_alive:  # pragma: no cover
                    # Exercised by the Postgres-only
                    # test_tick_cooldown_revives_a_previously_dead_key, not the
                    # SQLite diff-cover run (all of tick() needs a real DB).
                    await recover(f"key:{r.id}",
                                  f"{r.provider}/{r.label} back alive (rate-limited)")
            elif verdict == "dead":
                dead_count += 1
                await s.execute(
                    update(ApiKeyRow).where(ApiKeyRow.id == r.id).values(
                        is_alive=False, error_count=ApiKeyRow.error_count + 1,
                        last_alive_check_at=datetime.now(UTC).replace(tzinfo=None),
                        last_error=hint or None,
                    )
                )
                if was_alive:
                    await alert(
                        f"key:{r.id}",
                        f"{r.provider}/{r.label} died: HTTP {http_code} ({hint})",
                    )

    await _check_paid_tail()

    log.info("monitor tick: alive=%d cooldown=%d dead=%d total=%d",
             alive_count, cooldown_count, dead_count, len(rows))


async def main() -> None:  # pragma: no cover — process entrypoint loop
    logging.basicConfig(level=getattr(logging, get_settings().LOG_LEVEL.upper(), logging.INFO),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    await init_engine()
    log.info("monitor started, interval=%ss", INTERVAL_S)
    try:
        sweep = 0
        while True:
            try:
                await tick(sweep)
            except Exception as e:
                log.exception("monitor tick failed: %s", e)
            sweep += 1
            await asyncio.sleep(INTERVAL_S)
    finally:
        await close_engine()


if __name__ == "__main__":
    asyncio.run(main())
