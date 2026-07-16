"""monitor — health tick loop (no-key, alive, cooldown, dead branches)."""
from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import insert, select, update

from aibroker.crypto import encrypt
from aibroker.db import get_session
from aibroker.db.models import ApiKeyRow
from aibroker.monitor import (
    _ALIVE_PROBE_EVERY_N,
    _MIN_RPD_FOR_LIVE_PROBE,
    _PAID_TAIL_CAPS,
    _check_paid_tail,
    _cooldown_end,
    _paid_key_usable,
    _should_probe,
    tick,
)

ON_SQLITE = "sqlite" in os.environ.get("DATABASE_URL", "")


def _key(provider: str = "cerebras", *, is_alive: bool = True,
         cooldown_until: datetime | None = None, tier: str = "free",
         manual_req_limit: int | None = None,
         discovered_req_limit: int | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        provider=provider, is_alive=is_alive, cooldown_until=cooldown_until,
        tier=tier, manual_req_limit=manual_req_limit,
        discovered_req_limit=discovered_req_limit,
        manual_tok_limit=None, discovered_tok_limit=None,
        manual_tok_in_limit=None, manual_tok_out_limit=None,
    )


def test_should_probe_alive_only_every_nth_sweep():
    k = _key()
    assert _should_probe(k, 0) is True                       # Nth sweep
    for sweep in range(1, _ALIVE_PROBE_EVERY_N):
        assert _should_probe(k, sweep) is False              # skipped between
    assert _should_probe(k, _ALIVE_PROBE_EVERY_N) is True    # next Nth


def test_should_probe_dead_and_cooldown_every_sweep():
    dead = _key(is_alive=False)
    cooled = _key(cooldown_until=datetime.now(UTC).replace(tzinfo=None)
                  + timedelta(minutes=5))
    for sweep in range(_ALIVE_PROBE_EVERY_N + 1):
        assert _should_probe(dead, sweep) is True
        assert _should_probe(cooled, sweep) is True


def test_should_probe_expired_cooldown_counts_as_alive():
    k = _key(cooldown_until=datetime.now(UTC).replace(tzinfo=None)
             - timedelta(minutes=5))
    assert _should_probe(k, 1) is False


def test_should_probe_micro_rpd_alive_never_dead_always():
    """sambanova (req_per_day=20 < _MIN_RPD_FOR_LIVE_PROBE): probing an alive
    key would eat its whole daily quota; a dead one is still worth one call."""
    alive = _key("sambanova")
    dead = _key("sambanova", is_alive=False)
    for sweep in range(2 * _ALIVE_PROBE_EVERY_N + 1):
        assert _should_probe(alive, sweep) is False
        assert _should_probe(dead, sweep) is True


def test_should_probe_micro_rpd_resolves_manual_over_default():
    """Effective RPD is manual > discovered > PROVIDER_QUOTAS — a manual bump
    above the threshold re-enables live probing on the Nth sweep."""
    bumped = _key("sambanova", manual_req_limit=_MIN_RPD_FOR_LIVE_PROBE)
    assert _should_probe(bumped, 0) is True
    tiny = _key("groq", discovered_req_limit=50)
    assert _should_probe(tiny, 0) is False
    uncapped = _key("deepseek")   # no req axis at all → normal cadence
    assert _should_probe(uncapped, 0) is True


def test_cooldown_end_monthly_vs_short():
    """A 'monthly quota' hint parks until next month; anything else ~5 min."""
    from aibroker.routing.cooldown import next_utc_month_start

    monthly = _cooldown_end("monthly quota")
    # ~= next UTC month start (naive), far more than a day out.
    assert abs((monthly - next_utc_month_start().replace(tzinfo=None)).total_seconds()) < 2
    assert (monthly - datetime.now(UTC).replace(tzinfo=None)).total_seconds() > 86400

    short = _cooldown_end("rate limit")
    delta = (short - datetime.now(UTC).replace(tzinfo=None)).total_seconds()
    assert 250 < delta < 350   # ~5 min


async def test_tick_with_no_keys_logs_and_returns():
    """tick() with empty key table is a clean no-op."""
    with patch("aibroker.monitor.probe_all", AsyncMock(return_value={})):
        await tick()   # no exception


# ─── paid-tail alert — a chat capability losing its last usable paid key ─────


_NOW = datetime.now(UTC).replace(tzinfo=None)


