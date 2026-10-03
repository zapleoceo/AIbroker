"""View-models for the admin pages: raw rows in, template-ready dicts out.

Pure functions (no DB, no request): the page routes fetch, these shape, the
templates render. Keeping them pure is what lets the tests exercise the
attention rules, the KPI deltas and the model catalogue without a database.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from typing import Any

from aibroker.providers.catalog import price_info
from aibroker.providers.quotas import axes_for_key, severity_class
from aibroker.providers.registry import all_models, get_spec, provider_names, spec_or_default
from aibroker.routes.dashboard_labels import KeyStatus, key_status, reason_labels
from aibroker.routing.chains import CAPABILITY_CHAINS
from aibroker.web import format as fmt

NEAR_CAP_PCT = 85
PROJECT_NEAR_CAP = 0.8
ERR_SPIKE_MIN = 5
ERR_SPIKE_RATE = 0.3


def pct_change(cur: float | None, prev: float | None) -> float | None:
    if cur is None or prev is None or prev == 0:
        return None
    return (cur - prev) / abs(prev) * 100.0


def _fill(series: Sequence[float | None]) -> list[float]:
    """Forward-fill gaps (a ratio is undefined in an empty bucket) so the
    sparkline stays continuous instead of dropping to zero."""
    first = next((v for v in series if v is not None), 0.0)
    out: list[float] = []
    last = first
    for v in series:
        last = v if v is not None else last
        out.append(float(last))
    return out


# ─── keys ───────────────────────────────────────────────────────────────────


_AXIS_LABEL = {
    "requests": ("requests/day", "запросов/день"), "tokens": ("tokens/day", "токенов/день"),
    "input": ("input/day", "вх. токенов/день"), "output": ("output/day", "исх. токенов/день"),
}
_QUOTA_SRC = {"manual": ("manual", "вручную"), "discovered": ("discovered", "из заголовков"),
              "default est.": ("default estimate", "оценка по умолчанию")}


def build_key_rows(keys: Iterable[Any], tokens_today: Mapping[int, Mapping[str, int]],
                   activity: Mapping[int, Mapping[str, Any]], now: datetime,
                   model_cooldowns: Mapping[int, Sequence[Mapping[str, Any]]] | None = None
                   ) -> list[dict[str, Any]]:
    """One display dict per api key: derived status + reason, every quota axis
    with its severity, the $ cap bar, recent activity, and the models of this
    key that are cooling on their own (api_key_model_cooldowns)."""
    model_cooldowns = model_cooldowns or {}
    rows: list[dict[str, Any]] = []
    for k in keys:
        st: KeyStatus = key_status(k, now)
        tt = tokens_today.get(k.id, {})
        axes = axes_for_key(k.daily_used or 0, int(tt.get("tot", 0)), k,
                            toks_in=int(tt.get("tin", 0)), toks_out=int(tt.get("tout", 0)))
        for a in axes:
            a["cls"] = severity_class(a["pct"])
            a["label"] = _AXIS_LABEL.get(a["name"], (a["name"], a["name"]))
        manual = bool(k.manual_req_limit or k.manual_tok_limit
                      or k.manual_tok_in_limit or k.manual_tok_out_limit)
        cap = k.daily_cost_cap_usd
        used = float(k.daily_cost_used_usd or 0)
        cost_pct = min(100, int(used / float(cap) * 100)) if cap else None
        act = activity.get(k.id, {})
        cooldown = k.cooldown_until if (k.cooldown_until and k.cooldown_until > now) else None
        top_pct = max([a["pct"] for a in axes] + ([cost_pct] if cost_pct is not None else []),
                      default=None)
        rows.append({
            "key": k, "status": st, "reason": reason_labels(k.last_error),
            "cooldown_until": cooldown, "axes": axes,
            "quota_src": _QUOTA_SRC["manual" if manual else "discovered"
                                    if k.limits_discovered_at else "default est."],
            "cost_used": used, "cost_cap": cap, "cost_pct": cost_pct,
            "cost_cls": severity_class(cost_pct),
            "top_pct": top_pct,
            "calls": act.get("calls", 0), "errs": act.get("errs", 0),
            "last_ok": act.get("last_ok"), "last_err": act.get("last_err"),
            "cooling_models": [c for c in model_cooldowns.get(k.id, ())
                               if c.get("until") and c["until"] > now],
        })
    return rows


def group_by_provider(rows: Iterable[dict[str, Any]],
                      activity_1h: Mapping[str, Mapping[str, Any]] | None = None
                      ) -> list[dict[str, Any]]:
    """Keys grouped under their provider with a roll-up (alive/cooling/dead
    counts, last-hour errors) — the card header on the providers page and the
    provider health grid on the overview."""
    activity_1h = activity_1h or {}
    groups: dict[str, dict[str, Any]] = {}
    for r in rows:
        k = r["key"]
        g = groups.setdefault(k.provider, {
            "provider": k.provider, "rows": [], "alive": 0, "cooling": 0,
            "dead": 0, "disabled": 0, "total": 0,
        })
        g["rows"].append(r)
        g["total"] += 1
        code = r["status"].code
        if code == "alive":
            g["alive"] += 1
        elif code in ("capped", "cooldown"):
            g["cooling"] += 1
        elif code == "disabled":
            g["disabled"] += 1
        else:
            g["dead"] += 1
    for name, g in groups.items():
        act = activity_1h.get(name, {})
        g["calls_1h"] = int(act.get("calls", 0))
        g["errs_1h"] = int(act.get("errs", 0))
        g["err_rate"] = (g["errs_1h"] / g["calls_1h"]) if g["calls_1h"] else 0.0
        active_n = g["total"] - g["disabled"]
        if active_n == 0:
            g["cls"] = "off"
        elif g["alive"] == 0:
            g["cls"] = "bad"
        elif g["dead"] or (g["err_rate"] >= ERR_SPIKE_RATE and g["errs_1h"] >= ERR_SPIKE_MIN):
            g["cls"] = "warn"
        else:
            g["cls"] = "ok"
    return sorted(groups.values(), key=lambda g: g["provider"])


# ─── overview ───────────────────────────────────────────────────────────────


def _delta(cur: float | None, prev: float | None, good: str | None) -> dict[str, Any] | None:
    ch = pct_change(cur, prev)
    if ch is None:
        return None
    direction = "up" if ch > 0.5 else "down" if ch < -0.5 else "flat"
    tone = "flat"
    if good and direction != "flat":
        tone = "good" if direction == good else "bad"
    return {"pct": ch, "dir": direction, "tone": tone, "text": f"{abs(ch):.0f}%"}


def build_kpis(cur: Mapping[str, Any], prev: Mapping[str, Any] | None, p95: int | None,
               p95_prev: int | None, series: Sequence[Mapping[str, Any]],
               groups: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """The six KPI tiles: value, delta vs the previous equal window, sparkline."""
    prev = prev or {}
    alive = sum(g["alive"] for g in groups)
    active = sum(g["total"] - g["disabled"] for g in groups)
    cooling = sum(g["cooling"] for g in groups)
    dead = sum(g["dead"] for g in groups)
    avg_lat = cur.get("avg_lat")
    return [
        {"id": "spend", "en": "Spend", "ru": "Расходы", "value": fmt.money(cur["spend"]),
         "sub": (f"{fmt.compact(cur['tin'])} in / {fmt.compact(cur['tout'])} out tokens",
                 f"{fmt.compact(cur['tin'])} вх / {fmt.compact(cur['tout'])} исх токенов"),
         "delta": _delta(cur["spend"], prev.get("spend"), None),
         "spark": [s["spend"] for s in series]},
        {"id": "calls", "en": "Calls", "ru": "Вызовы", "value": fmt.num(cur["calls"]),
         "sub": (f"{fmt.num(cur['err_n'])} failed", f"{fmt.num(cur['err_n'])} ошибок"),
         "delta": _delta(cur["calls"], prev.get("calls"), None),
         "spark": [s["calls"] for s in series]},
        {"id": "success", "en": "Success rate", "ru": "Успешность",
         "value": fmt.pct(cur["success"], 1) if cur["success"] is not None else "—",
         "sub": ("of all attempts", "всех попыток"),
         "delta": _delta(cur["success"], prev.get("success"), "up"),
         "spark": _fill([s["success"] for s in series])},
        {"id": "cache", "en": "Cache hit", "ru": "Кэш-хит",
         "value": fmt.pct(cur["cache_hit"], 0) if cur["cache_hit"] is not None else "—",
         "sub": (f"{fmt.compact(cur['cache_read'])} cached input tokens",
                 f"{fmt.compact(cur['cache_read'])} кэшированных вх. токенов"),
         "delta": _delta(cur["cache_hit"], prev.get("cache_hit"), "up"),
         "spark": _fill([s["cache_hit"] for s in series])},
        {"id": "p95", "en": "p95 latency", "ru": "p95 задержка", "value": fmt.ms(p95),
         "sub": (f"avg {fmt.ms(avg_lat)}", f"средняя {fmt.ms(avg_lat)}"),
         "delta": _delta(p95, p95_prev, "down"),
         "spark": _fill([s["avg_lat"] for s in series])},
        {"id": "keys", "en": "Active keys", "ru": "Активные ключи", "value": f"{alive}/{active}",
         "sub": (f"{cooling} cooling · {dead} dead", f"{cooling} на паузе · {dead} мертвы"),
         "delta": None, "spark": None,
         "bar": [("ok", alive), ("warn", cooling), ("bad", dead)]},
    ]


def build_attention(key_rows: Sequence[dict[str, Any]], groups: Sequence[Mapping[str, Any]],
                    project_cards: Sequence[Mapping[str, Any]],
                    jobs: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """What needs the owner's eyes, worst first. Each item is bilingual and
    links to the page where it can be fixed."""
    items: list[dict[str, Any]] = []

    def add(sev: str, en: str, ru: str, href: str) -> None:
        items.append({"sev": sev, "en": en, "ru": ru, "href": href})

    by_status: dict[str, dict[str, list[str]]] = {"dead": {}, "no_credits": {}}
    for r in key_rows:
        code = r["status"].code
        if code in by_status:
            by_status[code].setdefault(r["key"].provider, []).append(r["key"].label)
    for prov, labels in sorted(by_status["dead"].items()):
        shown = ", ".join(labels[:3]) + (f" +{len(labels) - 3}" if len(labels) > 3 else "")
        n = len(labels)
        add("bad", f"{prov}: {fmt.plural_en(n, 'dead key', 'dead keys')} — {shown}",
            f"{prov}: {fmt.plural_ru(n, 'мёртвый ключ', 'мёртвых ключа', 'мёртвых ключей')} ({shown})",
            f"/dashboard/providers#p-{prov}")
    for prov, labels in sorted(by_status["no_credits"].items()):
        n = len(labels)
        add("warn", f"{prov}: {fmt.plural_en(n, 'key', 'keys')} out of credits — top up",
            f"{prov}: {fmt.plural_ru(n, 'ключ', 'ключа', 'ключей')} без средств — пополните",
            f"/dashboard/providers#p-{prov}")
    for r in key_rows:
        k = r["key"]
        if r["status"].code in ("alive", "capped") and (r["top_pct"] or 0) >= NEAR_CAP_PCT:
            what = (r["axes"][0]["short"] + "/day" if r["axes"] and
                    r["axes"][0]["pct"] == r["top_pct"] else "$ cap")
            add("warn", f"{k.provider}/{k.label} at {r['top_pct']}% of its {what}",
                f"{k.provider}/{k.label}: {r['top_pct']}% лимита ({what})",
                f"/dashboard/providers#p-{k.provider}")
    for g in groups:
        if g["errs_1h"] >= ERR_SPIKE_MIN and g["err_rate"] >= ERR_SPIKE_RATE:
            sev = "bad" if g["err_rate"] >= 0.7 else "warn"
            rate = f"{g['err_rate'] * 100:.0f}%"
            e = g["errs_1h"]
            add(sev, f"{g['provider']}: {fmt.plural_en(e, 'error', 'errors')} in the last hour ({rate})",
                f"{g['provider']}: {fmt.plural_ru(e, 'ошибка', 'ошибки', 'ошибок')} за час ({rate})",
                f"/dashboard/requests?provider={g['provider']}&status=error&range=today")
    for c in project_cards:
        cap, today = c["cap"], c["today_spend"]
        if cap and today / cap >= PROJECT_NEAR_CAP:
            p = c["project"]
            add("bad" if today >= cap else "warn",
                f"{p.name}: {fmt.money(today)} of its {fmt.money(cap)} daily cap",
                f"{p.name}: {fmt.money(today)} из дневного лимита {fmt.money(cap)}",
                f"/dashboard/projects/{p.id}")
    if jobs and jobs.get("stuck_pending"):
        add("bad", "Job queue: pending jobs are waiting too long — dispatcher stuck?",
            "Очередь: задачи ждут слишком долго — завис диспетчер?",
            "/dashboard/requests?type=job&status=pending&range=all")
    if jobs and jobs.get("stuck_running"):
        add("warn", "Job queue: a job has been running for a very long time",
            "Очередь: задача выполняется слишком долго",
            "/dashboard/requests?type=job&status=running&range=all")
    order = {"bad": 0, "warn": 1, "info": 2}
    items.sort(key=lambda i: order[i["sev"]])
    return items


# ─── projects ───────────────────────────────────────────────────────────────


def request_cap_view(project: Any) -> dict[str, Any]:
    """Lifetime request allowance of a project for the dashboard: `cap` None =
    unlimited (no bar), else used/cap with a percentage and severity class."""
    cap, used = project.total_request_cap, int(project.total_requests_used or 0)
    pct = None if cap is None else (100 if cap <= 0 else min(100, int(used / cap * 100)))
    return {"cap": cap, "used": used, "pct": pct, "cls": severity_class(pct)}


def build_project_cards(projects: Iterable[Any], range_stats: Mapping[int, Mapping[str, Any]],
                        today_spend: Mapping[int, float]) -> list[dict[str, Any]]:
    cards: list[dict[str, Any]] = []
    for p in projects:
        st = range_stats.get(p.id, {})
        today = float(today_spend.get(p.id, 0.0) or 0.0)
        cap = p.daily_cost_cap_usd
        cap_pct = min(100, int(today / cap * 100)) if cap else None
        cards.append({
            "project": p, "calls": st.get("calls", 0), "spend": st.get("spend", 0.0),
            "success": st.get("success"), "cache_hit": st.get("cache_hit"),
            "spark": st.get("spark", []), "today_spend": today, "cap": cap,
            "cap_pct": cap_pct, "cap_cls": severity_class(cap_pct),
            "req": request_cap_view(p),
        })
    return cards


# ─── providers (the merged Keys + Models page) ─────────────────────────────


# Capability filter chips: the registry's lanes folded into the kinds a human
# picks between. Only groups that some registered model serves are offered.
_CAP_GROUPS: tuple[tuple[str, str, str], ...] = (
    ("chat", "Chat", "Чат"), ("structured", "Structured", "Структура"),
    ("vision", "Vision", "Зрение"), ("voice", "Voice", "Голос"),
    ("embedding", "Embeddings", "Эмбеддинги"), ("decision", "Decisions", "Решения"),
)
_CAP_GROUP_OF = {"structured": "structured", "vision": "vision", "transcription": "voice",
                 "embedding": "embedding", "decision": "decision"}


def cap_group(capability: str) -> str:
    """`chat:fast` / `prefilter` / `translate` -> chat, `transcription` -> voice…"""
    if capability.startswith("chat:") or capability in ("prefilter", "translate"):
        return "chat"
    return _CAP_GROUP_OF.get(capability, capability)


def capability_filters() -> list[dict[str, str]]:
    """Filter chips derived from the registry, in a stable human order."""
    present = {cap_group(c) for m in all_models() for c in m.capabilities}
    known = [g for g in _CAP_GROUPS if g[0] in present]
    extra = sorted(present - {g[0] for g in _CAP_GROUPS})
    return [{"key": k, "en": en, "ru": ru} for k, en, ru in known] +            [{"key": k, "en": k.capitalize(), "ru": k.capitalize()} for k in extra]


def _quota_burn(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any] | None:
    """Aggregate today's burn of a provider's enabled keys: per quota axis the
    summed used / summed cap, reported for the axis that is closest to its cap."""
    totals: dict[str, dict[str, Any]] = {}
    for r in rows:
        if r["status"].code == "disabled":
            continue
        for a in r["axes"]:
            t = totals.setdefault(a["name"], {"label": a["label"], "used": 0, "cap": 0})
            t["used"] += a["used"]
            t["cap"] += a["cap"]
    best: dict[str, Any] | None = None
    for t in totals.values():
        if not t["cap"]:
            continue
        t["pct"] = min(100, int(t["used"] / t["cap"] * 100))
        if best is None or t["pct"] > best["pct"]:
            best = t
    if best:
        best["cls"] = severity_class(best["pct"])
    return best


def _model_view(spec: Any, provider: str, observed: Mapping[tuple[str, str], Mapping[str, Any]],
                rotation_ids: frozenset[str], cooling: Mapping[str, int]) -> dict[str, Any]:
    price = price_info(spec)
    kind = price["kind"]
    pin, pout = price.get("input_usd_per_mtok"), price.get("output_usd_per_mtok")
    obs = observed.get((provider, spec.id), {})
    caps = sorted(spec.capabilities)
    groups = sorted({cap_group(c) for c in caps}, key=lambda g: next(
        (i for i, x in enumerate(_CAP_GROUPS) if x[0] == g), 99))
    routed = any(provider in CAPABILITY_CHAINS.get(c, []) for c in caps)
    labels = {k: (en, ru) for k, en, ru in _CAP_GROUPS}
    return {
        "id": spec.id, "caps": caps, "groups": groups,
        "group_labels": [labels.get(g, (g.capitalize(), g.capitalize())) for g in groups],
        "routed": routed, "rotation": spec.id in rotation_ids,
        "price_in": pin, "price_out": pout, "per_minute": price.get("usd_per_minute"),
        "free": kind in ("free", "local") or (pin == 0 and pout in (0, None)),
        "calls": obs.get("calls", 0), "success": obs.get("success"), "p50": obs.get("p50"),
        "cooling_keys": cooling.get(spec.id, 0),
    }


def build_providers(key_groups: Sequence[Mapping[str, Any]],
                    observed: Mapping[tuple[str, str], Mapping[str, Any]]) -> list[dict[str, Any]]:
    """One card per provider for the Providers page: the key roll-up from
    group_by_provider, the registry's models with price / 7d latency / success,
    and an aggregate quota burn. Order: providers with a live key first, then
    by registry rank; `inactive` marks a provider with no keys and no routed
    model (the page folds those away)."""
    by_name = {g["provider"]: g for g in key_groups}
    cards: list[dict[str, Any]] = []
    for name in dict.fromkeys([*provider_names(), *by_name]):
        spec = get_spec(name)
        g = by_name.get(name) or {
            "provider": name, "rows": [], "alive": 0, "cooling": 0, "dead": 0,
            "disabled": 0, "total": 0, "calls_1h": 0, "errs_1h": 0, "err_rate": 0.0, "cls": "off"}
        rows = g["rows"]
        cooling: dict[str, int] = {}
        for r in rows:
            for c in r.get("cooling_models", ()):
                cooling[c["model"]] = cooling.get(c["model"], 0) + 1
        rotation_ids = frozenset(m for ms in (spec.rotation.values() if spec else ()) for m in ms)
        models = [_model_view(m, name, observed, rotation_ids, cooling)
                  for m in (spec.models.values() if spec else ())]
        models.sort(key=lambda m: (not m["routed"], m["id"]))
        card = {
            **g, "paid": spec_or_default(name).paid, "rank": spec_or_default(name).rank,
            "models": models, "routed_n": sum(1 for m in models if m["routed"]),
            "burn": _quota_burn(rows), "live": g["alive"] > 0,
            "cap_groups": sorted({x for m in models for x in m["groups"]}),
        }
        card["inactive"] = not g["total"] and not card["routed_n"]
        cards.append(card)
    cards.sort(key=lambda c: (c["inactive"], not c["live"], c["rank"], c["provider"]))
    return cards
