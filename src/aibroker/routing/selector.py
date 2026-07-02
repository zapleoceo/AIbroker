"""Atomic LRU + availability-aware key picker.

Uses `SELECT ... FOR UPDATE SKIP LOCKED` so multiple broker replicas can
pick concurrently without race. Touch last_used_at inside the same TX
to advance the LRU.
"""
from __future__ import annotations

from datetime import UTC, datetime

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
    """Pick the best available key for `provider` that supports `scope`.

    'Available' = is_active AND is_alive AND not in cooldown AND under per-key
    daily cost cap (if set). Returns None if nothing fits — caller walks the
    capability chain to the next provider.

    Ordering: non-reserve keys first (is_reserve ASC), then LRU-oldest. A
    reserved key (is_reserve=True) is therefore picked only when every shared
    key in its (provider, scope) group is exhausted — the Coach safety net.

    The returned row already has last_used_at advanced — so concurrent picks
    in another replica will see a different LRU order.
    """
    conds = [
        "k.provider = :provider",
        "k.is_active = TRUE",
        "k.is_alive = TRUE",
        "k.scopes ? :scope",
        "(k.cooldown_until IS NULL OR k.cooldown_until < now())",
        "(k.daily_cost_cap_usd IS NULL OR k.daily_cost_used_usd < k.daily_cost_cap_usd)",
        "(k.daily_limit = 0 OR k.daily_used < k.daily_limit)",
    ]
    params: dict[str, object] = {"provider": provider, "scope": scope}
    if require_tier:
        conds.append("k.tier = :tier")
        params["tier"] = require_tier

    where = " AND ".join(conds)
    # 2026-06-28: pure random rotation + soft skip of over-quota keys.
    #   1. is_reserve         — non-reserve first; Coach reserve last.
    #   2. is_quota_saturated — keys at ≥95% of their daily token/request
    #      quota are pushed to the back of the queue. They still get picked
    #      if every healthy peer is also saturated (fallback, not hard cut).
    #   3. recent_errors      — failures in the last 15 min push the key back.
    #   4. random()           — true random rotation within the bucket. No
    #      LRU, no daily_used sort — those caused one key per workload to
    #      monopolise its slot while others sat idle.
    #
    # Quota source priority per key:
    #   - discovered_*_limit (parsed from provider response headers)
    #   - PROVIDER_QUOTAS default (Python config, baked into VALUES CTE)
    #   - effective ∞ when neither knows (paid / unknown provider)
    from aibroker.providers.quotas import PROVIDER_QUOTAS
    def _q(v: int | None) -> str:
        return str(int(v)) if v else "NULL"
    quota_rows = ",\n          ".join(
        f"('{p}', {_q(q.req_per_day)}, {_q(q.tok_per_day)})"
        for p, q in PROVIDER_QUOTAS.items()
    )

    # Saturation per axis = today's usage ≥ 95% of the effective cap, where
    # the effective cap = manual_* > discovered_* > provider default. Four
    # axes: requests, total tokens, input tokens, output tokens. The corp
    # Gemini case (3M in / 80k out) is exactly why in/out are separate — its
    # 80k output cap saturates long before the 3M input cap.
    _INF = "999999999999"
    stmt = text(
        f"""
        WITH defaults(provider, req_def, tok_def) AS (VALUES
          {quota_rows}
        ),
        recent AS MATERIALIZED (
            SELECT api_key_id, COUNT(*) AS n
            FROM usage_log
            WHERE created_at > now() - INTERVAL '15 minutes'
              AND status <> 'ok'
              AND api_key_id IS NOT NULL
            GROUP BY api_key_id
        ),
        toks_today AS MATERIALIZED (
            SELECT api_key_id,
                   COALESCE(SUM(tokens_in + tokens_out), 0) AS toks,
                   COALESCE(SUM(tokens_in), 0)  AS toks_in,
                   COALESCE(SUM(tokens_out), 0) AS toks_out,
                   COUNT(*) AS reqs
            FROM usage_log
            WHERE created_at::date = (now() AT TIME ZONE 'UTC')::date
              AND api_key_id IS NOT NULL
            GROUP BY api_key_id
        )
        UPDATE api_keys SET last_used_at = now()
        WHERE id = (
            SELECT k.id FROM api_keys k
            LEFT JOIN recent r      ON r.api_key_id = k.id
            LEFT JOIN toks_today t  ON t.api_key_id = k.id
            LEFT JOIN defaults d    ON d.provider   = k.provider
            WHERE {where}
            ORDER BY
                k.is_reserve,
                -- Soft saturation skip across all 4 axes. Pushed to back when
                -- ≥95% on ANY axis; used only if every peer is also full.
                CASE
                  WHEN COALESCE(t.reqs, 0) >=
                       COALESCE(k.manual_req_limit, k.discovered_req_limit, d.req_def, {_INF}) * 0.95
                    OR COALESCE(t.toks, 0) >=
                       COALESCE(k.manual_tok_limit, k.discovered_tok_limit, d.tok_def, {_INF}) * 0.95
                    OR COALESCE(t.toks_in, 0) >=
                       COALESCE(k.manual_tok_in_limit, {_INF}) * 0.95
                    OR COALESCE(t.toks_out, 0) >=
                       COALESCE(k.manual_tok_out_limit, {_INF}) * 0.95
                  THEN 1 ELSE 0
                END,
                -- Pure random within the healthy bucket. Errors are already
                -- reflected in is_alive (auth fails → mark_dead) and
                -- cooldown_until (429 → adaptive_cooldown). The earlier
                -- recent_errors sort caused a single 'cleanest' key to
                -- monopolise traffic while peers sat idle — bucketing didn't
                -- help when one key was in its own bucket. random() now
                -- distributes uniformly across every healthy peer.
                random()
            LIMIT 1
            FOR UPDATE OF k SKIP LOCKED
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
        is_reserve=row["is_reserve"],
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
    # cooldown_until is a naive UTC TIMESTAMP; asyncpg rejects tz-aware values
    # ("can't subtract offset-naive and offset-aware"). Callers pass UTC-aware
    # datetimes — normalise to naive UTC here so prod (asyncpg) doesn't blow up.
    if until.tzinfo is not None:
        until = until.astimezone(UTC).replace(tzinfo=None)
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
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> int:
    """Insert usage_log row + update counters on the api_key.

    cache_read_tokens/cache_write_tokens default to 0 — only anthropic chat
    calls populate them today (see providers/litellm_adapter.py
    apply_prompt_cache); every other call site (embed, transcribe, vending's
    /v1/usage self-report) has no cache concept and leaves them at 0.

    Returns the new row's id — the broker-side request ID. Threaded back
    through the outcome dataclasses and returned to the caller in the API
    response (`request_id`) so both sides can find the same call in their own
    logs / this dashboard's project detail table."""
    async with get_session() as s:
        usage_id = (await s.execute(
            text(
                "INSERT INTO usage_log "
                "(api_key_id, project_id, lease_id, provider, model, capability, workflow, "
                " tokens_in, tokens_out, cache_read_tokens, cache_write_tokens, "
                " cost_usd, latency_ms, status, error_kind, http_status) "
                "VALUES (:k, :p, :l, :pr, :m, :c, :w, :ti, :to, :cr, :cw, :co, :lm, :s, :e, :h) "
                "RETURNING id"
            ),
            {
                "k": api_key_id, "p": project_id, "l": lease_id, "pr": provider,
                "m": model, "c": capability, "w": workflow,
                "ti": tokens_in, "to": tokens_out,
                "cr": cache_read_tokens, "cw": cache_write_tokens,
                "co": cost_usd, "lm": latency_ms,
                "s": status, "e": error_kind, "h": http_status,
            },
        )).scalar_one()
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
    return int(usage_id)
