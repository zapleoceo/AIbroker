"""Activation blocker: ordinary in-flight estimates are invisible across keys."""
import pytest
from sqlalchemy import func, select

from aibroker.config import get_settings
from aibroker.db.engine import get_session
from aibroker.db.models import ApiKeyRow, ProjectRow, UsageLogRow
from aibroker.providers.gemini_video import VideoRejected
from aibroker.routing.cost_guard import invalidate_global_cache, reserve_cost
from tests.test_vivix_billing import binding as binding
from tests.test_vivix_billing import prepared
from tests.test_vivix_service import ledger as ledger


@pytest.mark.parametrize("window", ["project daily", "project monthly", "global daily"])
async def test_inflight_ordinary_reservation_blocks_vivix_on_other_key(binding, monkeypatch, window):
    monkeypatch.setattr(get_settings(), "GLOBAL_DAILY_CAP_USD", 3 if window == "global daily" else 100)
    invalidate_global_cache()
    async with get_session() as session:
        project = await session.get(ProjectRow, 1)
        if window == "project daily":
            project.daily_cost_cap_usd = 3
        if window == "project monthly":
            project.monthly_cost_cap_usd = 3
        session.add(ApiKeyRow(id=2, provider="other", label="overlap-fixture", tier="paid",
                              token_encrypted="fixture-not-a-secret", scopes=["llm:video"],
                              daily_cost_cap_usd=10, monthly_cost_cap_usd=10,
                              is_active=True, is_alive=True))
    async with get_session() as session:
        key = await session.get(ApiKeyRow, 2)
        project = await session.get(ProjectRow, 1)
    # Deterministic concurrent interleaving: ordinary admission commits, its
    # provider operation remains in flight (no usage/refund), then Vivix admits.
    # No transport or live provider operation is needed to expose the gap.
    await reserve_cost(api_key=key, project=project, estimated_cost=2)
    with pytest.raises(VideoRejected, match=f"Vivix {window} budget exhausted"):
        await prepared(binding)
    async with get_session() as session:
        key = await session.get(ApiKeyRow, 2)
        assert key.daily_cost_used_usd == key.monthly_cost_used_usd == 2
        assert await session.scalar(select(func.count()).select_from(UsageLogRow)) == 0
