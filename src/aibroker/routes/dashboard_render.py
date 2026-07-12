"""Dashboard render layer — turns the data-layer dicts into the admin HTML
(the main dashboard and the per-project drill-down), plus the small
presentation helpers they use: the page shell, the provider-catalogue
that drives the add-key form, and the friendly-error translation.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from html import escape as esc
from typing import Any
from zoneinfo import ZoneInfo

from fastapi.responses import HTMLResponse

from aibroker import __version__
from aibroker.config import get_settings
from aibroker.providers.litellm_adapter import DEFAULT_MODEL
from aibroker.providers.quotas import axes_for_key, severity_class
from aibroker.routes.dashboard_assets import _NO_STORE
from aibroker.routes.dashboard_data import _LAT_LABELS, _RANGE_HOURS, _SPARK_BUCKETS
from aibroker.routes.dashboard_scopes import _scope_checkboxes
from aibroker.routes.dashboard_time import UTC_TZ, today_in

# ─── Provider catalogue (drives add-key form dropdown) ──────────────────────


def _provider_catalogue() -> list[dict[str, Any]]:
    """One entry per known provider: name, capabilities, default scope.

    Sorted by free-first usefulness: cerebras/groq/gemini/openrouter/deepseek
    first (free or cheap), then paid (openai/anthropic), then voyage (embed-only).
    """
    # capability → scope mapping; voyage embeddings → llm:embed, everything else → llm:chat
    def scope_for(caps: list[str]) -> str:
        if "embedding" in caps:
            return "llm:embed"
        if "vision" in caps and len(caps) == 1:
            return "llm:vision"
        if "chat:deep" in caps and len(caps) == 1:
            return "llm:deep"
        return "llm:chat"

    order = ["cerebras", "groq", "gemini", "mistral", "cohere",
             "openrouter", "deepseek",
             "openai", "anthropic", "voyage",
             "sambanova", "nvidia", "cloudflare", "zai"]
    out = []
    for p in order:
        caps = list(DEFAULT_MODEL.get(p, {}).keys())
        if not caps:
            continue
        out.append({
            "provider": p,
            "capabilities": caps,
            "default_scope": scope_for(caps),
            "models": DEFAULT_MODEL[p],
        })
    return out


def _provider_meta_json() -> str:
    """Compact JSON for the in-page <script> — what JS reads on provider change."""
    import json
    return json.dumps({p["provider"]: p for p in _provider_catalogue()},
                       separators=(",", ":"))


def _dash_html(*, body: str, flash: str = "") -> str:
    return f"""<!doctype html><html><head>
<meta charset="utf-8"><title>AIbroker</title>
<link rel="icon" type="image/svg+xml" href="/favicon.svg">
<link rel="alternate icon" href="/favicon.ico">
<link rel="stylesheet" href="/dashboard/assets.css?v={__version__}">
</head><body>

<nav>
  <h1>AIbroker</h1>
  <span class="pill">v{__version__}</span>
  <span class="right">
    <span class="lang-toggle">
      <button type="button" data-lang="en" class="active">EN</button>
      <button type="button" data-lang="ru">RU</button>
    </span>
    <a href="/v1/health">/v1/health</a>
    <a href="/docs">/docs</a>
    <a href="/logout" data-i18n data-en="logout" data-ru="выйти">logout</a>
  </span>
</nav>

{('<div class="flash">' + esc(flash) + '</div>') if flash and not flash.startswith('!') else ''}
{('<div class="flash err">' + esc(flash[1:]) + '</div>') if flash.startswith('!') else ''}

{body}

<script src="/dashboard/assets.js?v={__version__}"></script>

</body></html>"""


# Known failure signatures → a short actionable label instead of raw
# exception text. 2026-07-05: "мёртв" + a raw litellm dump didn't tell the
# operator what to actually DO — "credit balance is too low" buried in JSON
# reads as generic breakage, not "go add money". Order matters (first match
# wins); nothing here duplicates classify_provider_error's signs — this is
# purely a display-layer translation, not a routing decision.
# Billing-exhaustion signs — a VALID key that's just out of money (not a broken
# credential). Kept as their own group so the status can render "нет средств"
# (needs top-up, recovers on top-up) instead of the alarming "мёртв".
_TOP_UP_SIGNS: tuple[str, ...] = (
    "credit balance is too low",
    "prepayment credits are depleted",
    "credits are depleted",
    "insufficient",
    "payment required",
    "no funds",
)
# Rate-limit / quota signs — a healthy key that's just throttled. Every
# provider phrases it differently and litellm often prepends its own class name,
# so raw last_error ranges from a tidy "rate limit" to a multi-line
# "litellm.RateLimitError: geminiException - {..json..}" dump. Collapse them all
# to one clean label. "monthly quota" is listed FIRST (before the generic
# "quota") so Mistral's monthly-ceiling reads as its own thing, not a transient
# throttle. First match wins.
_RATE_LIMIT_DISPLAY_SIGNS: tuple[str, ...] = (
    "rate limit", "ratelimit", "too many requests", "429",
    "resource_exhausted", "resource has been exhausted",
)
_FRIENDLY_REASONS: tuple[tuple[str, str, str], ...] = (
    *((s, "top up balance", "пополнить баланс") for s in _TOP_UP_SIGNS),
    ("monthly quota", "monthly quota", "месячная квота"),
    *((s, "rate limited", "лимит запросов") for s in _RATE_LIMIT_DISPLAY_SIGNS),
    ("quota", "quota exceeded", "квота исчерпана"),
    ("timeout", "provider timeout", "таймаут провайдера"),
    ("response_format type is unavailable", "provider feature outage",
     "сбой фичи у провайдера"),
)


# Timestamp fields per format hint — mirrored in the dashboard JS (F map). The
# server renders the UTC fallback; the client rewrites it to the viewer's zone.
_TS_FMT: dict[str, str] = {
    "hm": "%H:%M", "mdhm": "%m-%d %H:%M", "mdhms": "%m-%d %H:%M:%S",
}


def _ts_span(dt: datetime, tf: str) -> str:
    """A viewer-localised timestamp. `dt` is naive UTC (as stored); the dashboard
    JS rewrites the text into the browser's timezone. The server-rendered text is
    the UTC fallback when JS is off; the trailing 'Z' tells `new Date` it's UTC."""
    iso = dt.replace(microsecond=0).isoformat() + "Z"
    return (f'<span class="ts" data-utc="{iso}" data-tf="{tf}">'
            f'{dt.strftime(_TS_FMT[tf])}</span>')


