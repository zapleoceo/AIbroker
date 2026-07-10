"""monitor — health tick loop (no-key, alive, cooldown, dead branches)."""
from __future__ import annotations

import os
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import insert, select

from aibroker.crypto import encrypt
from aibroker.db import get_session
from aibroker.db.models import ApiKeyRow
from aibroker.monitor import _cooldown_end, tick

ON_SQLITE = "sqlite" in os.environ.get("DATABASE_URL", "")


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
    fake_alert.assert_awaited_once()
    args = fake_alert.await_args.args
    assert "key:" in args[0]
    assert "401" in args[1]


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
    fake_alert.assert_awaited_once()
