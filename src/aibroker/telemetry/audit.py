"""Append-only audit log. Every admin op + every key checkout."""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import text

from aibroker.db import get_session

log = logging.getLogger(__name__)


async def audit(
    *,
    actor: str,
    action: str,
    target: str | None = None,
    metadata: dict[str, Any] | None = None,
    ip: str | None = None,
) -> None:
    try:
        async with get_session() as s:
            await s.execute(
                text(
                    "INSERT INTO audit_log (actor, action, target, metadata, ip) "
                    "VALUES (:a, :ac, :t, CAST(:m AS JSONB), :ip)"
                ),
                {
                    "a": actor, "ac": action, "t": target,
                    "m": _json(metadata or {}), "ip": ip,
                },
            )
    except Exception as e:
        log.warning("audit insert failed: %s", e)


def _json(d: dict[str, Any]) -> str:
    import json
    return json.dumps(d, ensure_ascii=False, default=str)