def _paid_key(provider: str = "deepseek", **kw) -> SimpleNamespace:
    base = {
        "provider": provider, "is_active": True, "is_alive": True,
        "cooldown_until": None, "scopes": ["llm:chat"],
        "daily_cost_cap_usd": None, "daily_cost_used_usd": 0.0,
        "daily_reset_at": None,
    }
    base.update(kw)
    return SimpleNamespace(**base)


def test_paid_key_usable_happy_path():
    assert _paid_key_usable(_paid_key(), {"deepseek"}, "llm:chat", _NOW) is True


def test_paid_key_usable_rejects_wrong_provider_dead_inactive():
    assert not _paid_key_usable(_paid_key("openai"), {"deepseek"}, "llm:chat", _NOW)
    assert not _paid_key_usable(_paid_key(is_alive=False), {"deepseek"}, "llm:chat", _NOW)
    assert not _paid_key_usable(_paid_key(is_active=False), {"deepseek"}, "llm:chat", _NOW)


def test_paid_key_usable_rejects_cooldown_and_scope():
    cooling = _paid_key(cooldown_until=_NOW + timedelta(minutes=5))
    assert not _paid_key_usable(cooling, {"deepseek"}, "llm:chat", _NOW)
    expired = _paid_key(cooldown_until=_NOW - timedelta(minutes=5))
    assert _paid_key_usable(expired, {"deepseek"}, "llm:chat", _NOW)
    assert not _paid_key_usable(_paid_key(scopes=["llm:edit"]),
                                {"deepseek"}, "llm:chat", _NOW)


def test_paid_key_usable_cost_cap_follows_fresh_semantics():
    """Over-cap today → unusable; a STALE daily_reset_at means the counter
    belongs to a previous day and reads 0 (FRESH_DAILY_COST_SQL rule)."""
    over = _paid_key(daily_cost_cap_usd=5.0, daily_cost_used_usd=5.0,
                     daily_reset_at=_NOW.date())
    assert not _paid_key_usable(over, {"deepseek"}, "llm:chat", _NOW)
    stale = _paid_key(daily_cost_cap_usd=5.0, daily_cost_used_usd=5.0,
                      daily_reset_at=_NOW.date() - timedelta(days=1))
    assert _paid_key_usable(stale, {"deepseek"}, "llm:chat", _NOW)
    under = _paid_key(daily_cost_cap_usd=5.0, daily_cost_used_usd=1.0,
                      daily_reset_at=_NOW.date())
    assert _paid_key_usable(under, {"deepseek"}, "llm:chat", _NOW)


async def test_check_paid_tail_alerts_when_no_paid_key():
    """Empty key table → every paid-tail capability alerts, none recovers.
    The alert must carry the once-a-DAY reminder throttle (owner's choice,
    2026-07-12) — dropping the kwarg would silently revert to the notifier's
    default throttle and spam during a long outage."""
    with patch("aibroker.monitor.alert", AsyncMock()) as fake_alert, \
         patch("aibroker.monitor.recover", AsyncMock()) as fake_recover:
        await _check_paid_tail()
    alerted = {c.args[0] for c in fake_alert.await_args_list}
    assert alerted == {f"paid_tail:{cap}" for cap in _PAID_TAIL_CAPS}
    for c in fake_alert.await_args_list:
        assert c.kwargs["throttle_min"] == 24 * 60
    fake_recover.assert_not_awaited()


async def test_check_paid_tail_recovers_when_paid_key_usable():
    """A live, scoped paid key in a chain provider → recover for both caps.
    Explicit id makes the insert SQLite-safe (no BIGSERIAL dependency)."""
    async with get_session() as s:
        await s.execute(insert(ApiKeyRow).values(
            id=90001, provider="deepseek", label="paid-tail",
            token_encrypted=encrypt("t"), tier="paid",
            is_active=True, is_alive=True, scopes=["llm:chat"],
        ))
    with patch("aibroker.monitor.alert", AsyncMock()) as fake_alert, \
         patch("aibroker.monitor.recover", AsyncMock()) as fake_recover:
        await _check_paid_tail()
    recovered = {c.args[0] for c in fake_recover.await_args_list}
    assert recovered == {f"paid_tail:{cap}" for cap in _PAID_TAIL_CAPS}
    fake_alert.assert_not_awaited()