def _is_top_up(raw: str | None) -> bool:
    """True if the error is a billing-exhaustion (out of money) — a valid key
    that recovers on top-up, not a dead credential."""
    low = (raw or "").lower()
    return any(s in low for s in _TOP_UP_SIGNS)


def _friendly_reason(raw: str) -> tuple[str, str] | None:
    """(en, ru) short actionable label for a known raw error, else None —
    caller falls back to showing (a truncated slice of) the raw text."""
    low = raw.lower()
    for sign, en, ru in _FRIENDLY_REASONS:
        if sign in low:
            return en, ru
    return None


def _render(data: dict[str, Any], *, tz: ZoneInfo = UTC_TZ, flash: str = "",
             new_project_key: str | None = None) -> HTMLResponse:
    s = get_settings()

    df, dt = data["date_from"], data["date_to"]
    df_str = df.isoformat() if df else ""
    dt_str = dt.isoformat() if dt else ""
    all_time = df is None and dt is None
    if all_time:
        range_label_en = "all time"
        range_label_ru = "за всё время"
    elif df == dt:
        range_label_en = f"on {df_str}"
        range_label_ru = f"за {df_str}"
    else:
        range_label_en = f"{df_str or '…'} → {dt_str or '…'}"
        range_label_ru = f"{df_str or '…'} → {dt_str or '…'}"

    today_d = today_in(tz)
    today_iso = today_d.isoformat()
    d7_iso = (today_d - timedelta(days=6)).isoformat()
    d30_iso = (today_d - timedelta(days=29)).isoformat()
    # Which quick-range link (if any) matches the current from/to — drives the
    # 'active' class below. Mutually exclusive by construction (different
    # from-dates), so at most one is ever true.
    is_today = df_str == today_iso and dt_str == today_iso
    is_7d = df_str == d7_iso and dt_str == today_iso
    is_30d = df_str == d30_iso and dt_str == today_iso
    range_form = f"""
    <form method="get" action="/dashboard" class="range-form">
      <label data-i18n data-en="From" data-ru="С">From</label>
      <input type="date" name="from" value="{df_str}" max="{today_iso}">
      <label data-i18n data-en="To" data-ru="по">To</label>
      <input type="date" name="to" value="{dt_str}" max="{today_iso}">
      <button type="submit" data-i18n data-en="apply" data-ru="применить">apply</button>
      <a href="/dashboard" class="range-reset{' active' if all_time else ''}" data-i18n
         data-en="all time" data-ru="за всё">all time</a>
      <span class="range-quick">
        <a href="?from={today_iso}&to={today_iso}" class="{'active' if is_today else ''}">today</a>
        <a href="?from={d7_iso}&to={today_iso}" class="{'active' if is_7d else ''}">7d</a>
        <a href="?from={d30_iso}&to={today_iso}" class="{'active' if is_30d else ''}">30d</a>
      </span>
    </form>"""

    cards = f"""
    {range_form}
    <div class="cards">
      <div class="card">
        <div class="card-label" data-i18n
             data-en="Spend ({range_label_en})" data-ru="Потрачено ({range_label_ru})">Spend ({range_label_en})</div>
        <div class="card-value">${data['range_spend']:.4f}</div>
        <div class="card-sub"><span data-i18n data-en="global cap" data-ru="общий лимит">global cap</span> ${s.GLOBAL_DAILY_CAP_USD}/day</div>
      </div>
      <div class="card">
        <div class="card-label" data-i18n
             data-en="Calls ({range_label_en})" data-ru="Вызовов ({range_label_ru})">Calls ({range_label_en})</div>
        <div class="card-value">{data['range_calls']:,}</div>
        <div class="card-sub">{data['calls_1h']} <span data-i18n data-en="in last 1h" data-ru="за последний час">in last 1h</span></div>
      </div>
      <div class="card">
        <div class="card-label" data-i18n data-en="Tokens in / out" data-ru="Токены вх / исх">Tokens in / out</div>
        <div class="card-value" style="font-size:18px">{data['range_tin']:,} / {data['range_tout']:,}</div>
      </div>
      <div class="card">
        <div class="card-label" data-i18n data-en="Projects · keys" data-ru="Проекты · ключи">Projects · keys</div>
        <div class="card-value">{len(data['projects'])} · {len(data['keys'])}</div>
      </div>
    </div>"""

    providers_html = "".join(
        f'<span class="provider"><b>{esc(p)}</b> '
        f'<span class="ok">{a}</span> / <span class="bad">{d}</span> / {t}'
        + (f' <span class="bad" title="errors in the last hour">⚠{e1h}/1h</span>'
           if e1h else "")
        + '</span>'
        for p, a, d, t, e1h in data["provider_summary"]
    )

    show_new_key = ""
    if new_project_key:
        show_new_key = (
            f'<div class="flash">Project created. SAVE this key now '
            f'(not retrievable later):<br><code>{esc(new_project_key)}</code></div>'
        )

    proj_spend = data["proj_spend"]
    rows_projects = ""
    projects_total_cap = 0.0
    projects_total_spend = 0.0
    for p in data["projects"]:
        scopes_csv = ",".join(p.allowed_scopes)
        cap_val = p.daily_cost_cap_usd if p.daily_cost_cap_usd is not None else ""
        cap_disp = f"${p.daily_cost_cap_usd:.2f}" if p.daily_cost_cap_usd is not None else "—"
        active_cell = "✓" if p.is_active else "✗"
        active_class = "ok" if p.is_active else "bad"
        p_spend = float(proj_spend.get(p.id, 0) or 0)
        projects_total_spend += p_spend
        if p.daily_cost_cap_usd is not None:
            projects_total_cap += float(p.daily_cost_cap_usd)
        rows_projects += (
            f'<tr class="data-row" data-row-id="p{p.id}">'
            f'<td class="rownum"></td>'
            f'<td data-sort="{p.id}">{p.id}</td>'
            f'<td><a href="/dashboard/projects/{p.id}" class="proj-link">'
            f'{esc(p.name)}</a></td>'
            f"<td><span class='pill'>{esc(scopes_csv)}</span></td>"
            f"<td class='{active_class}'>{active_cell}</td>"
            f"<td data-sort=\"{p.daily_cost_cap_usd or 0}\">{cap_disp}</td>"
            f"<td data-sort=\"{p_spend}\" class='mono'>${p_spend:.4f}</td>"
            f"<td><code>{esc(p.project_key_prefix)}…</code></td>"
            f'<td><button type="button" data-edit-toggle="p{p.id}" data-i18n '
            f'data-en="edit" data-ru="ред.">edit</button></td>'
            f"</tr>"
            # ── inline edit form row ──
            f'<tr class="edit-row" data-edit-for="p{p.id}"><td colspan="9">'
            f'<form method="post" action="/dashboard/projects/{p.id}/edit" class="row-form">'
            f'<input name="name" value="{esc(p.name)}" required>'
            f'<span class="scope-group">{_scope_checkboxes(p.allowed_scopes, "allowed_scopes")}</span>'
            f'<input name="daily_cost_cap_usd" type="number" step="0.01" '
            f'value="{cap_val}" '
            f'data-en-placeholder="cap (blank = none)" '
            f'data-ru-placeholder="лимит (пусто = нет)" '
            f'placeholder="cap (blank = none)">'
            f'<input name="owner_email" value="{esc(p.owner_email or "")}" '
            f'data-en-placeholder="owner email" '
            f'data-ru-placeholder="email владельца" '
            f'placeholder="owner email">'
            f'<button type="submit" data-i18n data-en="save" data-ru="сохранить">save</button>'
            f'<button type="button" data-edit-toggle="p{p.id}" data-i18n '
            f'data-en="cancel" data-ru="отмена">cancel</button>'
            f'</form></td></tr>'
        )

    now = datetime.now(UTC).replace(tzinfo=None)
    rows_keys = ""
    keys_total_used = 0
    keys_total_spent = 0.0
    keys_total_cap = 0.0
    keys_total_errs = 0
    keys_alive = 0
    for k in data["keys"]:
        keys_total_used += k.daily_used or 0
        keys_total_spent += float(k.daily_cost_used_usd or 0)
        if k.daily_cost_cap_usd is not None:
            keys_total_cap += float(k.daily_cost_cap_usd)
        keys_total_errs += k.error_count or 0
        if k.is_alive and not (k.cooldown_until and k.cooldown_until > now):
            keys_alive += 1
        in_cd = k.cooldown_until and k.cooldown_until > now
        # A key that's is_alive=False only because its BALANCE ran out isn't a
        # dead credential — it's valid and auto-recovers the moment it's topped
        # up (the monitor's probe revives it). Show "нет средств" (warn), not the
        # alarming "мёртв" (bad), so the operator knows to top up, not replace.
        no_credits = not k.is_alive and not in_cd and _is_top_up(k.last_error)
        status_label = (
            "alive" if (k.is_alive and not in_cd)
            else "cooldown" if in_cd
            else "no_credits" if no_credits
            else "dead"
        )
        status_class = {"alive": "ok", "cooldown": "warn",
                        "no_credits": "warn", "dead": "bad"}[status_label]
        status_en = {"alive": "alive", "cooldown": "cooldown",
                     "no_credits": "no credits", "dead": "dead"}[status_label]
        status_ru = {"alive": "жив", "cooldown": "пауза",
                     "no_credits": "нет средств", "dead": "мёртв"}[status_label]
        # Reason + (for cooldown) when it ends — 2026-07-05: status used to be
        # just "мёртв"/"пауза" with no way to tell "no money" from "rate
        # limited" apart, or when a cooldown actually ends. last_error is set
        # by _penalize (real traffic) / monitor.py (probes); cleared back to
        # None the moment a key is confirmed alive again. A known failure
        # (_friendly_reason) renders as a short actionable EN/RU label
        # ("top up balance"/"пополнить баланс") instead of a raw litellm
        # dump; the full raw text is always still in the hover tooltip.
        cooldown_plain = None
        cooldown_html = None
        if status_label == "cooldown" and k.cooldown_until:
            tf = "hm" if k.cooldown_until.date() == now.date() else "mdhm"
            cooldown_plain = f"until {k.cooldown_until.strftime(_TS_FMT[tf])} UTC"
            cooldown_html = "until " + _ts_span(k.cooldown_until, tf)
        detail_title = esc(" — ".join(
            b for b in (k.last_error, cooldown_plain) if b
        ))
        reason_html = ""
        if k.last_error:
            friendly = _friendly_reason(k.last_error)
            if friendly:
                en, ru = friendly
                reason_html = f'<span data-i18n data-en="{en}" data-ru="{ru}">{en}</span>'
            else:
                short = k.last_error[:40] + ("…" if len(k.last_error) > 40 else "")
                reason_html = esc(short)
        detail_sub = (
            f'<div class="status-detail" title="{detail_title}">'
            f'{reason_html}'
            f'{(" · " + cooldown_html) if cooldown_html else ""}'
            f'</div>' if (k.last_error or cooldown_html) else ""
        )
        status_html = (
            f'<span class="{status_class}" data-i18n title="{detail_title}" '
            f'data-en="{status_en}" data-ru="{status_ru}">{status_en}</span>'
            f'{detail_sub}'
        )

        # Daily-quota usage — show ALL capped axes (req / tok / in / out) so
        # it's obvious every key of a provider shares the SAME caps and only
        # the fill differs. Bar width/colour follows the dominant axis.
        # Token usage pulled live from usage_log (today UTC).
        tt = data["tokens_today"].get(k.id, {})
        tok_today = int(tt.get("tot", 0))
        tin_today = int(tt.get("tin", 0))
        tout_today = int(tt.get("tout", 0))
        axes = axes_for_key(
            k.daily_used or 0, tok_today, k,
            toks_in=tin_today, toks_out=tout_today,
        )
        if axes:
            used_pct = axes[0]["pct"]   # dominant axis drives bar + sort
            bar_fill = severity_class(used_pct)
            src = ("manual" if (k.manual_req_limit or k.manual_tok_limit
                                or k.manual_tok_in_limit or k.manual_tok_out_limit)
                   else "discovered" if k.limits_discovered_at else "default est.")
            # Compact per-axis chips: "84% tok · 15% req"
            chips = " · ".join(f"{a['pct']}% {a['short']}" for a in axes)
            # Tooltip spells out used/cap on every axis + source of the cap.
            detail = " · ".join(
                f"{a['used']:,}/{a['cap']:,} {a['short']}" for a in axes
            )
            used_html = (
                f"<span class='mono'>{chips}</span>"
                f"<span class='cap-bar' title='{detail} · {src}'>"
                f"<span class='fill {bar_fill}' style='width:{used_pct}%'></span></span>"
            )
        else:
            # paid / unknown — no quota, just the count
            used_pct = None
            used_html = f"<span class='mono'>{k.daily_used}</span>"
            # Sort paid keys after quota'd keys (use -1 sentinel in data-sort)

        used = float(k.daily_cost_used_usd or 0)
        cap_v = k.daily_cost_cap_usd
        if cap_v:
            pct = min(100, int(used / float(cap_v) * 100)) if cap_v else 0
            bar_cls = "fill bad" if pct >= 90 else "fill warn" if pct >= 70 else "fill"
            cap_html = (
                f"<span class='mono'>${used:.4f} / ${cap_v:.2f}</span>"
                f"<span class='cap-bar'><span class='{bar_cls}' "
                f"style='width:{pct}%'></span></span>"
            )
            cap_sort = float(cap_v)
        else:
            cap_html = f"<span class='mono'>${used:.4f}</span>"
            cap_sort = 0.0

        cap_input_val = f"{cap_v:.2f}" if cap_v is not None else ""
        scopes_csv = ",".join(k.scopes or ["llm:chat"])
        reserve_checked = " checked" if k.is_reserve else ""
        tier_options = "".join(
            f'<option value="{t}"{" selected" if t == k.tier else ""}>{t}</option>'
            for t in ("free", "paid", "trial")
        )

        rows_keys += (
            f'<tr class="data-row" data-row-id="k{k.id}">'
            f'<td class="rownum"></td>'
            f'<td data-sort="{k.id}">{k.id}</td>'
            f"<td>{esc(k.provider)}</td>"
            f"<td>{esc(k.label)}</td>"
            f"<td data-sort='{esc(k.tier)}'><span class='pill'>{esc(k.tier)}</span></td>"
            f"<td data-sort='{status_label}'>{status_html}</td>"
            f"<td data-sort='{used_pct if used_pct is not None else -1}'>{used_html}</td>"
            f"<td data-sort='{cap_sort}'>{cap_html}</td>"
            f"<td data-sort='{k.error_count}'>{k.error_count}</td>"
            f"<td>"
            f'<button type="button" data-edit-toggle="k{k.id}" '
            f'data-i18n data-en="edit" data-ru="ред.">edit</button> '
            f'<form class="inline" method="post" action="/dashboard/keys/{k.id}/disable">'
            f'<button type="submit" data-i18n '
            + (
                'data-en="enable" data-ru="вкл.">enable'
                if not k.is_active else
                'data-en="disable" data-ru="откл.">disable'
            )
            + "</button>"
            f'</form> '
            f'<form class="inline" method="post" action="/dashboard/keys/{k.id}/delete"'
            f' data-confirm="Delete {esc(k.provider)}/{esc(k.label)}?">'
            f'<button class="danger" type="submit" data-i18n '
            f'data-en="del" data-ru="удал.">del</button>'
            f'</form>'
            f"</td></tr>"
            # ── inline edit form row ──
            f'<tr class="edit-row" data-edit-for="k{k.id}"><td colspan="10">'
            f'<form method="post" action="/dashboard/keys/{k.id}/edit" class="row-form">'
            f'<input name="label" value="{esc(k.label)}" required>'
            f'<select name="tier">{tier_options}</select>'
            f'<span class="scope-group">{_scope_checkboxes(k.scopes or ["llm:chat"])}</span>'
            f'<label class="rsv" title="reserved lane: picked last in its group, '
            f'invisible to other scopes"><input type="checkbox" name="is_reserve" '
            f'value="1"{reserve_checked}> reserve</label>'
            f'<input name="daily_cost_cap_usd" type="number" step="0.01" '
            f'value="{cap_input_val}" '
            f'data-en-placeholder="cap (blank = none)" '
            f'data-ru-placeholder="лимит (пусто = нет)" '
            f'placeholder="cap (blank = none)">'
            f'<input name="token" type="password" style="min-width:240px" '
            f'data-en-placeholder="new token (leave blank to keep)" '
            f'data-ru-placeholder="новый токен (пусто = оставить)" '
            f'placeholder="new token (leave blank to keep)">'
            f'<input name="account_id" value="{esc(k.account_id or "")}" style="min-width:150px" '
            f'title="Only needed for cloudflare." '
            f'data-en-placeholder="account ID" data-ru-placeholder="account ID" '
            f'placeholder="account ID">'
            f'<span class="quota-override" title="Manual daily quota override — '
            f'blank = use discovered/default. For corp keys (e.g. Gemini 3M in / 80k out).">'
            f'<input name="manual_req_limit" type="number" min="0" value="{k.manual_req_limit or ""}" '
            f'data-en-placeholder="req/day" data-ru-placeholder="запр/день" placeholder="req/day">'
            f'<input name="manual_tok_limit" type="number" min="0" value="{k.manual_tok_limit or ""}" '
            f'data-en-placeholder="tok/day" data-ru-placeholder="ток/день" placeholder="tok/day">'
            f'<input name="manual_tok_in_limit" type="number" min="0" value="{k.manual_tok_in_limit or ""}" '
            f'data-en-placeholder="in/day" data-ru-placeholder="вх/день" placeholder="in/day">'
            f'<input name="manual_tok_out_limit" type="number" min="0" value="{k.manual_tok_out_limit or ""}" '
            f'data-en-placeholder="out/day" data-ru-placeholder="исх/день" placeholder="out/day">'
            f'</span>'
            f'<button type="submit" data-i18n data-en="save" data-ru="сохранить">save</button>'
            f'<button type="button" data-edit-toggle="k{k.id}" data-i18n '
            f'data-en="cancel" data-ru="отмена">cancel</button>'
            f'</form></td></tr>'
        )

    provider_options = "".join(
        f'<option value="{p["provider"]}" data-scope="{p["default_scope"]}">'
        f'{p["provider"]}</option>'
        for p in _provider_catalogue()
    )

    add_key_form = f"""
    <fieldset><legend data-i18n data-en="Add API key" data-ru="Добавить API-ключ">Add API key</legend>
      <form method="post" action="/dashboard/keys/create" class="row-form" id="add-key-form">
        <select name="provider" id="add-key-provider" required>
          <option value="" disabled selected hidden
                  data-i18n data-en="— provider —" data-ru="— провайдер —">— provider —</option>
          {provider_options}
        </select>
        <input name="label" required
               data-en-placeholder="label (your handle, project, …)"
               data-ru-placeholder="ярлык (ваш handle, проект, …)"
               placeholder="label (your handle, project, …)">
        <input name="token" type="password" required style="min-width:280px"
               data-en-placeholder="raw token"
               data-ru-placeholder="токен"
               placeholder="raw token">
        <input name="account_id" style="min-width:180px"
               title="Only needed for cloudflare (its API URL embeds the account ID)."
               data-en-placeholder="account ID (cloudflare only)"
               data-ru-placeholder="account ID (только cloudflare)"
               placeholder="account ID (cloudflare only)">
        <select name="tier">
          <option value="free">free</option>
          <option value="paid">paid</option>
          <option value="trial">trial</option>
        </select>
        <span class="scope-group" id="add-key-scope">{_scope_checkboxes(["llm:chat"])}</span>
        <label class="rsv" title="reserved lane: picked last in its group, invisible to other scopes">
          <input type="checkbox" name="is_reserve" value="1"> reserve</label>
        <input name="daily_cost_cap_usd" type="number" step="0.01" style="min-width:130px"
               data-en-placeholder="$ cap (optional)"
               data-ru-placeholder="$ лимит (опц.)"
               placeholder="$ cap (optional)">
        <span class="quota-override" title="Optional daily quota override — blank = use discovered/default. For known caps (e.g. corp Gemini 3M in / 80k out).">
          <input name="manual_req_limit" type="number" min="0"
                 data-en-placeholder="req/day" data-ru-placeholder="запр/день" placeholder="req/day">
          <input name="manual_tok_limit" type="number" min="0"
                 data-en-placeholder="tok/day" data-ru-placeholder="ток/день" placeholder="tok/day">
          <input name="manual_tok_in_limit" type="number" min="0"
                 data-en-placeholder="in/day" data-ru-placeholder="вх/день" placeholder="in/day">
          <input name="manual_tok_out_limit" type="number" min="0"
                 data-en-placeholder="out/day" data-ru-placeholder="исх/день" placeholder="out/day">
        </span>
        <button type="submit" data-i18n data-en="add" data-ru="добавить">add</button>
      </form>
      <div id="provider-hint" class="provider-hint"
           data-i18n
           data-en="Pick a provider — the form will set the right scope and show which models the broker will route through this key."
           data-ru="Выберите провайдера — форма проставит нужный scope и покажет, какие модели брокер будет вызывать через этот ключ.">
        Pick a provider — the form will set the right scope and show which models the broker will route through this key.
      </div>
    </fieldset>"""

    add_project_form = f"""
    <fieldset><legend data-i18n data-en="Add project" data-ru="Добавить проект">Add project</legend>
      <form method="post" action="/dashboard/projects/create" class="row-form">
        <input name="name" required
               data-en-placeholder="name (lowercase, e.g. stepan)"
               data-ru-placeholder="имя (lowercase, напр. stepan)"
               placeholder="name (lowercase, e.g. stepan)">
        <input name="owner_email"
               data-en-placeholder="owner email"
               data-ru-placeholder="email владельца"
               placeholder="owner email">
        <span class="scope-group">{_scope_checkboxes(["llm:chat", "llm:embed"], "allowed_scopes")}</span>
        <input name="daily_cost_cap_usd" type="number" step="0.01"
               data-en-placeholder="cap (optional)"
               data-ru-placeholder="лимит (опц.)"
               placeholder="cap (optional)">
        <button type="submit" data-i18n data-en="create" data-ru="создать">create</button>
      </form>
    </fieldset>"""

    body = f"""
    <script id="provider-meta" type="application/json">{_provider_meta_json()}</script>
    {show_new_key}
    {cards}

    <h2 data-i18n data-en="Providers" data-ru="Провайдеры">Providers</h2>
    <div>{providers_html or '<span class="provider">none</span>'}</div>

    <h2 data-i18n data-en="Projects" data-ru="Проекты">Projects</h2>
    {add_project_form}
    <table><thead><tr>
      <th>#</th>
      <th class="sortable" data-type="num" data-i18n data-en="id" data-ru="id">id</th>
      <th class="sortable" data-i18n data-en="name" data-ru="имя">name</th>
      <th class="sortable" data-i18n data-en="scopes" data-ru="права">scopes</th>
      <th class="sortable" data-i18n data-en="act" data-ru="акт">act</th>
      <th class="sortable" data-type="num" data-i18n data-en="daily cap" data-ru="суточный лимит">daily cap</th>
      <th class="sortable" data-type="num" data-i18n
          data-en="spend in range" data-ru="потрачено за период">spend in range</th>
      <th class="sortable" data-i18n data-en="key prefix" data-ru="префикс ключа">key prefix</th>
      <th data-i18n data-en="actions" data-ru="действия">actions</th>
    </tr></thead><tbody>{rows_projects}</tbody>
    <tfoot><tr>
      <td colspan="4" class="k" data-i18n data-en="TOTAL" data-ru="ИТОГО">TOTAL</td>
      <td>{len(data['projects'])}</td>
      <td class="num">${projects_total_cap:.2f}</td>
      <td class="num">${projects_total_spend:.4f}</td>
      <td colspan="2"></td>
    </tr></tfoot>
    </table>

    <h2 data-i18n data-en="API keys" data-ru="API-ключи">API keys</h2>
    {add_key_form}
    <table><thead><tr>
      <th>#</th>
      <th class="sortable" data-type="num" data-i18n data-en="id" data-ru="id">id</th>
      <th class="sortable" data-i18n data-en="provider" data-ru="провайдер">provider</th>
      <th class="sortable" data-i18n data-en="label" data-ru="ярлык">label</th>
      <th class="sortable" data-i18n data-en="tier" data-ru="тариф">tier</th>
      <th class="sortable" data-i18n data-en="status" data-ru="статус">status</th>
      <th class="sortable" data-type="num" data-i18n
          data-en="daily %" data-ru="% дня">daily %</th>
      <th class="sortable" data-type="num" data-i18n data-en="$/cap" data-ru="$/лимит">$/cap</th>
      <th class="sortable" data-type="num" data-i18n data-en="errs" data-ru="ошибки">errs</th>
      <th data-i18n data-en="actions" data-ru="действия">actions</th>
    </tr></thead><tbody>{rows_keys}</tbody>
    <tfoot><tr>
      <td colspan="5" class="k" data-i18n data-en="TOTAL" data-ru="ИТОГО">TOTAL</td>
      <td><span data-i18n data-en="{keys_alive} alive" data-ru="{keys_alive} живых">{keys_alive} alive</span> / {len(data['keys'])}</td>
      <td class="num">{keys_total_used:,}</td>
      <td class="num">${keys_total_spent:.4f} / ${keys_total_cap:.2f}</td>
      <td class="num">{keys_total_errs}</td>
      <td></td>
    </tr></tfoot>
    </table>
    """
    return HTMLResponse(_dash_html(body=body, flash=flash), headers=_NO_STORE)


