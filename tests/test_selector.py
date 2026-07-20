"""Selector — atomic LRU + cap-aware key picker.

Skipped on SQLite: selector relies on JSONB `?` operator + FOR UPDATE SKIP LOCKED,
neither of which SQLite supports. These tests need a real Postgres to run.
"""
from __future__ import annotations

import os
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import insert

from aibroker.db import get_session
from aibroker.db.models import ApiKeyRow
from aibroker.routing import selector
from aibroker.routing.selector import (
    _note_affinity,
    invalidate_saturation_cache,
    mark_cooldown,
    mark_dead,
    pick_and_reserve,
    record_usage,
)

_DB = os.environ.get("DATABASE_URL", "")
pytestmark = pytest.mark.skipif(
    "postgres" not in _DB and "asyncpg" not in _DB,
    reason="Selector uses Postgres-specific JSONB ? operator + FOR UPDATE SKIP LOCKED",
)


@pytest.fixture(autouse=True)
def _fresh_selector_caches():
    """Key ids are reused across tests (fresh schema per test) — a stale
    saturation/affinity entry from a previous test would poison this one."""
    invalidate_saturation_cache()
    selector._affinity.clear()
    yield


async def _add_key(provider: str, label: str, **kw) -> int:
    """Insert one row, return its id."""
    defaults = {
        "provider": provider, "label": label, "tier": "free",
        "scopes": ["llm:chat"], "token_encrypted": "dummy",
        "is_active": True, "is_alive": True,
        "daily_limit": 999999, "daily_used": 0,
        "daily_cost_used_usd": 0.0, "monthly_cost_used_usd": 0.0,
        "total_cost_usd": 0.0, "error_count": 0, "notes": "",
    }
    defaults.update(kw)
    async with get_session() as s:
        r = await s.execute(insert(ApiKeyRow).returning(ApiKeyRow.id), defaults)
        return int(r.scalar_one())


async def test_pick_none_when_no_keys():
    result = await pick_and_reserve("cerebras", "llm:chat")
    assert result is None


async def test_storm_skip_applies_to_free_downgrade_but_not_paid():
    """2026-07-19: the timeout-storm circuit skip must fire for the free-only
    tail after a paid-budget downgrade (require_tier='free'), not only an
    initial free walk (require_tier=None) — a storm coinciding with a spent cap
    is exactly when answerless calls into a storming free pool must be avoided.
    Only the guaranteed-answer PAID escalation is exempt."""
    from aibroker.routing import circuit

    circuit.reset()
    fid = await _add_key("cerebras", "free-a")
    # A timeout storm = >= _TIMEOUT_STORM_MIN_KEYS (2) distinct cerebras keys hung.
    circuit.note_timeout("cerebras", fid)
    circuit.note_timeout("cerebras", fid + 999999)
    try:
        # downgraded free pick → storm-skipped though a healthy key exists
        assert await pick_and_reserve("cerebras", "llm:chat", require_tier="free") is None
        # normal free pick → also skipped
        assert await pick_and_reserve("cerebras", "llm:chat") is None
        # PAID escalation → exempt: reaches the SQL and picks the paid key
        pid = await _add_key("cerebras", "paid-a", tier="paid")
        picked = await pick_and_reserve("cerebras", "llm:chat", require_tier="paid")
        assert picked is not None and picked.id == pid
    finally:
        circuit.reset()


def test_cache_sticky_providers_are_paid_percache_only():
    """Sticky concentration is only for PAID per-account-cache providers with no
    tight RPM limit. Free/RPM-limited providers must stay out (concentrating
    them hits rate limits + their cache gives no token discount)."""
    from aibroker.routing.selector import _CACHE_STICKY_PROVIDERS
    assert "deepseek" in _CACHE_STICKY_PROVIDERS
    assert "anthropic" in _CACHE_STICKY_PROVIDERS
    assert len(_CACHE_STICKY_PROVIDERS) == 2
    assert "cerebras" not in _CACHE_STICKY_PROVIDERS
    assert "groq" not in _CACHE_STICKY_PROVIDERS


