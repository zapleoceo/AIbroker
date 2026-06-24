"""Public health endpoints — no auth. Landing page lives in routes/landing.py."""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter
from sqlalchemy import text

from aibroker.db import get_session

router = APIRouter(tags=["health"])


@router.get("/healthz")
async def healthz() -> dict:
    return {"ok": True, "service": "aibroker", "ts": datetime.now(timezone.utc).isoformat()}


@router.get("/v1/health")
async def health_summary() -> dict:
    """Per-provider alive/dead/cooldown counts. Surface for ops dashboards."""
    async with get_session() as s:
        rows = (
            await s.execute(
                text(
                    "SELECT provider, "
                    "       COUNT(*) FILTER (WHERE is_active AND is_alive "
                    "                         AND (cooldown_until IS NULL OR cooldown_until < now())) AS alive, "
                    "       COUNT(*) FILTER (WHERE is_active AND is_alive AND cooldown_until > now()) AS cooldown, "
                    "       COUNT(*) FILTER (WHERE NOT is_alive OR NOT is_active) AS dead, "
                    "       COUNT(*) AS total "
                    "FROM api_keys GROUP BY provider ORDER BY provider"
                )
            )
        ).all()
    return {
        "providers": [
            {"provider": r[0], "alive": r[1], "cooldown": r[2], "dead": r[3], "total": r[4]}
            for r in rows
        ],
    }
