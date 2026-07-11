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


# A key's "daily" counters (daily_used, daily_cost_used_usd) never reset via
# any cron — nothing in the codebase writes daily_reset_at forward. Confirmed
# on prod: a key created 2026-06-26 had daily_used=51,921 six days later
# (~8.6k/day) with daily_reset_at still NULL — so "daily_limit"/"daily_cost_cap"
# were actually "lifetime limit", locking a key out forever the first time it
# was ever crossed rather than resetting the next day.
#
# Fix: lazy, self-healing reset — every READ and WRITE of these two columns
# treats them as 0 if `daily_reset_at IS DISTINCT FROM CURRENT_DATE` (works for
# NULL too), and every WRITE stamps `daily_reset_at = CURRENT_DATE`. No cron
# dependency; the counter heals itself the next time the key is touched after
# midnight UTC. Shared here (not duplicated) so pick_and_reserve's read-side
# check, record_usage's write-side increment, and cost_guard.reserve_cost's
# admission check can never drift out of sync on what "fresh" means.
FRESH_DAILY_USED_SQL = (
    "(CASE WHEN k.daily_reset_at IS DISTINCT FROM CURRENT_DATE "
    "THEN 0 ELSE k.daily_used END)"
)
FRESH_DAILY_COST_SQL = (
    "(CASE WHEN k.daily_reset_at IS DISTINCT FROM CURRENT_DATE "
    "THEN 0 ELSE k.daily_cost_used_usd END)"
)


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
        f"(k.daily_cost_cap_usd IS NULL OR {FRESH_DAILY_COST_SQL} < k.daily_cost_cap_usd)",
        f"(k.daily_limit = 0 OR {FRESH_DAILY_USED_SQL} < k.daily_limit)",
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
    #   3. random()           — true random rotation within the bucket. No
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
        toks_today AS MATERIALIZED (
            SELECT api_key_id,
                   COALESCE(SUM(tokens_in + tokens_out), 0) AS toks,
                   COALESCE(SUM(tokens_in), 0)  AS toks_in,
                   COALESCE(SUM(tokens_out), 0) AS toks_out,
                   COUNT(*) AS reqs
            FROM usage_log
            -- Sargable half-open bound on the bare created_at column (uses the
            -- ix_usage_created_at index). The old `created_at::date = today`
            -- cast forced a full seq-scan of usage_log on EVERY pick — ~220ms
            -- and rising with the table, on the hot path of every request.
            WHERE created_at >= date_trunc('day', now() AT TIME ZONE 'UTC')
              AND created_at <  date_trunc('day', now() AT TIME ZONE 'UTC') + INTERVAL '1 day'
              AND api_key_id IS NOT NULL
            GROUP BY api_key_id
        )
        UPDATE api_keys SET last_used_at = now()
        WHERE id = (
            SELECT k.id FROM api_keys k
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
                -- cooldown_until (429 → adaptive_cooldown). An earlier
                -- recent-errors sort caused one 'cleanest' key to monopolise
                -- traffic while peers sat idle, so random() replaced it and
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
        account_id=row["account_id"],
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


async def mark_cooldown(api_key_id: int, until: datetime, reason: str | None = None) -> None:
    # cooldown_until is a naive UTC TIMESTAMP; asyncpg rejects tz-aware values
    # ("can't subtract offset-naive and offset-aware"). Callers pass UTC-aware
    # datetimes — normalise to naive UTC here so prod (asyncpg) doesn't blow up.
    if until.tzinfo is not None:
        until = until.astimezone(UTC).replace(tzinfo=None)
    async with get_session() as s:
        await s.execute(
            text("UPDATE api_keys SET cooldown_until = :u, error_count = error_count + 1, "
                 "last_error = :reason WHERE id = :id"),
            {"u": until, "id": api_key_id, "reason": (reason or "")[:200] or None},
        )


async def mark_dead(api_key_id: int, reason: str | None = None) -> None:
    async with get_session() as s:
        await s.execute(
            text("UPDATE api_keys SET is_alive = FALSE, error_count = error_count + 1, "
                 "last_error = :reason WHERE id = :id"),
            {"id": api_key_id, "reason": (reason or "")[:200] or None},
        )


def _recover_set_sql(status: str, error_kind: str | None) -> str:
    """SQL SET-fragment that wipes stale failure state after a genuinely
    successful call, else empty. A success proves the key healthy RIGHT NOW, so
    clear last_error/error_count/cooldown_until inline rather than waiting up to
    MONITOR_INTERVAL_S for the monitor probe (2026-07-11) — a rate-limited key
    that recovered otherwise kept showing "жив" + a phantom last_error. Only a
    real success (status ok, no error_kind) resets; error rows keep their state.
    Pure + module-level so the SQLite quality gate covers the branch — the
    record_usage UPDATE it feeds is Postgres-only (integration job)."""
    if status == "ok" and error_kind is None:
        return ", last_error = NULL, error_count = 0, cooldown_until = NULL"
    return ""


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
        recover_sql = _recover_set_sql(status, error_kind)  # pragma: no cover
        await s.execute(
            text(
                "UPDATE api_keys AS k "
                f"SET daily_used = {FRESH_DAILY_USED_SQL} + 1, "
                f"    daily_cost_used_usd = {FRESH_DAILY_COST_SQL} + :c, "
                "    daily_reset_at = CURRENT_DATE, "
                "    monthly_cost_used_usd = monthly_cost_used_usd + :c, "
                f"    total_cost_usd = total_cost_usd + :c{recover_sql} "
                "WHERE k.id = :id"
            ),
            {"c": cost_usd, "id": api_key_id},
        )
    return int(usage_id)