async def test_sticky_pick_concentrates_deepseek_under_concurrent_burst():
    """2026-07-20: deepseek's prompt cache is per-key. Under a concurrent burst
    the normal SKIP-LOCKED pick scatters across keys (each a cold cache); the
    sticky path pins the whole burst to the affinity key so its cache stays hot.
    All 8 concurrent picks must land on the pin, not spread."""
    import asyncio

    b = await _add_key("deepseek", "b", tier="paid")
    for lbl in ("c", "d", "e"):
        await _add_key("deepseek", lbl, tier="paid")
    _note_affinity(4, "deepseek", b)
    picks = await asyncio.gather(*(
        pick_and_reserve("deepseek", "llm:chat", project_id=4) for _ in range(8)))
    assert all(p is not None and p.id == b for p in picks)


async def test_sticky_pick_falls_through_when_pin_unavailable():
    """A cooled/capped pin must not strand the project — the sticky path yields
    nothing and the normal pick takes the healthy key."""
    a = await _add_key("deepseek", "a", tier="paid")
    b = await _add_key("deepseek", "b", tier="paid",
                       cooldown_until=datetime.now(UTC).replace(tzinfo=None) + timedelta(minutes=5))
    _note_affinity(4, "deepseek", b)   # pin is cooled → unavailable
    picked = await pick_and_reserve("deepseek", "llm:chat", project_id=4)
    assert picked is not None and picked.id == a


async def test_pick_distributes_randomly_across_eligible_keys():
    """2026-06-28: LRU replaced by random() — over 100 picks both keys get
    real share of traffic instead of one monopolising. Reset last_used_at
    each iteration so neither becomes 'oldest'."""
    from sqlalchemy import update
    await _add_key("cerebras", "a")
    await _add_key("cerebras", "b")
    counts = {"a": 0, "b": 0}
    for _ in range(100):
        picked = await pick_and_reserve("cerebras", "llm:chat")
        assert picked is not None
        counts[picked.label] += 1
        # reset both back so neither dominates by LRU; only random() decides
        async with get_session() as s:
            await s.execute(update(ApiKeyRow).values(last_used_at=None))
    # With random rotation, expect roughly 50/50 ± 30 over 100 picks
    assert 20 <= counts["a"] <= 80, f"distribution skewed: {counts}"
    assert 20 <= counts["b"] <= 80, f"distribution skewed: {counts}"


async def test_pick_pushes_over_quota_key_to_back():
    """A key already burned >=95% of today's token quota should not be picked
    while a clean peer is eligible. Cerebras default tok_per_day=1_000_000;
    seed today's usage_log to push key 'hot' over the threshold."""
    from sqlalchemy import insert as sql_insert
    await _add_key("cerebras", "cold")
    hot = await _add_key("cerebras", "hot")
    # 1.1M tokens today on 'hot' → > 95% of 1M default cap
    async with get_session() as s:
        from aibroker.db.models import UsageLogRow
        await s.execute(sql_insert(UsageLogRow), [{
            "api_key_id": hot, "provider": "cerebras", "tokens_in": 1_100_000,
            "tokens_out": 0, "cost_usd": 0.0, "status": "ok",
        }])
    invalidate_saturation_cache()  # seeded AFTER import-time cache state — force a fresh verdict
    # 20 picks — none should land on 'hot' while 'cold' is alive
    cold_count = hot_count = 0
    from sqlalchemy import update
    for _ in range(20):
        picked = await pick_and_reserve("cerebras", "llm:chat")
        assert picked is not None
        if picked.label == "cold":
            cold_count += 1
        else:
            hot_count += 1
        async with get_session() as s:
            await s.execute(update(ApiKeyRow).values(last_used_at=None))
    assert hot_count == 0, f"saturated key still picked {hot_count} times"
    assert cold_count == 20


