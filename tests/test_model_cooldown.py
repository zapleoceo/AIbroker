"""Gemini per-day quota → per-(key, model) cooldown (2026-10-03 review)."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from aibroker.db import get_session
from aibroker.db.models import ApiKeyRow
from aibroker.routing.cooldown import cooldown_until, is_model_scoped_quota
from aibroker.routing.model_cooldown import cooled_models, mark_model_cooldown

_GEMINI_DAILY = (
    'litellm.RateLimitError: GeminiException - {"error": {"code": 429, "message": '
    '"You exceeded your current quota", "status": "RESOURCE_EXHAUSTED", "details": ['
    '{"@type": "type.googleapis.com/google.rpc.QuotaFailure", "violations": [{"quotaMetric": '
    '"generativelanguage.googleapis.com/generate_content_free_tier_requests", "quotaId": '
    '"GenerateRequestsPerDayPerProjectPerModel-FreeTier", "quotaDimensions": '
    '{"model": "gemini-3.5-flash"}}]}, {"@type": "type.googleapis.com/google.rpc.RetryInfo", '
    '"retryDelay": "39791s"}]}}'
)
_GEMINI_RPM = ('litellm.RateLimitError: GeminiException - {"error": {"code": 429, "details": ['
               '{"quotaId": "GenerateRequestsPerMinutePerProjectPerModel-FreeTier"}, '
               '{"retryDelay": "24s"}]}}')


async def _add_key(key_id: int) -> None:
    async with get_session() as s:
        s.add(ApiKeyRow(id=key_id, provider="gemini", label=f"k{key_id}", tier="free",
                        scopes=["llm:chat"], token_encrypted="x"))


def test_gemini_per_day_quota_is_model_scoped_but_rpm_is_not():
    assert is_model_scoped_quota("gemini", _GEMINI_DAILY)
    assert not is_model_scoped_quota("gemini", _GEMINI_RPM)
    assert not is_model_scoped_quota("groq", _GEMINI_DAILY)  # provider-scoped


async def test_cooldown_until_parks_gemini_daily_quota_to_the_hint():
    until = await cooldown_until(1, "gemini", _GEMINI_DAILY)
    secs = (until - datetime.now(UTC)).total_seconds()
    assert 39791 - 5 <= secs <= 39791 + 60


async def test_mark_model_cooldown_only_extends():
    long_until = datetime.now(UTC) + timedelta(hours=10)
    short_until = datetime.now(UTC) + timedelta(minutes=1)
    await _add_key(7)
    await mark_model_cooldown(7, "gemini/m1", long_until, "daily")
    await mark_model_cooldown(7, "gemini/m1", short_until, "rpm")  # must not shorten
    await mark_model_cooldown(7, "gemini/m2", short_until, "rpm")
    assert await cooled_models(7) == {"gemini/m1", "gemini/m2"}
    async with get_session() as s:
        row = (await s.execute(text(
            "SELECT cooldown_until FROM api_key_model_cooldowns "
            "WHERE api_key_id = 7 AND model = 'gemini/m1'"))).scalar_one()
    if isinstance(row, str):
        row = datetime.fromisoformat(row)
    assert row > datetime.now(UTC).replace(tzinfo=None) + timedelta(hours=9)


async def test_cooled_models_ignores_expired_rows():
    await _add_key(8)
    await mark_model_cooldown(8, "gemini/old", datetime.now(UTC) - timedelta(minutes=1))
    assert await cooled_models(8) == set()


async def test_penalize_gemini_daily_quota_cools_the_model_not_the_key():
    import aibroker.services.attempt as att

    await _add_key(9)
    key = SimpleNamespace(id=9, provider="gemini", label="k")
    kind = await att._penalize(key, RuntimeError(_GEMINI_DAILY),
                               capability="chat:fast", model="gemini/gemini-3.5-flash")
    assert kind == "rate_limit"
    assert await cooled_models(9) == {"gemini/gemini-3.5-flash"}
    async with get_session() as s:
        key_cd = (await s.execute(text(
            "SELECT cooldown_until FROM api_keys WHERE id = 9"))).scalar_one()
    assert key_cd is None  # the key itself stays pickable for its other models


async def test_penalize_gemini_rpm_429_still_cools_the_whole_key():
    import aibroker.services.attempt as att

    await _add_key(10)
    key = SimpleNamespace(id=10, provider="gemini", label="k")
    await att._penalize(key, RuntimeError(_GEMINI_RPM),
                        capability="chat:fast", model="gemini/gemini-3.5-flash")
    assert await cooled_models(10) == set()
    async with get_session() as s:
        key_cd = (await s.execute(text(
            "SELECT cooldown_until FROM api_keys WHERE id = 10"))).scalar_one()
    assert key_cd is not None


async def test_rotate_model_skips_cooled_models_and_fails_open(monkeypatch):
    from aibroker.services import llm_service as svc

    pool = ["a", "b", "c"]
    monkeypatch.setattr(svc, "cooled_models", _async_return({"a", "b"}))
    assert await svc._rotate_model(pool, 1, 0) == "c"
    monkeypatch.setattr(svc, "cooled_models", _async_return({"a", "b", "c"}))
    assert await svc._rotate_model(pool, 1, 1) == "b"   # all cooled → original pick

    async def boom(_):
        raise RuntimeError("db down")
    monkeypatch.setattr(svc, "cooled_models", boom)
    assert await svc._rotate_model(pool, 1, 2) == "c"   # fail open
    assert await svc._rotate_model(["only"], 1, 0) == "only"  # single model: no lookup


def _async_return(value):
    async def _f(_):
        return value
    return _f


@pytest.mark.parametrize("pinned,primary,expect", [
    ("gemini/x", "gemini/p", ["gemini/x"]),
    (None, None, []),
])
def test_model_pool_pin_and_empty(pinned, primary, expect):
    from aibroker.services import llm_service as svc
    assert svc._model_pool("gemini", "chat:fast", primary, pinned) == expect
