"""Atomic LRU + availability-aware key picker.

Uses `SELECT ... FOR UPDATE SKIP LOCKED` so multiple broker replicas can
pick concurrently without race. Touch last_used_at inside the same TX
to advance the LRU.
"""
from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from aibroker.db.engine import get_session
from aibroker.db.models import ApiKeyRow
from aibroker.db.resilience import retry_terminal_write
from aibroker.routing import circuit, shared_state

# Free provider soft-skipped when this many of its keys are in timeout-cooldown
# — the whole pool is degraded, so fail the chain over cheaply rather than send
# another ~60s answerless call (2026-07-16 free-pool timeout storm).
_TIMEOUT_STORM_MIN_KEYS = 2

# Providers whose prompt cache is PER-KEY (per-account) AND that carry a paid,
# high-throughput API with no tight per-key RPM limit. For these, keeping ALL of
# a project's traffic on ONE key (a warm cache-hit input is ~50x cheaper on
# deepseek) beats the LRU spread the normal pick does — so the sticky fast path
# pins to the affinity key directly instead of letting SKIP-LOCKED scatter a
# burst across keys (each a cold cache). Free/RPM-limited providers (cerebras,
# groq, …) are excluded: concentrating them would hit rate limits and their
# cache gives no token discount anyway.
_CACHE_STICKY_PROVIDERS = frozenset({"deepseek", "anthropic"})


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

# Effective ∞ for an uncapped saturation axis (paid / unknown provider).
_INF = "999999999999"

# The 4-axis saturation verdict needs an aggregate over today's usage_log
# slice (30-60k rows). Computing it inline on EVERY pick (~60-100k picks/day)
# made the hot path O(day-rows × picks); staleness is harmless — a key crosses
# 95% of a DAILY quota once a day, not once a second. With the shared layer the
# staleness compounds up to ~2×TTL (~30s): a worker whose local TTL just
# expired can adopt a shared verdict another worker computed almost a full TTL
# ago, then keep it for its own TTL. Same in-process pattern as
# cost_guard._global_cache: per uvicorn worker, no Redis dependency, worst
# case each worker recomputes once per TTL.
_SATURATION_TTL_S = 15.0
_saturated: dict[str, Any] = {"ids": frozenset(), "fetched_at": float("-inf")}

# Provider prompt caches (deepseek auto prefix-cache, gemini implicit) live
# per ACCOUNT/key — pure random() rotation fragmented a project's stable
# prompt prefix across every key, wasting hits (deepseek measured 56%, could
# be much higher). Map (project_id, provider) → last successful key so repeat
# traffic lands where the prefix is already warm. TTL ≈ provider cache
# retention windows. Since 2026-07-16 the map is shared across workers/nodes
# via routing/shared_state (Redis, fail-open); this dict stays as the
# fallback for single-node / Redis-down / SQLite-test runs. TTL constant
# lives in shared_state so both layers expire in step.
_AFFINITY_TTL_S = shared_state.AFFINITY_TTL_S
_affinity: dict[tuple[int, str], tuple[int, float]] = {}


def _note_affinity(project_id: int, provider: str, api_key_id: int) -> None:
    """Pin the key that just successfully served (project, provider).
    Internal — services go through note_affinity_shared so the pin also
    reaches the cross-worker store."""
    _affinity[(project_id, provider)] = (api_key_id, time.monotonic())


async def note_affinity_shared(project_id: int, provider: str, api_key_id: int) -> None:
    """_note_affinity + publish the pin to the cross-worker store (fail-open)."""
    _note_affinity(project_id, provider, api_key_id)
    await shared_state.set_affinity(project_id, provider, api_key_id)


async def _affinity_for_shared(project_id: int | None, provider: str) -> int | None:
    """Cross-worker pin first; in-process dict as the fallback/miss path."""
    if project_id is None:
        return None
    shared = await shared_state.get_affinity(project_id, provider)
    if shared is not None:
        return shared
    return _affinity_for(project_id, provider)


def _affinity_for(project_id: int | None, provider: str) -> int | None:
    if project_id is None:
        return None
    entry = _affinity.get((project_id, provider))
    if entry is None:
        return None
    api_key_id, noted_at = entry
    if time.monotonic() - noted_at > _AFFINITY_TTL_S:
        del _affinity[(project_id, provider)]
        return None
    return api_key_id