async def test_pick_respects_manual_tok_out_limit():
    """A key with a manual OUTPUT-token cap (e.g. corp Gemini 80k out) is
    skipped once today's tokens_out ≥95% of it — even though its total/in
    usage and provider defaults are nowhere near saturated. Proves the
    manual in/out axis is honoured in rotation."""
    from sqlalchemy import insert as sql_insert
    from sqlalchemy import update

    from aibroker.db.models import UsageLogRow
    await _add_key("gemini", "cold", scopes=["llm:chat"])
    hot = await _add_key("gemini", "hot", scopes=["llm:chat"],
                          manual_tok_out_limit=80_000)
    # 'hot' used 76k output today = 95% of its 80k manual out-cap.
    # tokens_in tiny, total tiny — only the OUT axis trips.
    async with get_session() as s:
        await s.execute(sql_insert(UsageLogRow), [{
            "api_key_id": hot, "provider": "gemini", "tokens_in": 100,
            "tokens_out": 76_000, "cost_usd": 0.0, "status": "ok",
        }])
    invalidate_saturation_cache()
    cold_count = hot_count = 0
    for _ in range(20):
        picked = await pick_and_reserve("gemini", "llm:chat")
        assert picked is not None
        if picked.label == "cold":
            cold_count += 1
        else:
            hot_count += 1
        async with get_session() as s:
            await s.execute(update(ApiKeyRow).values(last_used_at=None))
    assert hot_count == 0, f"out-saturated key still picked {hot_count} times"
    assert cold_count == 20


async def test_pick_falls_back_to_saturated_when_all_saturated():
    """When every alive peer is over-quota, picker still returns one
    (it's a soft-sort, not a hard exclude — better a maybe-throttled call
    than no call)."""
    from sqlalchemy import insert as sql_insert

    from aibroker.db.models import UsageLogRow
    a = await _add_key("cerebras", "a")
    b = await _add_key("cerebras", "b")
    async with get_session() as s:
        await s.execute(sql_insert(UsageLogRow), [
            {"api_key_id": a, "provider": "cerebras", "tokens_in": 2_000_000,
             "tokens_out": 0, "cost_usd": 0.0, "status": "ok"},
            {"api_key_id": b, "provider": "cerebras", "tokens_in": 2_000_000,
             "tokens_out": 0, "cost_usd": 0.0, "status": "ok"},
        ])
    invalidate_saturation_cache()
    picked = await pick_and_reserve("cerebras", "llm:chat")
    assert picked is not None
    assert picked.label in ("a", "b")


async def test_pick_skips_inactive():
    await _add_key("cerebras", "x", is_active=False)
    result = await pick_and_reserve("cerebras", "llm:chat")
    assert result is None


async def test_pick_skips_dead():
    await _add_key("cerebras", "x", is_alive=False)
    result = await pick_and_reserve("cerebras", "llm:chat")
    assert result is None


async def test_pick_skips_in_cooldown():
    future = datetime.now(UTC) + timedelta(minutes=10)
    await _add_key("cerebras", "x", cooldown_until=future.replace(tzinfo=None))
    result = await pick_and_reserve("cerebras", "llm:chat")
    assert result is None


async def test_pick_skips_capped():
    # daily_reset_at=today: the selector's reset-aware read treats a non-today
    # counter as 0, so "capped" only means anything if the spend is today's.
    await _add_key("cerebras", "x", tier="paid",
                    daily_cost_cap_usd=1.0, daily_cost_used_usd=1.0,
                    daily_reset_at=date.today())
    result = await pick_and_reserve("cerebras", "llm:chat")
    assert result is None


async def test_pick_ignores_stale_cap_from_yesterday():
    """A key that hit its cap YESTERDAY must be selectable today — the
    reset-aware read treats a non-today daily_reset_at as counter=0."""
    await _add_key("cerebras", "x", tier="paid",
                    daily_cost_cap_usd=1.0, daily_cost_used_usd=1.0,
                    daily_reset_at=date.today() - timedelta(days=1))
    result = await pick_and_reserve("cerebras", "llm:chat")
    assert result is not None


async def test_pick_filters_by_scope():
    await _add_key("voyage", "x", scopes=["llm:embed"])
    result = await pick_and_reserve("voyage", "llm:chat")
    assert result is None   # wrong scope
    result = await pick_and_reserve("voyage", "llm:embed")
    assert result is not None


