"""Atomic LRU + availability-aware key picker.

Uses `SELECT ... FOR UPDATE SKIP LOCKED` so multiple broker replicas can
pick concurrently without race. Touch last_used_at inside the same TX
to advance the LRU.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import text

from aibroker.db.engine import get_session
from aibroker.db.models import ApiKeyRow


class SelectionError(Exception):
    """No usable api_key for the (provider, scope) request."""


async def pick_and_reserve(
    provider: str,
    scope: str,
    *,
    require_tier: str | None = None,
) -> ApiKeyRow | None:
    """Pick the LRU-oldest available key for `provider` that supports `scope`.

    'Available' = is_active AND is_alive AND not in cooldown AND under per-key
    daily cost cap (if set). Returns None if nothing fits — caller walks the
    capability chain to the next provider.

    The returned row already has last_used_at advanced — so concurrent picks
    in another replica will see a different LRU order.
    """
    conds = [
        "provider = :provider",
        "is_active = TRUE",
        "is_alive = TRUE",
        "scopes ? :scope",
        "(cooldown_until IS NULL OR cooldown_until < now())",
        "(daily_cost_cap_usd IS NULL OR daily_cost_used_usd < daily_cost_cap_usd)",
        "(daily_limit = 0 OR daily_used < daily_limit)",
    ]
    params: dict[str, object] = {"provider": provider, "scope": scope}
    if require_tier:
        conds.append("tier = :tier")
        params["tier"] = require_tier

    where = " AND ".join(conds)
    stmt = text(
        f"""
        UPDATE api_keys SET last_used_at = now()
        WHERE id = (
            SELECT id FROM api_keys
            WHERE {where}
            ORDER BY last_used_at NULLS FIRST, id
            LIMIT 1
            FOR UPDATE SKIP LOCKED
        )
        RETURNING *
        """
    )

    async with get_session() as s:
        row = (await s.execute(stmt, params)).mappings().first()
    if row is None:
        return None
    # Hydrate into ORM-like object
    return ApiKeyRow(
        id=row["id"],
        provider=row["provider"],
        label=row["label"],
        tier=row["tier"],
        scopes=row["scopes"],
        token_encrypted=row["token_encrypted"],
        is_active=row["is_active"],
        is_alive=row["is_alive"],
        daily_limit=row["daily_limit"],
        daily_used=row["daily_used"],
        daily_cost_cap_usd=row["daily_cost_cap_usd"],
        daily_cost_used_usd=row["daily_cost_used_usd"],
        monthly_cost_cap_usd=row["monthly_cost_cap_usd"],
        monthly_cost_used_usd=row["monthly_cost_used_usd"],
        total_cost_usd=row["total_cost_usd"],
        daily_reset_at=row["daily_reset_at"],
        cooldown_until=row["cooldown_until"],
        error_count=row["error_count"],
        last_used_at=row["last_used_at"],
        last_alive_check_at=row["last_alive_check_at"],
        notes=row["notes"],
        created_at=row["created_at"],
    )


async def mark_cooldown(api_key_id: int, until: datetime) -> None:
    async with get_session() as s:
        await s.execute(
            text("UPDATE api_keys SET cooldown_until = :u, error_count = error_count + 1 "
                 "WHERE id = :id"),
            {"u": until, "id": api_key_id},
        )


async def mark_dead(api_key_id: int) -> None:
    async with get_session() as s:
        await s.execute(
            text("UPDATE api_keys SET is_alive = FALSE, error_count = error_count + 1 "
                 "WHERE id = :id"),
            {"id": api_key_id},
        )


async def record_usage(
    *,
    api_key_id: int,
    project_id: int | None,
    lease_id: str | None,
    provider: str,
    model: str | None,
    capability: str | None,
    workflow: str | None,
    tokens_in: int,
    tokens_out: int,
    cost_usd: float,
    latency_ms: int | None,
    status: str,
    error_kind: str | None,
    http_status: int | None,
) -> None:
    """Insert usage_log row + update counters on the api_key."""
    async with get_session() as s:
        await s.execute(
            text(
                "INSERT INTO usage_log "
                "(api_key_id, project_id, lease_id, provider, model, capability, workflow, "
                " tokens_in, tokens_out, cost_usd, latency_ms, status, error_kind, http_status) "
                "VALUES (:k, :p, :l, :pr, :m, :c, :w, :ti, :to, :co, :lm, :s, :e, :h)"
            ),
            {
                "k": api_key_id, "p": project_id, "l": lease_id, "pr": provider,
                "m": model, "c": capability, "w": workflow,
                "ti": tokens_in, "to": tokens_out, "co": cost_usd, "lm": latency_ms,
                "s": status, "e": error_kind, "h": http_status,
            },
        )
        await s.execute(
            text(
                "UPDATE api_keys "
                "SET daily_used = daily_used + 1, "
                "    daily_cost_used_usd = daily_cost_used_usd + :c, "
                "    monthly_cost_used_usd = monthly_cost_used_usd + :c, "
                "    total_cost_usd = total_cost_usd + :c "
                "WHERE id = :id"
            ),
            {"c": cost_usd, "id": api_key_id},
        )