def invalidate_saturation_cache() -> None:
    _saturated["fetched_at"] = float("-inf")


def _quota_values_sql() -> str:
    """VALUES rows for the defaults CTE — one (provider, req, tok) per seed."""
    from aibroker.providers.quotas import PROVIDER_QUOTAS

    def q(v: int | None) -> str:
        return str(int(v)) if v else "NULL"

    return ",\n          ".join(
        f"('{p}', {q(quota.req_per_day)}, {q(quota.tok_per_day)})"
        for p, quota in PROVIDER_QUOTAS.items()
    )


def _saturation_order_params(
    saturated: frozenset[int], affinity_id: int | None,
    timed_out: frozenset[int] = frozenset(),
) -> dict[str, object]:
    """Bind params for the pick ORDER BY. saturated_ids/timed_out_ids are never
    empty — asyncpg needs a concrete bigint[] — and -1 matches no real key id."""
    return {
        "saturated_ids": list(saturated) or [-1],
        "timed_out_ids": list(timed_out) or [-1],
        "aff": affinity_id if affinity_id is not None else -1,
    }


async def _saturated_key_ids() -> frozenset[int]:  # pragma: no cover — Postgres-only, exercised by tests/test_selector.py
    now = time.monotonic()
    if now - _saturated["fetched_at"] < _SATURATION_TTL_S:
        return _saturated["ids"]
    # Local TTL expired — another worker may have computed the verdict within
    # the same window: take it from the shared store before hitting the DB.
    shared = await shared_state.get_saturated()
    if shared is not None:
        _saturated["ids"] = shared
        _saturated["fetched_at"] = now
        return shared
    # Saturation per axis = today's usage ≥ 95% of the effective cap, resolved
    # manual_* > discovered_* > PROVIDER_QUOTAS seed. Four axes: requests,
    # total tokens, input, output — the corp Gemini case (3M in / 80k out) is
    # exactly why in/out are separate: its 80k output cap saturates long
    # before the 3M input cap.
    stmt = text(
        f"""
        WITH defaults(provider, req_def, tok_def) AS (VALUES
          {_quota_values_sql()}
        ),
        toks_today AS (
            SELECT api_key_id,
                   COALESCE(SUM(tokens_in + tokens_out), 0) AS toks,
                   COALESCE(SUM(tokens_in), 0)  AS toks_in,
                   COALESCE(SUM(tokens_out), 0) AS toks_out,
                   COUNT(*) AS reqs
            FROM usage_log
            -- Sargable half-open bound on the bare created_at column (uses
            -- ix_usage_created_at); a ::date cast would force a full seq scan.
            WHERE created_at >= date_trunc('day', now() AT TIME ZONE 'UTC')
              AND created_at <  date_trunc('day', now() AT TIME ZONE 'UTC') + INTERVAL '1 day'
              AND api_key_id IS NOT NULL
            GROUP BY api_key_id
        )
        SELECT k.id FROM api_keys k
        JOIN toks_today t   ON t.api_key_id = k.id
        LEFT JOIN defaults d ON d.provider  = k.provider
        WHERE t.reqs >= COALESCE(k.manual_req_limit, k.discovered_req_limit, d.req_def, {_INF}) * 0.95
           OR t.toks >= COALESCE(k.manual_tok_limit, k.discovered_tok_limit, d.tok_def, {_INF}) * 0.95
           OR t.toks_in  >= COALESCE(k.manual_tok_in_limit, {_INF}) * 0.95
           OR t.toks_out >= COALESCE(k.manual_tok_out_limit, {_INF}) * 0.95
        """
    )
    async with get_session() as s:
        ids = frozenset((await s.execute(stmt)).scalars().all())
    _saturated["ids"] = ids
    _saturated["fetched_at"] = now
    await shared_state.set_saturated(ids, _SATURATION_TTL_S)
    return ids


