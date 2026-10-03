"""Pure/portable logic behind the admin UI: formatters, range, labels, view-models,
portable queries (seeded SQLite/Postgres), attempt linking."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from aibroker.routes import dashboard_queries as q
from aibroker.routes import dashboard_views as v
from aibroker.routes.dashboard_labels import _friendly_call_error, _friendly_reason, key_status
from aibroker.routes.dashboard_range import resolve_range
from aibroker.routes.dashboard_time import today_in
from aibroker.web import format as fmt
from tests import dash_seed as ds

# ─── formatters ─────────────────────────────────────────────────────────────


def test_formatters():
    assert fmt.money(0.00042) == "$0.0004" and fmt.money(12.3456) == "$12.35" and fmt.money(None) == "—"
    assert fmt.compact(1_250_000) == "1.2M" and fmt.compact(999) == "999" and fmt.compact(3000) == "3k"
    assert fmt.ms(340) == "340 ms" and fmt.ms(1250) == "1.2 s" and fmt.ms(125_000) == "2m 05s"
    assert fmt.ms_ru(340) == "340 мс" and fmt.ms_ru(1250) == "1,2 с" and fmt.ms_ru(60_000) == "1 мин"
    assert fmt.ms_ru(125_000) == "2 мин 05 с" and fmt.ms_ru(None) == "—"
    t0 = datetime(2026, 1, 1, 12, 0)
    assert fmt.age(t0 - timedelta(minutes=36), t0) == "36m" and fmt.age(None) == "—"
    assert fmt.age(t0 - timedelta(minutes=36), t0, ru=True) == "36 мин"
    assert fmt.age(t0 - timedelta(seconds=9), t0, ru=True) == "9 с"
    assert fmt.age(t0 - timedelta(hours=5), t0, ru=True) == "5 ч"
    assert fmt.age(t0 - timedelta(days=3), t0, ru=True) == "3 д"
    assert fmt.pct(79.94, 1) == "79.9%" and fmt.num(1234) == "1,234" and fmt.num(None) == "—"
    now = datetime(2026, 1, 1, 12, 0)
    assert fmt.ago(now - timedelta(minutes=5), now) == "5m ago" and fmt.ago(None) == "—"


def test_time_tag_is_utc_fallback_with_iso_datetime():
    html = str(fmt.time_tag(datetime(2026, 1, 2, 3, 4, 5), "mdhm"))
    assert 'datetime="2026-01-02T03:04:05Z"' in html and ">01-02 03:04<" in html


def test_spark_is_unitless_and_flat_series_safe():
    assert 'preserveAspectRatio="none"' in str(fmt.spark([1, 3, 2]))
    assert "width=" not in str(fmt.spark([1, 3, 2]))
    assert "flat" in str(fmt.spark([])) and "polyline" in str(fmt.spark([5, 5, 5]))


# ─── range ──────────────────────────────────────────────────────────────────


def test_range_presets_default_and_custom_normalising():
    assert resolve_range({}).key == "7d"
    assert resolve_range({"range": "bogus"}).key == "7d"
    assert resolve_range({"range": "all"}).start is None
    t = today_in(resolve_range({}).tz)
    assert resolve_range({"from": t.isoformat(), "to": t.isoformat()}).key == "today"
    c = resolve_range({"from": "2026-01-01", "to": "2026-01-03"})
    assert c.key == "custom" and c.qs == "from=2026-01-01&to=2026-01-03"
    assert c.previous()[1] == c.start and c.bucket == "day"
    assert resolve_range({"range": "today"}).bucket == "hour"
    assert resolve_range({"range": "all"}).previous() is None


# ─── labels ─────────────────────────────────────────────────────────────────


def test_friendly_labels():
    assert _friendly_reason("Your credit balance is too low") == ("top up balance", "пополнить баланс")
    assert _friendly_reason("never seen before") is None
    assert _friendly_call_error(429, "TimeoutError") == ("timeout", "таймаут", "warn")
    assert _friendly_call_error(401, "x") == ("auth failed", "ошибка авторизации", "bad")
    assert _friendly_call_error(200, None) is None


def _k(**kw):
    base = {"is_active": True, "is_alive": True, "cooldown_until": None, "last_error": None,
                "daily_reset_at": None, "daily_cost_cap_usd": None, "daily_cost_used_usd": 0.0,
                "daily_limit": 999_999, "daily_used": 0}
    base.update(kw)
    return SimpleNamespace(**base)


def test_key_status_precedence():
    now = datetime(2026, 5, 5, 12, 0)
    assert key_status(_k(), now).code == "alive"
    assert key_status(_k(is_active=False), now).code == "disabled"
    assert key_status(_k(is_alive=False, last_error="auth failed"), now).code == "dead"
    assert key_status(_k(is_alive=False, last_error="credit balance is too low"), now).code == "no_credits"
    assert key_status(_k(cooldown_until=now + timedelta(minutes=1)), now).code == "cooldown"
    capped = _k(daily_reset_at=now.date(), daily_cost_cap_usd=1.0, daily_cost_used_usd=1.0)
    assert key_status(capped, now).code == "capped"
    stale = _k(daily_reset_at=now.date() - timedelta(days=1), daily_cost_cap_usd=1.0, daily_cost_used_usd=1.0)
    assert key_status(stale, now).code == "alive"          # stale counter reads 0
    by_req = _k(daily_reset_at=now.date(), daily_limit=10, daily_used=10)
    assert key_status(by_req, now).code == "capped"


# ─── view-models ────────────────────────────────────────────────────────────


def test_pct_change_and_kpi_deltas():
    assert v.pct_change(110, 100) == 10 and v.pct_change(1, 0) is None and v.pct_change(None, 3) is None
    cur = {"spend": 2.0, "calls": 10, "err_n": 1, "tin": 100, "tout": 50, "success": 90.0, "cache_hit": 40.0,
               "cache_read": 40, "avg_lat": 500.0}
    prev = {"spend": 1.0, "calls": 10, "success": 95.0, "cache_hit": 20.0}
    series = [{"spend": 1, "calls": 5, "success": None, "cache_hit": None, "avg_lat": None},
              {"spend": 2, "calls": 5, "success": 90.0, "cache_hit": 40.0, "avg_lat": 500.0}]
    ks = {k["id"]: k for k in v.build_kpis(cur, prev, 900, 1000, series, [])}
    assert list(ks) == ["spend", "calls", "success", "cache", "p95", "keys"]
    assert ks["success"]["delta"]["tone"] == "bad"          # success fell: bad
    assert ks["cache"]["delta"]["tone"] == "good" and ks["p95"]["delta"]["tone"] == "good"
    assert ks["spend"]["delta"]["tone"] == "flat"           # spend is neutral
    assert ks["success"]["spark"] == [90.0, 90.0]           # gaps forward-filled


def _row(provider, label, code, top=None, axes=None):
    k = SimpleNamespace(provider=provider, label=label)
    return {"key": k, "status": SimpleNamespace(code=code), "top_pct": top, "axes": axes or []}


def test_attention_rules_and_order():
    rows = [_row("gemini", "a", "dead"), _row("groq", "b", "no_credits"),
            _row("cerebras", "c", "alive", 91, [{"short": "tok", "pct": 91}])]
    groups = [{"provider": "groq", "errs_1h": 20, "err_rate": 0.8}, {"provider": "x", "errs_1h": 2, "err_rate": 1.0}]
    cards = [{"project": SimpleNamespace(id=1, name="p"), "cap": 1.0, "today_spend": 0.9}]
    items = v.build_attention(rows, groups, cards, {"stuck_pending": True})
    text = " | ".join(i["en"] for i in items)
    assert "dead key" in text and "out of credits" in text and "91%" in text
    assert "20 errors" in text and "x:" not in text         # below the spike floor
    assert "p:" in text and "dispatcher" in text
    assert [i["sev"] for i in items] == sorted((i["sev"] for i in items), key=["bad", "warn", "info"].index)
    assert v.build_attention([], [], [], None) == []


def test_group_by_provider_classes():
    now = datetime(2026, 5, 5)
    rows = [_row("a", "1", "alive"), _row("a", "2", "dead"), _row("b", "1", "disabled"),
            _row("c", "1", "cooldown")]
    g = {x["provider"]: x for x in v.group_by_provider(rows, {"a": {"calls": 10, "errs": 9}})}
    assert g["a"]["cls"] == "warn" and g["b"]["cls"] == "off" and g["c"]["cls"] == "bad"
    assert g["a"]["alive"] == 1 and g["a"]["dead"] == 1 and now


def test_model_catalogue_flags_unrouted_prices_and_observed_stats():
    rows = v.build_model_catalogue({("deepseek", "deepseek/deepseek-flash"): {"calls": 4, "success": 75.0, "p50": 900}})
    ds_row = next(r for r in rows if r["model"] == "deepseek/deepseek-flash" and r["capability"] == "chat:smart")
    assert ds_row["paid"] and ds_row["p50"] == 900 and round(ds_row["price_in"], 2) == 0.15
    assert not any(r["capability"] not in __import__("aibroker.routing.chains", fromlist=["x"]).CAPABILITY_CHAINS for r in rows)
    mistral = [r for r in rows if r["provider"] == "mistral"]
    assert mistral and not any(r["routed"] for r in mistral)


# ─── portable queries ───────────────────────────────────────────────────────


def test_range_totals_and_percentile_and_series():
    ds.seed(ds.project(1), ds.key(1))
    ds.seed(*[ds.usage(i, minutes_ago=5 + i, latency_ms=100 * i, cost=0.01, cache_read=10,
                       tokens_in=100) for i in range(1, 11)],
            ds.usage(20, status="error", error_kind="X", http_status=500, latency_ms=None))
    start = ds.now() - timedelta(hours=2)
    t = ds.run(q.range_totals(start, None))
    assert t["calls"] == 11 and t["ok_n"] == 10 and t["err_n"] == 1
    assert round(t["success"], 1) == 90.9 and round(t["cache_hit"], 1) == 9.1 and round(t["spend"], 2) == 0.1
    assert ds.run(q.latency_percentile(start, None, 0.95)) == 1000
    assert ds.run(q.latency_percentile(start, None, 0.5)) == 500
    assert ds.run(q.range_totals(start, None, project_id=99))["calls"] == 0
    series = ds.run(q.time_series(start, ds.now() + timedelta(hours=1), "hour"))
    assert sum(s["calls"] for s in series) == 11 and len(series) >= 3
    assert any(s["calls"] == 0 for s in series) or len(series) == 3


def test_usage_by_model_series_top_n_and_other():
    ds.seed(ds.project(1), ds.key(1))
    ds.seed(*[ds.usage(i, model=f"m{i}", minutes_ago=3, cost=0.01 * i) for i in range(1, 5)])
    out = ds.run(q.usage_by_model_series(ds.now() - timedelta(hours=3), ds.now() + timedelta(hours=1), "hour", top=2))
    assert out["spend"]["names"] == ["m4", "m3", "other"]
    assert sum(sum(s) for s in out["calls"]["series"]) == 4
    assert len(out["buckets"]) == len(out["spend"]["series"][0])


def test_project_stats_breakdown_activity_models_jobs_audit():
    ds.seed_demo()
    start = ds.now() - timedelta(days=1)
    stats = ds.run(q.project_range_stats(start, None, "hour"))
    assert stats[1]["calls"] == 5 and stats[2]["calls"] == 1 and sum(stats[1]["spark"]) == 5
    d = ds.run(q.project_breakdown(1, start, None, "hour"))
    assert d["totals"]["calls"] == 5 and len(d["lat_hist"]) == 8 and sum(n for _, n in d["lat_hist"]) == 5
    assert {r["k"] for r in d["by_provider"]} >= {"gemini", "groq"} and d["recent"] and d["used_keys"]
    act = ds.run(q.key_activity(start))
    assert act[3]["errs"] == 2 and act[1]["last_ok"] is not None
    obs = ds.run(q.observed_model_stats(start))
    assert obs[("deepseek", "deepseek/deepseek-flash")]["p50"] == 800
    assert ds.run(q.provider_activity(ds.now() - timedelta(hours=1)))["groq"]["errs"] == 2
    jobs = ds.run(q.job_overview())
    assert jobs["by_status"]["pending"] == 1 and jobs["stuck_pending"] and not jobs["stuck_running"]
    assert jobs["failed_24h"] == 1
    rows, more = ds.run(q.audit_page(before_id=None, actor=None, action="key."))
    assert {r["action"] for r in rows} == {"key.added", "key.delete"} and not more
    assert ds.run(q.audit_page(before_id=None, actor=None, action="%"))[0] == []   # LIKE escaped


def test_bucket_helpers():
    s = datetime(2026, 1, 1, 10, 30)
    assert q.floor_bucket(s, "hour") == datetime(2026, 1, 1, 10) and q.floor_bucket(s, "day").hour == 0
    assert len(q.bucket_starts(s, s + timedelta(hours=3), "hour")) == 4
    assert datetime.now(UTC) and len(q.bucket_starts(s, s + timedelta(days=2), "day")) == 3
