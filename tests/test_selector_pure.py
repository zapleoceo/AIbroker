"""Pure (DB-free) selector helpers — run on the SQLite quality gate, unlike
test_selector.py which is Postgres-only (FOR UPDATE SKIP LOCKED / JSONB)."""
from __future__ import annotations

import time

from aibroker.providers.registry import quotas

PROVIDER_QUOTAS = quotas()
from aibroker.routing import selector
from aibroker.routing.selector import (
    _quota_values_sql,
    _recover_set_sql,
    _saturation_order_params,
    invalidate_saturation_cache,
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


# ─── saturation-cache helpers ────────────────────────────────────────────────


def test_saturation_order_params_never_binds_empty_array():
    assert _saturation_order_params(frozenset(), None) == {
        "saturated_ids": [-1],
        "timed_out_ids": [-1],
        "aff": -1,
    }


def test_saturation_order_params_passes_ids_and_affinity():
    params = _saturation_order_params(frozenset({3, 5}), 3)
    assert sorted(params["saturated_ids"]) == [3, 5]
    assert params["aff"] == 3


def test_saturation_order_params_carries_timed_out_ids():
    params = _saturation_order_params(frozenset({3}), None, frozenset({7, 8}))
    assert params["saturated_ids"] == [3]
    assert sorted(params["timed_out_ids"]) == [7, 8]
    assert params["aff"] == -1


async def test_pick_soft_skips_free_provider_in_timeout_storm():
    """A free provider with ≥2 keys recently timed out is soft-skipped with NO
    DB call — pick returns None so run_chat fails the chain over cheaply."""
    from aibroker.routing import circuit
    from aibroker.routing.selector import pick_and_reserve

    circuit.reset()
    # Storms are bucketed per (provider, scope) since 2026-09-07 — note the
    # timeouts under the scope the pick asks for.
    circuit.note_timeout("gemini", 101, scope="llm:chat")
    circuit.note_timeout("gemini", 102, scope="llm:chat")
    # Returns None BEFORE touching the DB (no Postgres-only query runs on SQLite).
    assert await pick_and_reserve("gemini", "llm:chat", project_id=1) is None
    # A different scope's pick is NOT skipped by this storm.
    circuit.reset()
    circuit.note_timeout("gemini", 101, scope="llm:vision")
    circuit.note_timeout("gemini", 102, scope="llm:vision")
    assert circuit.providers_in_timeout_storm(2, scope="llm:chat") == frozenset()
    circuit.reset()


def test_invalidate_saturation_cache_forces_refresh():
    selector._saturated["fetched_at"] = time.monotonic()
    invalidate_saturation_cache()
    assert selector._saturated["fetched_at"] == float("-inf")


def test_quota_values_sql_covers_every_seed_provider():
    sql = _quota_values_sql()
    for provider in PROVIDER_QUOTAS:
        assert f"('{provider}'," in sql
    assert "NULL" in sql  # uncapped axes render as SQL NULL, not Python None
