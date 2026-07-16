"""shared_state — FakeRedis unit tests. No real Redis anywhere: the client
factory is monkeypatched, and the fail-open contract (error → trip → retry
window) is what's under test."""
from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from aibroker.routing import shared_state


class FakeRedis:
    """In-memory stand-in: just the get/setex/set surface shared_state uses."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.ttls: dict[str, int] = {}

    async def get(self, key: str) -> str | None:
        return self.store.get(key)

    async def setex(self, key: str, ttl: int, value) -> None:
        self.store[key] = str(value)
        self.ttls[key] = ttl

    async def set(self, key: str, value) -> None:
        self.store[key] = str(value)


class BoomRedis:
    """Every call raises — exercises the trip/fallback path."""

    def __init__(self) -> None:
        self.calls = 0

    async def get(self, key: str) -> str | None:
        self.calls += 1
        raise ConnectionError("redis down")

    async def setex(self, key: str, ttl: int, value) -> None:
        self.calls += 1
        raise ConnectionError("redis down")


@pytest.fixture(autouse=True)
def _pristine_module_state(monkeypatch):
    monkeypatch.setattr(shared_state, "_client", None)
    monkeypatch.setattr(shared_state, "_disabled_until", 0.0)
    monkeypatch.setattr(shared_state, "_warned", False)
    monkeypatch.setenv("REDIS_URL", "")


@pytest.fixture
def fake(monkeypatch) -> FakeRedis:
    client = FakeRedis()
    monkeypatch.setattr(shared_state, "_get_client", lambda: client)
    return client


async def test_affinity_round_trip_and_ttl(fake):
    await shared_state.set_affinity(1, "deepseek", 42)
    assert await shared_state.get_affinity(1, "deepseek") == 42
    assert await shared_state.get_affinity(1, "gemini") is None
    assert await shared_state.get_affinity(2, "deepseek") is None
    assert fake.ttls["aib:aff:1:deepseek"] == int(shared_state.AFFINITY_TTL_S)


async def test_saturated_round_trip_json(fake):
    assert await shared_state.get_saturated() is None  # miss before any write
    await shared_state.set_saturated(frozenset({5, 3}), 15.0)
    assert fake.store["aib:sat"] == "[3, 5]"
    assert fake.ttls["aib:sat"] == 15
    assert await shared_state.get_saturated() == frozenset({3, 5})


async def test_saturated_empty_set_round_trips_and_ttl_clamped(fake):
    await shared_state.set_saturated(frozenset(), 0.5)
    assert fake.ttls["aib:sat"] == 1  # setex refuses ttl < 1
    assert await shared_state.get_saturated() == frozenset()


async def test_corrupt_saturated_payload_is_a_miss_not_an_outage(fake, caplog):
    fake.store["aib:sat"] = "not-json"
    with caplog.at_level(logging.WARNING):
        assert await shared_state.get_saturated() is None
    assert shared_state._disabled_until == 0.0  # no trip — DB path overwrites it


async def test_disabled_without_redis_url():
    assert shared_state._get_client() is None
    assert await shared_state.get_affinity(1, "deepseek") is None
    assert await shared_state.get_saturated() is None
    await shared_state.set_affinity(1, "deepseek", 42)  # no-op, must not raise
    await shared_state.set_saturated(frozenset({1}), 15.0)
    assert shared_state._client is None  # never constructed


async def test_error_trips_store_and_retry_window(monkeypatch, caplog):
    clock = {"now": 1000.0}
    monkeypatch.setattr(
        shared_state, "time", SimpleNamespace(monotonic=lambda: clock["now"])
    )
    monkeypatch.setenv("REDIS_URL", "redis://unreachable:6379/0")
    boom = BoomRedis()
    monkeypatch.setattr(shared_state, "_client", boom)

    with caplog.at_level(logging.WARNING):
        assert await shared_state.get_affinity(1, "deepseek") is None
    assert boom.calls == 1
    assert shared_state._disabled_until == 1000.0 + shared_state._REDIS_RETRY_S
    warnings = [r for r in caplog.records if "redis unavailable" in r.message]
    assert len(warnings) == 1

    # Inside the window: short-circuits without touching the client.
    clock["now"] += 30.0
    assert await shared_state.get_affinity(1, "deepseek") is None
    await shared_state.set_affinity(1, "deepseek", 42)
    assert boom.calls == 1

    # After the window: retries, fails again, re-arms — but warns only once.
    clock["now"] += 40.0
    with caplog.at_level(logging.WARNING):
        await shared_state.set_saturated(frozenset({1}), 15.0)
    assert boom.calls == 2
    assert shared_state._disabled_until == clock["now"] + shared_state._REDIS_RETRY_S
    warnings = [r for r in caplog.records if "redis unavailable" in r.message]
    assert len(warnings) == 1


async def test_missing_package_fails_open(monkeypatch):
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    import builtins

    real_import = builtins.__import__

    def _no_redis(name, *args, **kwargs):
        if name.startswith("redis"):
            raise ImportError("No module named 'redis'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _no_redis)
    assert shared_state._get_client() is None
    assert shared_state._disabled_until > 0.0  # tripped, not crashed