async def test_mark_cooldown_sets_future():
    kid = await _add_key("cerebras", "x")
    future = datetime.now(UTC) + timedelta(minutes=5)
    await mark_cooldown(kid, future)
    # Subsequent pick should skip it
    result = await pick_and_reserve("cerebras", "llm:chat")
    assert result is None


async def test_mark_dead_skips_subsequent_picks():
    kid = await _add_key("cerebras", "x")
    await mark_dead(kid)
    result = await pick_and_reserve("cerebras", "llm:chat")
    assert result is None


async def test_mark_dead_persists_reason():
    """2026-07-05: the dashboard used to show only 'мёртв' with no way to
    tell 'no money' from 'auth failed' apart — mark_dead now stores a short
    human reason on the key row."""
    kid = await _add_key("anthropic", "x")
    await mark_dead(kid, reason="Your credit balance is too low to access the API")
    async with get_session() as s:
        row = await s.get(ApiKeyRow, kid)
    assert row.last_error == "Your credit balance is too low to access the API"


async def test_mark_dead_truncates_long_reason():
    kid = await _add_key("anthropic", "x")
    await mark_dead(kid, reason="x" * 500)
    async with get_session() as s:
        row = await s.get(ApiKeyRow, kid)
    assert len(row.last_error) == 200


async def test_mark_cooldown_persists_reason():
    kid = await _add_key("deepseek", "x")
    future = datetime.now(UTC) + timedelta(minutes=5)
    await mark_cooldown(kid, future, reason="This response_format type is unavailable now")
    async with get_session() as s:
        row = await s.get(ApiKeyRow, kid)
    assert row.last_error == "This response_format type is unavailable now"
    assert row.cooldown_until is not None


async def test_reserve_key_picked_only_when_shared_exhausted():
    """Reserve key is the safety net: shared edit keys go first; the reserve
    is picked only once every shared key in the group is unavailable."""
    shared = await _add_key("gemini", "shared", scopes=["llm:chat", "llm:edit"],
                            last_used_at=datetime.now() - timedelta(hours=1))
    await _add_key("gemini", "reserve", scopes=["llm:edit"], is_reserve=True,
                   last_used_at=datetime.now() - timedelta(hours=5))  # older, but reserve

    # Even though the reserve key is older (LRU would prefer it), the shared key wins.
    picked = await pick_and_reserve("gemini", "llm:edit")
    assert picked is not None
    assert picked.label == "shared"

    # Knock the shared key into cooldown → now the reserve is used.
    await mark_cooldown(shared, datetime.now(UTC) + timedelta(minutes=10))
    picked = await pick_and_reserve("gemini", "llm:edit")
    assert picked is not None
    assert picked.label == "reserve"


async def test_pick_affinity_sticks_to_noted_key():
    """After _note_affinity, every pick for that (project, provider) returns
    the pinned key — the provider-side prompt cache stays on one account
    instead of random() fragmenting it across the pool."""
    pinned = await _add_key("deepseek", "pinned")
    await _add_key("deepseek", "other")
    _note_affinity(1, "deepseek", pinned)
    for _ in range(10):
        picked = await pick_and_reserve("deepseek", "llm:chat", project_id=1)
        assert picked is not None
        assert picked.id == pinned


async def test_pick_affinity_scoped_to_project():
    """A DIFFERENT project's pick is unaffected by project 1's affinity —
    both keys stay reachable for it (affinity is per (project, provider))."""
    from sqlalchemy import update
    pinned = await _add_key("deepseek", "pinned")
    await _add_key("deepseek", "other")
    _note_affinity(1, "deepseek", pinned)
    seen: set[int] = set()
    for _ in range(50):
        picked = await pick_and_reserve("deepseek", "llm:chat", project_id=2)
        assert picked is not None
        seen.add(picked.id)
        async with get_session() as s:
            await s.execute(update(ApiKeyRow).values(last_used_at=None))
    assert len(seen) == 2, "project 2 should still rotate over both keys"