@pytest.mark.skipif(ON_SQLITE, reason="BIGSERIAL needs Postgres")
async def test_tick_paid_tail_kill_then_revive():
    """End-to-end through tick(): all paid keys dead → paid_tail alert;
    revived → paid_tail recover."""
    async with get_session() as s:
        r = await s.execute(insert(ApiKeyRow).values(
            provider="deepseek", label="pt1",
            token_encrypted=encrypt("test-token-pt1"),
            tier="paid", is_active=True, is_alive=False,   # ← dead paid key
            scopes=["llm:chat"],
        ).returning(ApiKeyRow.id))
        kid = r.scalar_one()
    with patch("aibroker.monitor.probe_all", AsyncMock(return_value={})), \
         patch("aibroker.monitor.alert", AsyncMock()) as fake_alert, \
         patch("aibroker.monitor.recover", AsyncMock()):
        await tick()
    assert {c.args[0] for c in fake_alert.await_args_list} >= \
        {f"paid_tail:{cap}" for cap in _PAID_TAIL_CAPS}

    async with get_session() as s:
        await s.execute(update(ApiKeyRow).where(ApiKeyRow.id == kid)
                        .values(is_alive=True))
    with patch("aibroker.monitor.probe_all", AsyncMock(return_value={})), \
         patch("aibroker.monitor.alert", AsyncMock()) as fake_alert, \
         patch("aibroker.monitor.recover", AsyncMock()) as fake_recover:
        await tick()
    assert {c.args[0] for c in fake_recover.await_args_list} >= \
        {f"paid_tail:{cap}" for cap in _PAID_TAIL_CAPS}
    assert not [c for c in fake_alert.await_args_list
                if c.args[0].startswith("paid_tail:")]


async def test_tick_skip_verdict_does_not_revive_dead_key():
    """REGRESSION (2026-07-16): a provider with no configured probe returned
    'alive', so the monitor force-revived its dead keys every sweep
    (is_alive=True, last_error wiped) — a dead/revoked cloudflare key flapped
    pick→fail→dead→revive forever. The 'skip' verdict must leave the key's
    state exactly as real traffic left it. Explicit id → SQLite-safe insert;
    probe_all is REAL here (unknown provider short-circuits to skip, no HTTP)."""
    async with get_session() as s:
        await s.execute(insert(ApiKeyRow).values(
            id=90050, provider="mysteryprov", label="skip1",
            token_encrypted=encrypt("t"), tier="free",
            is_active=True, is_alive=False, error_count=4,
            last_error="auth failed", scopes=["llm:chat"],
        ))
    with patch("aibroker.monitor.alert", AsyncMock()), \
         patch("aibroker.monitor.recover", AsyncMock()) as fake_recover:
        await tick()
    async with get_session() as s:
        row = (await s.execute(
            select(ApiKeyRow).where(ApiKeyRow.id == 90050)
        )).scalar_one()
    assert row.is_alive is False               # NOT resurrected
    assert row.last_error == "auth failed"     # NOT wiped
    assert row.error_count == 4
    assert not [c for c in fake_recover.await_args_list
                if c.args[0].startswith("key:")]


async def test_tick_threads_account_id_to_probe_all():
    """The cloudflare probe needs the key's account_id for its account-scoped
    URL — tick must pass it through probe_all's key tuples."""
    async with get_session() as s:
        await s.execute(insert(ApiKeyRow).values(
            id=90051, provider="cloudflare", label="cf1",
            token_encrypted=encrypt("cf-token"), tier="free",
            is_active=True, is_alive=False,   # dead → probed every sweep
            account_id="acct-42", scopes=["llm:chat"],
        ))
    with patch("aibroker.monitor.probe_all", AsyncMock(return_value={})) as pa:
        await tick()
    entries = {(kid, prov, acc) for kid, prov, _plain, acc in pa.await_args.args[0]}
    assert (90051, "cloudflare", "acct-42") in entries


@pytest.mark.skipif(ON_SQLITE, reason="BIGSERIAL needs Postgres")
async def test_tick_marks_alive_to_alive_clears_error_count():
    async with get_session() as s:
        r = await s.execute(insert(ApiKeyRow).values(
            provider="cerebras", label="t1",
            token_encrypted=encrypt("test-token"),
            tier="free", is_active=True, is_alive=True,
            error_count=3, scopes=["llm:chat"],
        ).returning(ApiKeyRow.id))
        kid = r.scalar_one()
    fake_results = {kid: ("alive", 200, "ok")}
    with patch("aibroker.monitor.probe_all", AsyncMock(return_value=fake_results)):
        await tick()
    async with get_session() as s:
        row = (await s.execute(
            select(ApiKeyRow).where(ApiKeyRow.id == kid)
        )).scalar_one()
    assert row.is_alive is True
    assert row.error_count == 0


