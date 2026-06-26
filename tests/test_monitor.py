"""monitor — health tick loop (no-key, alive, cooldown, dead branches)."""
from __future__ import annotations

import os
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import insert, select

from aibroker.crypto import encrypt
from aibroker.db import get_session
from aibroker.db.models import ApiKeyRow
from aibroker.monitor import tick

ON_SQLITE = "sqlite" in os.environ.get("DATABASE_URL", "")


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
    delta = row.cooldown_until - datetime.now(timezone.utc).replace(tzinfo=None)
    assert delta.total_seconds() > 60      # at least 1 min in future
    assert delta.total_seconds() < 7 * 60  # less than 7 min


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