async def pick_and_reserve(
    provider: str,
    scope: str,
    *,
    require_tier: str | None = None,
    project_id: int | None = None,
) -> ApiKeyRow | None:
    """Pick the best available key for `provider` that supports `scope`.

    'Available' = is_active AND is_alive AND not in cooldown AND under per-key
    daily cost cap (if set). Returns None if nothing fits — caller walks the
    capability chain to the next provider.

    Ordering: non-reserve keys first (is_reserve ASC), saturated keys pushed
    back, then project→key cache affinity as a tie-break, then random(). A
    reserved key (is_reserve=True) is therefore picked only when every shared
    key in its (provider, scope) group is exhausted — the Coach safety net.

    The returned row already has last_used_at advanced — so concurrent picks
    in another replica will see a different LRU order.
    """
    # Circuit-breaker: a free provider whose pool is timing out in bulk is
    # soft-skipped with NO call sent, so the chain fails over cheaply instead of
    # burning another ~60s answerless call. Applies to any non-paid pick — both
    # a normal free walk (require_tier None) AND the free-only tail after a paid
    # budget downgrade (require_tier "free"): a timeout storm coinciding with a
    # spent cap is exactly when we must NOT feed answerless calls to a storming
    # free pool. Only the guaranteed-answer PAID escalation is exempt (it must
    # not be starved by a transient storm). 2026-07-19 review.
    if require_tier != "paid" and provider in circuit.providers_in_timeout_storm(
            _TIMEOUT_STORM_MIN_KEYS):
        return None

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
    saturated = await _saturated_key_ids()  # pragma: no cover — Postgres-only glue, covered by tests/test_selector.py
    timed_out = circuit.recent_timeout_key_ids()  # pragma: no cover — same
    affinity_id = await _affinity_for_shared(project_id, provider)  # pragma: no cover — same
    # Don't let cache-affinity pin to a key that just hung — a warm prompt cache
    # isn't worth re-hitting a degraded key (2026-07-16 storm).
    if affinity_id is not None and affinity_id in timed_out:  # pragma: no cover — same
        affinity_id = None

    where = " AND ".join(conds)

    # Cache-sticky fast path (2026-07-20): for deepseek/anthropic, pin ALL of the
    # project's traffic to the affinity key so its PER-KEY prompt cache stays hot.
    # The normal SKIP-LOCKED pick below scatters a concurrent burst across keys
    # and lets the affinity pin flip to a cold one — measured deepseek smart
    # cache hit ~50% (dragged by cold starts) vs ~80% on a continuously-warm key,
    # and a warm deepseek input token is 50x cheaper. No LRU/SKIP LOCKED here:
    # concurrent picks serialize on the one row and all get it (paid API, no RPM
    # limit). Falls through to the normal pick when the pinned key is unavailable
    # (cooled, or per-key cost cap spent). Excluded for require_tier="free": these
    # are paid-tier keys.
    if provider in _CACHE_STICKY_PROVIDERS and affinity_id is not None and require_tier != "free":  # pragma: no cover — Postgres-only, exercised by test_selector.py
        sticky = text(f"UPDATE api_keys AS k SET last_used_at = now() "  # pragma: no cover
                      f"WHERE k.id = :aff AND ({where}) RETURNING *")  # pragma: no cover
        async with get_session() as s:  # pragma: no cover
            srow = (await s.execute(sticky, {**params, "aff": affinity_id})).mappings().first()  # pragma: no cover
        if srow is not None:  # pragma: no cover
            return _hydrate_key_row(srow)  # pragma: no cover

    params.update(_saturation_order_params(saturated, affinity_id, timed_out))  # pragma: no cover — same
    # 2026-06-28 (random + soft saturation skip), 2026-07-12 (TTL cache + affinity):
    #   1. is_reserve  — non-reserve first; Coach reserve last.
    #   2. saturated   — keys ≥95% on any daily quota axis (verdict from the
    #      TTL-cached _saturated_key_ids, no per-pick usage_log aggregate) are
    #      pushed to the back. Still soft: picked if every peer is also full.
    #   3. timed_out   — keys that hung within the last ~2min sink below their
    #      healthy siblings, so a fresh key of the same provider is preferred
    #      over re-hitting one that just wasted a ~60s answerless call. Soft
    #      (peer of saturation): picked if every sibling also just hung.
    #   4. affinity    — the key that last successfully served this (project,
    #      provider) wins ties, keeping its provider-side prompt cache warm.
    #      Never overrides reserve/saturation/WHERE filters — tie-break only.
    #   5. random()    — rotation within the bucket. No LRU, no daily_used
    #      sort — those caused one key per workload to monopolise its slot
    #      while others sat idle.
    stmt = text(
        f"""
        UPDATE api_keys SET last_used_at = now()
        WHERE id = (
            SELECT k.id FROM api_keys k
            WHERE {where}
            ORDER BY
                k.is_reserve,
                COALESCE(k.id = ANY(CAST(:saturated_ids AS bigint[])), FALSE),
                COALESCE(k.id = ANY(CAST(:timed_out_ids AS bigint[])), FALSE),
                (k.id = :aff) DESC,
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
    return _hydrate_key_row(row)  # pragma: no cover — Postgres-only pick path


def _hydrate_key_row(row) -> ApiKeyRow:  # pragma: no cover — Postgres row → ORM-like
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


async def mark_cooldown(
    api_key_id: int,
    until: datetime,
    reason: str | None = None,
    *,
    session: AsyncSession | None = None,
) -> None:
    # cooldown_until is a naive UTC TIMESTAMP; asyncpg rejects tz-aware values
    # ("can't subtract offset-naive and offset-aware"). Callers pass UTC-aware
    # datetimes — normalise to naive UTC here so prod (asyncpg) doesn't blow up.
    if until.tzinfo is not None:
        until = until.astimezone(UTC).replace(tzinfo=None)
    stmt = text("UPDATE api_keys SET cooldown_until = :u, error_count = error_count + 1, "
                "last_error = :reason WHERE id = :id")
    params = {"u": until, "id": api_key_id, "reason": (reason or "")[:200] or None}
    # `session` lets _penalize land the whole penalty (adaptive COUNT + this
    # UPDATE) in one session/transaction instead of one session per statement.
    if session is not None:
        await session.execute(stmt, params)
        return
    async with get_session() as s:
        await s.execute(stmt, params)


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


@retry_terminal_write
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
    apply_prompt_cache); every other call site (embed, transcribe)
    /v1/usage self-report) has no cache concept and leaves them at 0.

    Returns the new row's id — the broker-side request ID. Threaded back
    through the outcome dataclasses and returned to the caller in the API
    response (`request_id`) so both sides can find the same call in their own
    logs / this dashboard's project detail table."""
    params = {
        "k": api_key_id, "p": project_id, "l": lease_id, "pr": provider,
        "m": model, "c": capability, "w": workflow,
        "ti": tokens_in, "to": tokens_out,
        "cr": cache_read_tokens, "cw": cache_write_tokens,
        "co": cost_usd, "lm": latency_ms,
        "s": status, "e": error_kind, "h": http_status,
    }
    insert_sql = (
        "INSERT INTO usage_log "
        "(api_key_id, project_id, lease_id, provider, model, capability, workflow, "
        " tokens_in, tokens_out, cache_read_tokens, cache_write_tokens, "
        " cost_usd, latency_ms, status, error_kind, http_status) "
        "VALUES (:k, :p, :l, :pr, :m, :c, :w, :ti, :to, :cr, :cw, :co, :lm, :s, :e, :h) "
        "RETURNING id"
    )
    recover_sql = _recover_set_sql(status, error_kind)
    update_sql = (
        "UPDATE api_keys AS k "
        f"SET daily_used = {FRESH_DAILY_USED_SQL} + 1, "
        f"    daily_cost_used_usd = {FRESH_DAILY_COST_SQL} + :co, "
        "    daily_reset_at = CURRENT_DATE, "
        "    monthly_cost_used_usd = monthly_cost_used_usd + :co, "
        f"    total_cost_usd = total_cost_usd + :co{recover_sql} "
        "WHERE k.id = :k"
    )
    async with get_session() as s:
        if s.bind.dialect.name == "postgresql":  # pragma: no cover — Postgres-only, exercised by tests/test_selector.py
            # One round-trip: data-modifying CTE folds the INSERT and the
            # counter UPDATE into a single statement (2026-07-16). record_usage
            # runs once per attempt at 60-100k picks/day — the second statement
            # was half this hot path's DB chatter.
            usage_id = (await s.execute(
                text(f"WITH ins AS ({insert_sql}), "
                     f"upd AS ({update_sql} RETURNING 1) "
                     "SELECT id FROM ins"),
                params,
            )).scalar_one()
        else:
            # SQLite (test gate) allows only SELECT in a WITH clause — a
            # data-modifying CTE is a syntax error there. Same statements,
            # same session/transaction, just two round-trips.
            usage_id = (await s.execute(text(insert_sql), params)).scalar_one()
            await s.execute(text(update_sql), params)
    return int(usage_id)
