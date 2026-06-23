"""Cap enforcement: per-key, per-project, global. Run BEFORE every paid call.

If any cap would be exceeded by est_cost, raise CostGuardError.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

from sqlalchemy import select, text

from aibroker.config import get_settings
from aibroker.db import get_session
from aibroker.db.models import ApiKeyRow, ProjectRow


class CostGuardError(Exception):
    """Raised when a cap would be exceeded by the attempted call."""

    def __init__(self, kind: str, limit: float, used: float, attempted: float):
        self.kind = kind
        self.limit = limit
        self.used = used
        self.attempted = attempted
        super().__init__(
            f"{kind} cap exceeded: used ${used:.4f} + attempt ${attempted:.4f} > ${limit:.4f}"
        )


# Cache global daily spend for 30s (low precision is OK — we mostly free tier)
_global_cache: dict[str, float] = {"value": 0.0, "fetched_at": 0.0}
_GLOBAL_TTL_S = 30


async def _global_cost_today() -> float:
    now = time.time()
    if now - _global_cache["fetched_at"] < _GLOBAL_TTL_S:
        return _global_cache["value"]
    today = datetime.now(timezone.utc).date()
    async with get_session() as s:
        v = (
            await s.execute(
                text(
                    "SELECT COALESCE(SUM(cost_usd), 0) FROM usage_log "
                    "WHERE created_at::date = :d"
                ),
                {"d": today},
            )
        ).scalar() or 0.0
    _global_cache["value"] = float(v)
    _global_cache["fetched_at"] = now
    return float(v)


def invalidate_global_cache() -> None:
    _global_cache["fetched_at"] = 0.0


async def check_caps(
    *,
    api_key: ApiKeyRow,
    project: ProjectRow,
    estimated_cost: float,
) -> None:
    """Raise CostGuardError if any of: api_key cap, project cap, global cap
    would be exceeded.  For free-tier keys (tier='free' and cost==0) → no-op."""
    if api_key.tier == "free" and estimated_cost <= 0:
        return

    # 1. per-key
    if api_key.daily_cost_cap_usd is not None:
        if api_key.daily_cost_used_usd + estimated_cost > api_key.daily_cost_cap_usd:
            raise CostGuardError(
                "api_key",
                api_key.daily_cost_cap_usd,
                api_key.daily_cost_used_usd,
                estimated_cost,
            )

    # 2. per-project
    if project.daily_cost_cap_usd is not None:
        today = datetime.now(timezone.utc).date()
        async with get_session() as s:
            used = (
                await s.execute(
                    text(
                        "SELECT COALESCE(SUM(cost_usd), 0) FROM usage_log "
                        "WHERE project_id = :pid AND created_at::date = :d"
                    ),
                    {"pid": project.id, "d": today},
                )
            ).scalar() or 0.0
        if used + estimated_cost > project.daily_cost_cap_usd:
            raise CostGuardError(
                "project",
                project.daily_cost_cap_usd,
                float(used),
                estimated_cost,
            )

    # 3. global
    s = get_settings()
    if s.GLOBAL_DAILY_CAP_USD > 0:
        used = await _global_cost_today()
        if used + estimated_cost > s.GLOBAL_DAILY_CAP_USD:
            raise CostGuardError(
                "global", s.GLOBAL_DAILY_CAP_USD, used, estimated_cost
            )
