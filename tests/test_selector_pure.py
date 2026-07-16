"""Pure (DB-free) selector helpers — run on the SQLite quality gate, unlike
test_selector.py which is Postgres-only (FOR UPDATE SKIP LOCKED / JSONB)."""
from __future__ import annotations

import time

from aibroker.providers.quotas import PROVIDER_QUOTAS
from aibroker.routing import selector
from aibroker.routing.selector import (
    _affinity_for,
    _quota_values_sql,
    _recover_set_sql,
    _saturation_order_params,
    invalidate_saturation_cache,
    note_affinity,
)


def test_recover_sql_clears_state_on_success():
    frag = _recover_set_sql("ok", None)
    assert "last_error = NULL" in frag
    assert "error_count = 0" in frag
    assert "cooldown_until = NULL" in frag


def test_recover_sql_empty_on_error_status():
    assert _recover_set_sql("error", "RateLimit") == ""


def test_recover_sql_empty_when_error_kind_set_despite_ok():
    assert _recover_set_sql("ok", "InvalidJSON") == ""


# ─── cache-affinity map (note_affinity / _affinity_for) ──────────────────────


def test_affinity_round_trip():
    selector._affinity.clear()
    note_affinity(1, "deepseek", 42)
    assert _affinity_for(1, "deepseek") == 42
    assert _affinity_for(1, "gemini") is None       # other provider — no pin
    assert _affinity_for(2, "deepseek") is None     # other project — no pin
    assert _affinity_for(None, "deepseek") is None  # callers without a project


def test_affinity_last_success_wins():
    selector._affinity.clear()
    note_affinity(1, "deepseek", 42)
    note_affinity(1, "deepseek", 43)
    assert _affinity_for(1, "deepseek") == 43


def test_affinity_expires_after_ttl(monkeypatch):
    selector._affinity.clear()
    note_affinity(7, "gemini", 9)
    monkeypatch.setattr(selector, "_AFFINITY_TTL_S", -1.0)
    assert _affinity_for(7, "gemini") is None
    assert (7, "gemini") not in selector._affinity  # expired entries are dropped


# ─── shared (Redis-backed) wrappers — in-process fallback path ────────────────


async def test_shared_wrappers_fall_back_in_process(monkeypatch):
    monkeypatch.setenv("REDIS_URL", "")  # shared store disabled
    selector._affinity.clear()
    await selector.note_affinity_shared(1, "deepseek", 42)
    assert await selector._affinity_for_shared(1, "deepseek") == 42
    assert await selector._affinity_for_shared(None, "deepseek") is None
    assert await selector._affinity_for_shared(2, "deepseek") is None


async def test_shared_affinity_wins_over_in_process(monkeypatch):
    selector._affinity.clear()
    note_affinity(1, "deepseek", 42)

    async def fake_get(project_id: int, provider: str) -> int | None:
        return 99 if (project_id, provider) == (1, "deepseek") else None

    monkeypatch.setattr(selector.shared_state, "get_affinity", fake_get)
    assert await selector._affinity_for_shared(1, "deepseek") == 99
    # Shared miss (other project) still falls through to the local dict.
    note_affinity(2, "deepseek", 7)
    assert await selector._affinity_for_shared(2, "deepseek") == 7


# ─── saturation-cache helpers ────────────────────────────────────────────────


def test_saturation_order_params_never_binds_empty_array():
    assert _saturation_order_params(frozenset(), None) == {
        "saturated_ids": [-1],
        "aff": -1,
    }


def test_saturation_order_params_passes_ids_and_affinity():
    params = _saturation_order_params(frozenset({3, 5}), 3)
    assert sorted(params["saturated_ids"]) == [3, 5]
    assert params["aff"] == 3


def test_invalidate_saturation_cache_forces_refresh():
    selector._saturated["fetched_at"] = time.monotonic()
    invalidate_saturation_cache()
    assert selector._saturated["fetched_at"] == float("-inf")


def test_quota_values_sql_covers_every_seed_provider():
    sql = _quota_values_sql()
    for provider in PROVIDER_QUOTAS:
        assert f"('{provider}'," in sql
    assert "NULL" in sql  # uncapped axes render as SQL NULL, not Python None
