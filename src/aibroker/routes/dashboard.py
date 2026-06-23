"""Minimal observability dashboard. Single page, dark theme, no JS framework."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import HTMLResponse
from sqlalchemy import select, text

from aibroker.auth import generate_project_key  # noqa: F401  (might be used by inline ops)
from aibroker.config import get_settings
from aibroker.db import get_session
from aibroker.db.models import ApiKeyRow, ProjectRow

router = APIRouter(tags=["dashboard"])


def _auth(x_admin_key: str | None) -> None:
    if not x_admin_key or x_admin_key != get_settings().ADMIN_KEY:
        raise HTTPException(401, "X-Admin-Key required")


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard(x_admin_key: str | None = Header(None, alias="X-Admin-Key")) -> HTMLResponse:
    _auth(x_admin_key)
    async with get_session() as s:
        projects = (await s.execute(select(ProjectRow).order_by(ProjectRow.id))).scalars().all()
        keys = (await s.execute(select(ApiKeyRow).order_by(ApiKeyRow.provider, ApiKeyRow.id))).scalars().all()

        today = datetime.now(timezone.utc).date()
        spend_today = (
            await s.execute(
                text("SELECT COALESCE(SUM(cost_usd), 0) FROM usage_log "
                     "WHERE created_at::date = :d"),
                {"d": today},
            )
        ).scalar() or 0.0

        provider_summary = (
            await s.execute(
                text(
                    "SELECT provider, "
                    " COUNT(*) FILTER (WHERE is_active AND is_alive "
                    "                   AND (cooldown_until IS NULL OR cooldown_until < now())) AS alive, "
                    " COUNT(*) FILTER (WHERE NOT is_alive OR NOT is_active) AS dead, "
                    " COUNT(*) AS total "
                    "FROM api_keys GROUP BY provider ORDER BY provider"
                )
            )
        ).all()

        calls_1h = (
            await s.execute(
                text("SELECT COUNT(*) FROM usage_log WHERE created_at > now() - interval '1 hour'")
            )
        ).scalar() or 0

    s_global = float(spend_today)
    cap_global = get_settings().GLOBAL_DAILY_CAP_USD

    rows_projects = "".join(
        f"<tr><td>{p.id}</td><td>{p.name}</td><td>{', '.join(p.allowed_scopes)}</td>"
        f"<td>{'✓' if p.is_active else '✗'}</td>"
        f"<td>{p.daily_cost_cap_usd or '—'}</td><td><code>{p.project_key_prefix}…</code></td></tr>"
        for p in projects
    )

    rows_keys = "".join(
        f"<tr><td>{k.id}</td><td>{k.provider}</td><td>{k.label}</td><td>{k.tier}</td>"
        f"<td>{'✓' if k.is_alive else '✗'}{' ⏸' if k.cooldown_until and k.cooldown_until > datetime.now(timezone.utc).replace(tzinfo=None) else ''}</td>"
        f"<td>{k.daily_used}</td>"
        f"<td>${k.daily_cost_used_usd:.4f}{'/' + str(k.daily_cost_cap_usd) if k.daily_cost_cap_usd else ''}</td>"
        f"<td>{k.error_count}</td></tr>"
        for k in keys
    )

    providers_html = "".join(
        f'<div class="provider"><b>{p}</b> <span class="ok">{a}</span>'
        f' / <span class="bad">{d}</span> / {t}</div>'
        for p, a, d, t in provider_summary
    )

    return HTMLResponse(f"""
<!doctype html><html><head><meta charset="utf-8">
<title>AIbroker</title>
<style>
body {{ font-family: -apple-system, sans-serif; background:#0f1115; color:#e4e6eb;
       margin:0; padding:24px; }}
h1, h2 {{ font-weight:500; }}
.cards {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(220px,1fr));
         gap:14px; margin:14px 0; }}
.card {{ background:#1a1d24; padding:18px; border-radius:10px; border:1px solid #2a2d34; }}
.card-label {{ font-size:11px; color:#888; text-transform:uppercase; letter-spacing:.05em; }}
.card-value {{ font-size:28px; font-weight:600; margin-top:4px; }}
.card-sub {{ font-size:12px; color:#888; }}
table {{ width:100%; border-collapse:collapse; background:#1a1d24;
         border-radius:10px; overflow:hidden; margin:14px 0; }}
th, td {{ padding:9px 12px; text-align:left; border-bottom:1px solid #2a2d34; font-size:13px; }}
th {{ background:#0f1115; color:#888; text-transform:uppercase; font-size:11px; }}
code {{ font-family:ui-monospace, monospace; color:#4dabf7; }}
.ok {{ color:#4caf50; }} .bad {{ color:#f44336; }}
.provider {{ display:inline-block; margin:4px 8px 4px 0; padding:6px 12px;
             background:#1a1d24; border:1px solid #2a2d34; border-radius:6px; font-size:13px; }}
</style></head><body>

<h1>AIbroker</h1>

<div class="cards">
  <div class="card"><div class="card-label">Spend today</div>
    <div class="card-value">${s_global:.4f}</div>
    <div class="card-sub">cap ${cap_global}</div></div>
  <div class="card"><div class="card-label">Calls 1h</div>
    <div class="card-value">{calls_1h}</div></div>
  <div class="card"><div class="card-label">Projects</div>
    <div class="card-value">{len(projects)}</div></div>
  <div class="card"><div class="card-label">API keys</div>
    <div class="card-value">{len(keys)}</div></div>
</div>

<h2>Providers</h2>
<div>{providers_html or '<div class="provider">none</div>'}</div>

<h2>Projects</h2>
<table><thead><tr><th>id</th><th>name</th><th>scopes</th><th>act</th>
<th>daily cap</th><th>key</th></tr></thead><tbody>{rows_projects}</tbody></table>

<h2>API keys</h2>
<table><thead><tr><th>id</th><th>provider</th><th>label</th><th>tier</th>
<th>alive</th><th>used</th><th>$$$</th><th>errs</th></tr></thead>
<tbody>{rows_keys}</tbody></table>

<p style="margin-top:24px;color:#666;font-size:12px;">
  Use <code>POST /admin/projects</code> + <code>POST /admin/keys</code> with
  <code>X-Admin-Key</code> to add. See <a style="color:#4dabf7" href="/docs">/docs</a>.
</p>
</body></html>
""")