def _cache_card(cache_read: int, cache_write: int) -> str:
    """Prompt-cache KPI card — only anthropic calls ever populate these
    (apply_prompt_cache), so most projects/ranges show neither; omit the card
    entirely rather than show a permanent 0/0. Reuse ratio (reads per write)
    is the honest cache-efficiency signal: one write feeds many cheap reads."""
    if not cache_read and not cache_write:
        return ""
    reuse = f"{cache_read / cache_write:.1f}× reuse" if cache_write else "—"
    return f"""
      <div class="card">
        <div class="card-label" data-i18n data-en="Prompt cache" data-ru="Кэш промпта">Prompt cache</div>
        <div class="card-value" style="font-size:18px">{cache_read:,} / {cache_write:,}</div>
        <div class="card-sub" data-i18n
             data-en="read / write · {reuse}" data-ru="чтения / записи · {reuse}">read / write · {reuse}</div>
      </div>
    """


# Sparkline geometry (px). Thin bars, 1px gaps — _SPARK_BUCKETS (24) of them
# at these dims render ~96px wide, fitting the narrow half of the split
# capability/workflow card.
_SPARK_BAR_W = 3
_SPARK_GAP = 1
_SPARK_H = 20


def _sparkline_svg(buckets: list[tuple[int, int]]) -> str:
    """Thin stacked-bar histogram: one bar per time bucket, blue=ok stacked
    below red=error, so a row's error share is visible at a glance. Each
    bar's height is scaled to THIS row's own busiest bucket (not the busiest
    across all rows) — a quiet workflow's bars stay visible instead of
    vanishing next to a loud one."""
    n = len(buckets)
    w = n * (_SPARK_BAR_W + _SPARK_GAP)
    max_total = max((ok + err for ok, err in buckets), default=0) or 1
    bars: list[str] = []
    for i, (ok, err) in enumerate(buckets):
        total = ok + err
        if not total:
            continue
        x = i * (_SPARK_BAR_W + _SPARK_GAP)
        total_h = max(1, round(total / max_total * _SPARK_H))
        ok_h = round(ok / total * total_h)
        err_h = total_h - ok_h
        if ok_h:
            bars.append(
                f'<rect x="{x}" y="{_SPARK_H - ok_h}" width="{_SPARK_BAR_W}" '
                f'height="{ok_h}" fill="#4dabf7"/>'
            )
        if err_h:
            bars.append(
                f'<rect x="{x}" y="{_SPARK_H - total_h}" width="{_SPARK_BAR_W}" '
                f'height="{err_h}" fill="#f44336"/>'
            )
    return (
        f'<svg class="spark" width="{w}" height="{_SPARK_H}" '
        f'viewBox="0 0 {w} {_SPARK_H}" preserveAspectRatio="none">'
        f'{"".join(bars)}</svg>'
    )