async def test_pick_affinity_never_resurrects_cooldown_key():
    """Affinity is a tie-break among ELIGIBLE keys only — a pinned key in
    cooldown must not be picked; the healthy peer serves instead."""
    pinned = await _add_key("deepseek", "pinned")
    other = await _add_key("deepseek", "other")
    _note_affinity(1, "deepseek", pinned)
    await mark_cooldown(pinned, datetime.now(UTC) + timedelta(minutes=10))
    picked = await pick_and_reserve("deepseek", "llm:chat", project_id=1)
    assert picked is not None
    assert picked.id == other


async def test_reserve_edit_key_invisible_to_chat_scope():
    """A key scoped only to llm:edit must never serve bot llm:chat traffic."""
    await _add_key("gemini", "reserve", scopes=["llm:edit"], is_reserve=True)
    assert await pick_and_reserve("gemini", "llm:chat") is None
    assert await pick_and_reserve("gemini", "llm:edit") is not None


async def test_record_usage_increments_counters():
    kid = await _add_key("cerebras", "x", tier="free")
    await record_usage(
        api_key_id=kid, project_id=None, lease_id=None,
        provider="cerebras", model="gpt-oss-120b",
        capability="chat:fast", workflow="test",
        tokens_in=100, tokens_out=50, cost_usd=0.01,
        latency_ms=200, status="ok", error_kind=None, http_status=200,
    )
    async with get_session() as s:
        row = await s.get(ApiKeyRow, kid)
    assert row.daily_used == 1
    assert abs(row.daily_cost_used_usd - 0.01) < 1e-9
    assert abs(row.total_cost_usd - 0.01) < 1e-9


async def test_pick_hydrates_account_id():
    """REGRESSION (2026-07-11): pick_and_reserve does RETURNING * but the
    ApiKeyRow hydration dropped account_id, so cloudflare's account-scoped
    api_base was never built → every cloudflare call 'Missing CLOUDFLARE_ACCOUNT_ID'
    (295 errors, 0 ok). account_id must survive selection."""
    await _add_key("cloudflare", "cf", scopes=["llm:vision"],
                   account_id="865824c3e1d2ced02b16adb355616363")
    key = await pick_and_reserve("cloudflare", "llm:vision")
    assert key is not None
    assert key.account_id == "865824c3e1d2ced02b16adb355616363"


async def test_record_usage_ok_clears_stale_error_and_cooldown():
    """REGRESSION (2026-07-11): a rate-limited key that recovered kept showing
    'жив' + a phantom last_error until the next monitor probe (up to 10 min). A
    successful call must wipe last_error/error_count/cooldown_until on the spot."""
    future = datetime.now(UTC) + timedelta(minutes=10)
    kid = await _add_key("openrouter", "x", tier="free")
    await mark_cooldown(kid, future, reason="rate limit")
    async with get_session() as s:
        row = await s.get(ApiKeyRow, kid)
        assert row.last_error == "rate limit" and row.cooldown_until is not None
    await record_usage(
        api_key_id=kid, project_id=None, lease_id=None,
        provider="openrouter", model="google/gemma-2-9b-it:free",
        capability="chat:fast", workflow="test",
        tokens_in=10, tokens_out=5, cost_usd=0.0,
        latency_ms=100, status="ok", error_kind=None, http_status=200,
    )
    async with get_session() as s:
        row = await s.get(ApiKeyRow, kid)
    assert row.last_error is None
    assert row.error_count == 0
    assert row.cooldown_until is None


async def test_record_usage_error_keeps_failure_state():
    """A NON-ok row (retry logged its own failure) must NOT clear the error
    state — only a genuine success proves the key healthy."""
    future = datetime.now(UTC) + timedelta(minutes=10)
    kid = await _add_key("openrouter", "y", tier="free")
    await mark_cooldown(kid, future, reason="rate limit")
    await record_usage(
        api_key_id=kid, project_id=None, lease_id=None,
        provider="openrouter", model="google/gemma-2-9b-it:free",
        capability="chat:fast", workflow="test",
        tokens_in=0, tokens_out=0, cost_usd=0.0,
        latency_ms=100, status="error", error_kind="RateLimit", http_status=429,
    )
    async with get_session() as s:
        row = await s.get(ApiKeyRow, kid)
    assert row.last_error == "rate limit"
    assert row.cooldown_until is not None


