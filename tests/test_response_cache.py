"""services.response_cache — exact-match cache for deterministic capabilities."""
from __future__ import annotations

import pytest

from aibroker.services import response_cache


@pytest.fixture(autouse=True)
def _clear_cache():
    response_cache.clear()
    yield
    response_cache.clear()


_MSGS = [{"role": "user", "content": "Halo apa kabar"}]
_KW = {"model": None, "max_tokens": 128, "temperature": 0.3}


def test_translate_and_prefilter_are_cacheable_chat_is_not():
    assert response_cache.is_cacheable("translate")
    assert response_cache.is_cacheable("prefilter")
    assert not response_cache.is_cacheable("chat:fast")
    assert not response_cache.is_cacheable("chat:smart")


def test_put_then_get_round_trips_for_translate():
    assert response_cache.get("translate", _MSGS, **_KW) is None      # miss
    response_cache.put("translate", _MSGS, "Hello how are you", **_KW)
    assert response_cache.get("translate", _MSGS, **_KW) == "Hello how are you"


def test_non_cacheable_capability_never_stores():
    response_cache.put("chat:fast", _MSGS, "should not persist", **_KW)
    assert response_cache.get("chat:fast", _MSGS, **_KW) is None


def test_different_params_do_not_collide():
    response_cache.put("translate", _MSGS, "A", model=None, max_tokens=128, temperature=0.3)
    # different temperature → different key → miss, not the wrong cached answer
    assert response_cache.get("translate", _MSGS, model=None,
                               max_tokens=128, temperature=0.9) is None


def test_different_text_is_a_separate_entry():
    response_cache.put("translate", _MSGS, "Hello", **_KW)
    other = [{"role": "user", "content": "Terima kasih"}]
    assert response_cache.get("translate", other, **_KW) is None


def test_empty_response_not_cached():
    response_cache.put("translate", _MSGS, "", **_KW)
    assert response_cache.get("translate", _MSGS, **_KW) is None


def test_lru_evicts_oldest_over_capacity(monkeypatch):
    monkeypatch.setattr(response_cache, "_MAX_ENTRIES", 3)
    for i in range(5):
        msgs = [{"role": "user", "content": f"phrase {i}"}]
        response_cache.put("translate", msgs, f"out {i}", **_KW)
    # oldest two evicted, newest three kept
    assert response_cache.get("translate",
                               [{"role": "user", "content": "phrase 0"}], **_KW) is None
    assert response_cache.get("translate",
                               [{"role": "user", "content": "phrase 4"}], **_KW) == "out 4"


def test_expired_entry_is_dropped(monkeypatch):
    monkeypatch.setitem(response_cache._TTL_S, "translate", -1)  # instantly stale
    response_cache.put("translate", _MSGS, "Hello", **_KW)
    assert response_cache.get("translate", _MSGS, **_KW) is None


# ─── prefilter — own short TTL (2026-07-12) ──────────────────────────────────


def test_prefilter_ttl_is_ten_minutes_translate_a_day():
    """Verdicts must roll over quickly after a prompt/threshold change; a
    translation is stable for a day."""
    assert response_cache._TTL_S["prefilter"] == 10 * 60
    assert response_cache._TTL_S["translate"] == 24 * 60 * 60


def test_prefilter_cached_within_ttl(monkeypatch):
    t = 1_000_000.0
    monkeypatch.setattr(response_cache.time, "time", lambda: t)
    response_cache.put("prefilter", _MSGS, "relevant", **_KW)
    monkeypatch.setattr(response_cache.time, "time", lambda: t + 9 * 60)
    assert response_cache.get("prefilter", _MSGS, **_KW) == "relevant"


def test_prefilter_expired_after_ttl_translate_unaffected(monkeypatch):
    """11 min later a prefilter verdict is stale, but a translate entry stored
    at the same moment still serves — the TTLs are independent."""
    t = 1_000_000.0
    monkeypatch.setattr(response_cache.time, "time", lambda: t)
    response_cache.put("prefilter", _MSGS, "relevant", **_KW)
    response_cache.put("translate", _MSGS, "Hello", **_KW)
    monkeypatch.setattr(response_cache.time, "time", lambda: t + 11 * 60)
    assert response_cache.get("prefilter", _MSGS, **_KW) is None
    assert response_cache.get("translate", _MSGS, **_KW) == "Hello"
