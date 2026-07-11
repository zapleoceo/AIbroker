"""Pure (DB-free) selector helpers — run on the SQLite quality gate, unlike
test_selector.py which is Postgres-only (FOR UPDATE SKIP LOCKED / JSONB)."""
from __future__ import annotations

from aibroker.routing.selector import _recover_set_sql


def test_recover_sql_clears_state_on_success():
    frag = _recover_set_sql("ok", None)
    assert "last_error = NULL" in frag
    assert "error_count = 0" in frag
    assert "cooldown_until = NULL" in frag


def test_recover_sql_empty_on_error_status():
    assert _recover_set_sql("error", "RateLimit") == ""


def test_recover_sql_empty_when_error_kind_set_despite_ok():
    assert _recover_set_sql("ok", "InvalidJSON") == ""