async def test_record_usage_returns_new_row_id():
    """The returned id is the broker-side request_id — threaded through the
    outcome dataclasses to the caller's API response (Stepan/Vera correlate
    their own logs against it, and it's the dashboard's 'req id' column)."""
    from sqlalchemy import select

    from aibroker.db.models import UsageLogRow

    kid = await _add_key("cerebras", "y", tier="free")
    usage_id = await record_usage(
        api_key_id=kid, project_id=None, lease_id=None,
        provider="cerebras", model="gpt-oss-120b",
        capability="chat:fast", workflow="test",
        tokens_in=1, tokens_out=1, cost_usd=0.0,
        latency_ms=1, status="ok", error_kind=None, http_status=200,
    )
    assert isinstance(usage_id, int)
    async with get_session() as s:
        row = (await s.execute(
            select(UsageLogRow).where(UsageLogRow.id == usage_id)
        )).scalar_one()
    assert row.api_key_id == kid  # returned id really is this row's PK


async def test_record_usage_persists_cache_tokens():
    """cache_read_tokens/cache_write_tokens must land in usage_log — the
    dashboard's cache KPI card sums straight from this column."""
    from sqlalchemy import select

    from aibroker.db.models import UsageLogRow

    kid = await _add_key("anthropic", "x", tier="paid")
    await record_usage(
        api_key_id=kid, project_id=None, lease_id=None,
        provider="anthropic", model="anthropic/claude-sonnet-5",
        capability="chat:smart", workflow="test",
        tokens_in=10_000, tokens_out=500, cost_usd=0.05,
        cache_read_tokens=9_000, cache_write_tokens=0,
        latency_ms=200, status="ok", error_kind=None, http_status=200,
    )
    async with get_session() as s:
        row = (await s.execute(
            select(UsageLogRow).where(UsageLogRow.api_key_id == kid)
        )).scalar_one()
    assert row.cache_read_tokens == 9_000
    assert row.cache_write_tokens == 0


async def test_record_usage_cache_tokens_default_to_zero():
    """Non-caching call sites (embed, transcribe) don't
    pass cache_read_tokens/cache_write_tokens — must default to 0, not NULL."""
    from sqlalchemy import select

    from aibroker.db.models import UsageLogRow

    kid = await _add_key("voyage", "x", tier="free")
    await record_usage(
        api_key_id=kid, project_id=None, lease_id=None,
        provider="voyage", model="voyage/voyage-3",
        capability="embedding", workflow=None,
        tokens_in=500, tokens_out=0, cost_usd=0.0,
        latency_ms=100, status="ok", error_kind=None, http_status=200,
    )
    async with get_session() as s:
        row = (await s.execute(
            select(UsageLogRow).where(UsageLogRow.api_key_id == kid)
        )).scalar_one()
    assert row.cache_read_tokens == 0
    assert row.cache_write_tokens == 0


async def test_pick_and_reserve_concurrent_no_double_allocation():
    """FOR UPDATE SKIP LOCKED is this module's whole reason to exist, yet it
    was never exercised under contention — a locking regression would ship
    green. Under N concurrent picks against a pool of 2 keys the CORRECT
    behaviour is: callers that catch every row locked get None (SKIP, never
    block/deadlock — observed live in CI: 5 winners of 12), every winner is
    from the pool, and nothing raises. A regression shows up as a deadlock
    (gather raises), an out-of-pool row, or a permanently stuck lock."""
    import asyncio

    k1 = await _add_key("cerebras", "c1")
    k2 = await _add_key("cerebras", "c2")

    async def one_pick() -> int | None:
        k = await pick_and_reserve("cerebras", "llm:chat")
        return k.id if k else None

    got = await asyncio.gather(*[one_pick() for _ in range(12)])
    winners = [g for g in got if g is not None]
    assert winners, "contention must not starve every caller"
    assert set(winners) <= {k1, k2}
    # Contention gone → a pick must succeed again (no lock left behind).
    assert await pick_and_reserve("cerebras", "llm:chat") is not None