@pytest.mark.skipif(ON_SQLITE, reason="BIGSERIAL needs Postgres")
async def test_tick_marks_dead_alerts_and_bumps_error_count():
    async with get_session() as s:
        r = await s.execute(insert(ApiKeyRow).values(
            provider="cerebras", label="t2",
            token_encrypted=encrypt("test-token-2"),
            tier="free", is_active=True, is_alive=True,
            error_count=0, scopes=["llm:chat"],
        ).returning(ApiKeyRow.id))
        kid = r.scalar_one()
    fake_results = {kid: ("dead", 401, "auth fail")}
    with patch("aibroker.monitor.probe_all", AsyncMock(return_value=fake_results)), \
         patch("aibroker.monitor.alert", AsyncMock()) as fake_alert:
        await tick()
    # Exactly one KEY alert (paid_tail:* alerts may also fire — no paid keys here).
    key_alerts = [c for c in fake_alert.await_args_list if c.args[0].startswith("key:")]
    assert len(key_alerts) == 1
    assert "401" in key_alerts[0].args[1]


@pytest.mark.skipif(ON_SQLITE, reason="BIGSERIAL needs Postgres")
async def test_tick_marks_cooldown_sets_expiry():
    async with get_session() as s:
        r = await s.execute(insert(ApiKeyRow).values(
            provider="cerebras", label="t3",
            token_encrypted=encrypt("test-token-3"),
            tier="free", is_active=True, is_alive=True,
            error_count=0, scopes=["llm:chat"],
        ).returning(ApiKeyRow.id))
        kid = r.scalar_one()
    fake_results = {kid: ("cooldown", 429, "rate limited")}
    with patch("aibroker.monitor.probe_all", AsyncMock(return_value=fake_results)):
        await tick()
    async with get_session() as s:
        row = (await s.execute(
            select(ApiKeyRow).where(ApiKeyRow.id == kid)
        )).scalar_one()
    assert row.cooldown_until is not None
    # Cooldown ~ now + 5min
    delta = row.cooldown_until - datetime.now(UTC).replace(tzinfo=None)
    assert delta.total_seconds() > 60      # at least 1 min in future
    assert delta.total_seconds() < 7 * 60  # less than 7 min
    assert row.is_alive is True            # 429 proves the key is alive, not dead


@pytest.mark.skipif(ON_SQLITE, reason="BIGSERIAL needs Postgres")
async def test_tick_cooldown_revives_a_previously_dead_key():
    """REGRESSION: a key marked dead by an earlier tick could get stuck
    forever if every later probe hit a 429 window instead of a clean 'alive'
    — pick_and_reserve excludes is_alive=False, so only this tiny probe could
    ever prove it's alive, and 429 (proof of valid auth) didn't count. A
    rate-limit response must revive it just like a clean 'alive' would."""
    async with get_session() as s:
        r = await s.execute(insert(ApiKeyRow).values(
            provider="cohere", label="t3b",
            token_encrypted=encrypt("test-token-3b"),
            tier="free", is_active=True, is_alive=False,   # ← was dead
            error_count=2, scopes=["llm:chat"],
        ).returning(ApiKeyRow.id))
        kid = r.scalar_one()
    fake_results = {kid: ("cooldown", 429, "rate limited")}
    with patch("aibroker.monitor.probe_all", AsyncMock(return_value=fake_results)), \
         patch("aibroker.monitor.recover", AsyncMock()) as fake_recover:
        await tick()
    async with get_session() as s:
        row = (await s.execute(
            select(ApiKeyRow).where(ApiKeyRow.id == kid)
        )).scalar_one()
    assert row.is_alive is True
    fake_recover.assert_awaited_once()


@pytest.mark.skipif(ON_SQLITE, reason="BIGSERIAL needs Postgres")
async def test_tick_recover_called_when_dead_becomes_alive():
    """A previously-dead key going alive emits a recover alert."""
    async with get_session() as s:
        r = await s.execute(insert(ApiKeyRow).values(
            provider="cerebras", label="t4",
            token_encrypted=encrypt("test-token-4"),
            tier="free", is_active=True, is_alive=False,   # ← was dead
            error_count=5, scopes=["llm:chat"],
        ).returning(ApiKeyRow.id))
        kid = r.scalar_one()
    fake_results = {kid: ("alive", 200, "ok")}
    with patch("aibroker.monitor.probe_all", AsyncMock(return_value=fake_results)), \
         patch("aibroker.monitor.recover", AsyncMock()) as fake_recover:
        await tick()
    fake_recover.assert_awaited_once()


