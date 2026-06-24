"""Cost guard — three-tier daily cap enforcement."""
from __future__ import annotations

import pytest

from aibroker.db.models import ApiKeyRow, ProjectRow
from aibroker.routing.cost_guard import CostGuardError, check_caps, invalidate_global_cache


def _key(*, tier="paid", used=0.0, cap=None) -> ApiKeyRow:
    return ApiKeyRow(
        id=1, provider="x", label="x", tier=tier, scopes=["llm:chat"],
        token_encrypted="x", is_active=True, is_alive=True,
        daily_limit=999999, daily_used=0,
        daily_cost_used_usd=used, daily_cost_cap_usd=cap,
        monthly_cost_used_usd=0, total_cost_usd=0,
        error_count=0, notes="",
    )


def _project(*, cap=None) -> ProjectRow:
    return ProjectRow(
        id=1, name="x", project_key_hash="x", project_key_prefix="x",
        allowed_scopes=["llm:chat"], daily_cost_cap_usd=cap,
        is_active=True, notes="",
    )


async def test_free_key_zero_cost_passes(monkeypatch):
    """Free keys with $0 cost bypass all checks."""
    await check_caps(api_key=_key(tier="free"), project=_project(), estimated_cost=0.0)


async def test_paid_under_cap_passes():
    await check_caps(api_key=_key(used=0.5, cap=2.0), project=_project(), estimated_cost=0.1)


async def test_paid_at_cap_blocks():
    with pytest.raises(CostGuardError) as exc:
        await check_caps(api_key=_key(used=1.99, cap=2.0), project=_project(),
                          estimated_cost=0.05)
    assert exc.value.kind == "api_key"
    assert exc.value.limit == 2.0
    assert exc.value.used == 1.99
    assert exc.value.attempted == 0.05


async def test_key_with_no_cap_passes():
    """daily_cost_cap_usd=None → never blocks at key level."""
    await check_caps(api_key=_key(used=1_000_000, cap=None),
                      project=_project(), estimated_cost=10.0)


async def test_invalidate_global_cache_resets_ttl():
    """Force-bust the 30s memo so a fresh SUM query happens."""
    invalidate_global_cache()
    # Just verify it's callable & idempotent
    invalidate_global_cache()