def _render_project_detail(d: dict[str, Any]) -> HTMLResponse:
    p = d["project"]
    t = d["totals"]
    err_pct = (t.err_n / t.calls * 100.0) if t.calls else 0.0
    ok_pct  = (t.ok_n  / t.calls * 100.0) if t.calls else 0.0

    cap_disp = (
        f"${p.daily_cost_cap_usd:.2f}" if p.daily_cost_cap_usd is not None else "—"
    )

    range_links = "".join(
        f'<a href="?range={r}" class="{"active" if d["hours"] == h else ""}">{r}</a>'
        for r, h in _RANGE_HOURS.items()
    )

    cards = f"""
    <div class="cards">
      <div class="card">
        <div class="card-label" data-i18n data-en="Calls" data-ru="Вызовов">Calls</div>
        <div class="card-value">{t.calls}</div>
        <div class="card-sub">
          <span class="ok">{t.ok_n} ok</span> ·
          <span class="bad">{t.err_n} err</span> ({err_pct:.0f}%)
        </div>
      </div>
      <div class="card">
        <div class="card-label" data-i18n data-en="Spend" data-ru="Потрачено">Spend</div>
        <div class="card-value">${float(t.spend):.4f}</div>
        <div class="card-sub" data-i18n
             data-en="daily cap {cap_disp}" data-ru="суточный лимит {cap_disp}">daily cap {cap_disp}</div>
      </div>
      <div class="card">
        <div class="card-label" data-i18n data-en="Tokens in / out" data-ru="Токены вх / исх">Tokens in / out</div>
        <div class="card-value" style="font-size:18px">{t.tin:,} / {t.tout:,}</div>
      </div>
      <div class="card">
        <div class="card-label" data-i18n data-en="Avg latency" data-ru="Средн. задержка">Avg latency</div>
        <div class="card-value">{int(t.avg_lat or 0)} ms</div>
        <div class="card-sub">{ok_pct:.0f}% success</div>
      </div>
      {_cache_card(t.cache_read, t.cache_write)}
    </div>
    """

    def _bd_section(title_en: str, title_ru: str, rows: list[tuple], fmt_row,
                      total_label_en: str = "total", total_label_ru: str = "итого",
                      total: tuple | None = None, colspan: int = 3) -> str:
        """h3 + table only — no wrapping .brk-card div, so two sections can share
        one card (see cap_wf_card, split by the .brk-section CSS divider)."""
        body = "".join(fmt_row(r) for r in rows) or (
            f'<tr><td colspan="{colspan}" style="color:#5a6171" data-i18n '
            'data-en="(no data in this range)" data-ru="(нет данных за период)">'
            "(no data in this range)</td></tr>"
        )
        total_html = ""
        if total:
            total_html = (
                '<tr class="total-row">'
                f'<td data-i18n data-en="{total_label_en}" data-ru="{total_label_ru}">'
                f'{total_label_en}</td>'
                f'<td class="num">{total[0]}</td><td class="num">{total[1]}</td>'
                '</tr>'
            )
        return (
            f'<div class="brk-section">'
            f'<h3 data-i18n data-en="{title_en}" data-ru="{title_ru}">{title_en}</h3>'
            f'<table><tbody>{body}{total_html}</tbody></table></div>'
        )

    def _bd_card(title_en: str, title_ru: str, rows: list[tuple],
                  fmt_row, total_label_en: str = "total",
                  total_label_ru: str = "итого", total: tuple | None = None) -> str:
        section = _bd_section(title_en, title_ru, rows, fmt_row,
                               total_label_en, total_label_ru, total)
        return f'<div class="brk-card">{section}</div>'

    prov_card = _bd_card("By provider", "По провайдерам", list(d["by_provider"]),
        lambda r: f'<tr><td class="k">{esc(r.provider)}</td>'
                  f'<td class="num">{r.n}</td>'
                  f'<td class="num">${float(r.spend):.4f}</td></tr>',
        total=(t.calls, f"${float(t.spend):.4f}"))

    # Capability + workflow are two small, related slices of the same calls —
    # merged into one card (a horizontal divider splits it top/bottom) instead
    # of two separate grid cells. Each row also gets a mini ok/error-over-time
    # histogram (cap_spark/wf_spark: N buckets spanning the selected range),
    # so a spike or an error burst on one specific capability/workflow is
    # visible without switching to the (unfiltered) latency histogram below.
    cap_spark = d.get("cap_spark", {})
    wf_spark = d.get("wf_spark", {})
    _empty_spark = [(0, 0)] * _SPARK_BUCKETS
    cap_wf_card = (
        '<div class="brk-card brk-card-split">'
        + _bd_section("By capability", "По способностям", list(d["by_capability"]),
            lambda r: f'<tr><td class="k">{esc(r.cap)}</td>'
                      f'<td class="num">{r.n}</td>'
                      f'<td class="num">${float(r.spend):.4f}</td>'
                      f'<td>{_sparkline_svg(cap_spark.get(r.cap, _empty_spark))}</td></tr>',
            colspan=4)
        + _bd_section("By workflow", "По workflow", list(d["by_workflow"]),
            lambda r: f'<tr><td class="k">{esc(r.wf)}</td>'
                      f'<td class="num">{r.n}</td>'
                      f'<td class="num">${float(r.spend):.4f}</td>'
                      f'<td>{_sparkline_svg(wf_spark.get(r.wf, _empty_spark))}</td></tr>',
            colspan=4)
        + '</div>'
    )

    model_card = _bd_card("Top models", "Топ моделей", list(d["by_model"]),
        lambda r: f'<tr><td class="k" style="font-size:11px">{esc(r.model or "")}</td>'
                  f'<td class="num">{r.n}</td>'
                  f'<td class="num">${float(r.spend):.4f}</td></tr>')

    # Latency histogram: count of calls per latency bucket (same period), bars
    # scaled to the busiest bucket. Reuses the cap-bar/fill quota-bar styling.
    lat_counts = d["lat_hist"]
    lat_max = max(lat_counts) or 1
    lat_rows = "".join(
        f'<tr><td class="k">{esc(lbl)}</td>'
        f"<td style='width:55%'><span class='cap-bar'>"
        f"<span class='fill' style='width:{int(n / lat_max * 100)}%'></span>"
        f'</span></td><td class="num">{n}</td></tr>'
        for lbl, n in zip(_LAT_LABELS, lat_counts, strict=True)
    )
    lat_card = (
        '<div class="brk-card">'
        '<h3 data-i18n data-en="Latency distribution" '
        'data-ru="Распределение задержек">Latency distribution</h3>'
        f'<table><tbody>{lat_rows}</tbody></table></div>'
    ) if sum(lat_counts) else ""

    # tr.data-row marker is required by the sortable-table JS in _dash_html.
    # data-sort on the time column uses iso8601 so lexical sort works.
    # data-row-id is usage_log.id — the same request_id returned to the API
    # caller in its response, so the caller can paste it here to find the call.
    recent_rows = "".join(
        f'<tr class="data-row" data-row-id="{r.id}">'
        f'<td class="num" data-sort="{r.id}" '
        f'style="color:#666;font-size:11px">{r.id}</td>'
        f'<td data-sort="{r.created_at.isoformat()}" '
        f'style="color:#888;font-size:11px">'
        f'{_ts_span(r.created_at, "mdhms")}</td>'
        f'<td>{esc(r.provider)}</td>'
        f'<td style="color:#888;font-size:11px">{esc((r.model or "—")[:32])}</td>'
        f'<td><span class="pill">{esc(r.capability or "—")}</span></td>'
        f'<td class="num" data-sort="{r.tokens_in + r.tokens_out}">'
        f'{r.tokens_in}/{r.tokens_out}</td>'
        f'<td class="num" data-sort="{float(r.cost_usd)}">'
        f'${float(r.cost_usd):.4f}</td>'
        f'<td class="num" data-sort="{r.latency_ms or 0}">'
        f'{r.latency_ms or "—"}</td>'
        f'<td class="status-{esc(r.status)}">{esc(r.status)}</td>'
        f'<td style="color:#888;font-size:11px">{r.http_status or ""} '
        f'{esc(r.error_kind or "")}</td></tr>'
        for r in d["recent"]
    ) or '<tr><td colspan="10" style="color:#5a6171">no calls yet</td></tr>'

    body = f"""
    <div class="breadcrumb">
      <a href="/dashboard">← dashboard</a> /
      <span>project {p.id}</span>
    </div>
    <h1 style="margin:0 0 4px;font-weight:500">{esc(p.name)}
      <span class="range-pills">{range_links}</span>
    </h1>
    <div style="color:#888;font-size:13px;margin-bottom:18px">
      <code>{esc(p.project_key_prefix)}…</code> ·
      scopes <span class="pill">{esc(",".join(p.allowed_scopes))}</span> ·
      {"active" if p.is_active else "<span class=bad>disabled</span>"}
      {f" · owner {esc(p.owner_email)}" if p.owner_email else ""}
    </div>

    {cards}

    <div class="breakdown">
      {prov_card}
      {cap_wf_card}
      {model_card}
      {lat_card}
    </div>

    <h2 data-i18n data-en="Recent 50 calls" data-ru="Последние 50 вызовов">Recent 50 calls</h2>
    <table class="recent-table"><thead><tr>
      <th class="sortable" data-type="num"
          title="usage_log.id — the same request_id returned in the API response"
          data-i18n data-en="req id" data-ru="req id">req id</th>
      <th class="sortable">when</th>
      <th class="sortable">provider</th>
      <th class="sortable">model</th>
      <th class="sortable">cap</th>
      <th class="sortable" data-type="num">tok in/out</th>
      <th class="sortable" data-type="num">$</th>
      <th class="sortable" data-type="num">ms</th>
      <th class="sortable">status</th>
      <th>http / err</th>
    </tr></thead><tbody>{recent_rows}</tbody></table>
    """
    return HTMLResponse(_dash_html(body=body), headers=_NO_STORE)