@pytest.mark.skipif(ON_SQLITE, reason="BIGSERIAL needs Postgres")
async def test_tick_skips_keys_missing_from_results():
    async with get_session() as s:
        await s.execute(insert(ApiKeyRow).values(
            provider="cerebras", label="t5",
            token_encrypted=encrypt("xyz"),
            tier="free", is_active=True, is_alive=True,
            scopes=["llm:chat"],
        ))
    with patch("aibroker.monitor.probe_all", AsyncMock(return_value={})):
        await tick()   # no crash even if results dict is empty


@pytest.mark.skipif(ON_SQLITE, reason="BIGSERIAL needs Postgres")
async def test_tick_marks_undecryptable_key_dead_and_alerts():
    """REGRESSION (2026-07-10): a key whose token can't be decrypted was logged
    and then silently dropped from `results`, so it stayed is_alive and was
    never health-checked. Now it's marked dead and alerted."""
    async with get_session() as s:
        r = await s.execute(insert(ApiKeyRow).values(
            provider="cerebras", label="broken",
            token_encrypted="not-a-valid-fernet-token",
            tier="free", is_active=True, is_alive=True,
            error_count=0, scopes=["llm:chat"],
        ).returning(ApiKeyRow.id))
        kid = r.scalar_one()
    with patch("aibroker.monitor.probe_all", AsyncMock(return_value={})), \
         patch("aibroker.monitor.alert", AsyncMock()) as fake_alert:
        await tick()
    async with get_session() as s:
        row = (await s.execute(
            select(ApiKeyRow).where(ApiKeyRow.id == kid)
        )).scalar_one()
    assert row.is_alive is False
    assert row.last_error == "token decrypt failed"
    key_alerts = [c for c in fake_alert.await_args_list if c.args[0].startswith("key:")]
    assert len(key_alerts) == 1


async def _probed_ids(sweep: int) -> set[int]:
    with patch("aibroker.monitor.probe_all", AsyncMock(return_value={})) as pa:
        await tick(sweep)
    return {kid for kid, *_ in pa.await_args.args[0]}


@pytest.mark.skipif(ON_SQLITE, reason="BIGSERIAL needs Postgres")
async def test_tick_alive_key_probed_only_on_nth_sweep():
    async with get_session() as s:
        r = await s.execute(insert(ApiKeyRow).values(
            provider="cerebras", label="t6",
            token_encrypted=encrypt("test-token-6"),
            tier="free", is_active=True, is_alive=True,
            scopes=["llm:chat"],
        ).returning(ApiKeyRow.id))
        kid = r.scalar_one()
    assert kid not in await _probed_ids(1)
    assert kid in await _probed_ids(_ALIVE_PROBE_EVERY_N)


@pytest.mark.skipif(ON_SQLITE, reason="BIGSERIAL needs Postgres")
async def test_tick_dead_key_probed_every_sweep():
    async with get_session() as s:
        r = await s.execute(insert(ApiKeyRow).values(
            provider="cerebras", label="t7",
            token_encrypted=encrypt("test-token-7"),
            tier="free", is_active=True, is_alive=False,
            scopes=["llm:chat"],
        ).returning(ApiKeyRow.id))
        kid = r.scalar_one()
    for sweep in (0, 1, 2, _ALIVE_PROBE_EVERY_N - 1):
        assert kid in await _probed_ids(sweep)


@pytest.mark.skipif(ON_SQLITE, reason="BIGSERIAL needs Postgres")
async def test_tick_micro_rpd_alive_never_probed_dead_probed():
    """sambanova-like req_per_day<_MIN_RPD_FOR_LIVE_PROBE: an alive key is
    never live-probed (probes would eat the daily quota); a dead one is."""
    async with get_session() as s:
        r = await s.execute(insert(ApiKeyRow).values(
            provider="sambanova", label="t8a",
            token_encrypted=encrypt("test-token-8a"),
            tier="free", is_active=True, is_alive=True,
            scopes=["llm:chat"],
        ).returning(ApiKeyRow.id))
        alive_kid = r.scalar_one()
        r = await s.execute(insert(ApiKeyRow).values(
            provider="sambanova", label="t8b",
            token_encrypted=encrypt("test-token-8b"),
            tier="free", is_active=True, is_alive=False,
            scopes=["llm:chat"],
        ).returning(ApiKeyRow.id))
        dead_kid = r.scalar_one()
    for sweep in (0, 1, _ALIVE_PROBE_EVERY_N):
        probed = await _probed_ids(sweep)
        assert alive_kid not in probed
        assert dead_kid in probed
