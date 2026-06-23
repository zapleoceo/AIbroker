"""Public health + landing endpoints — no auth."""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter
from fastapi.responses import HTMLResponse
from sqlalchemy import text

from aibroker.db import get_session
from aibroker import __version__

router = APIRouter(tags=["health"])


@router.get("/", response_class=HTMLResponse)
async def landing() -> HTMLResponse:
    """Static landing page — navigation for ops + public-info."""
    return HTMLResponse(f"""<!doctype html><html><head>
<meta charset="utf-8"><title>AIbroker</title>
<style>
body {{ font-family: -apple-system, sans-serif; background:#0f1115; color:#e4e6eb;
       margin:0; padding:60px 24px; max-width:720px; margin-inline:auto; line-height:1.6; }}
h1 {{ font-weight:500; font-size:32px; margin-bottom:4px; }}
.tag {{ color:#666; font-size:13px; margin-bottom:32px; }}
.row {{ display:flex; gap:14px; margin:14px 0; align-items:center; }}
.method {{ font-family:ui-monospace,monospace; background:#1a1d24; padding:3px 10px;
           border-radius:4px; font-size:12px; color:#4dabf7; min-width:55px; text-align:center; }}
a {{ color:#4dabf7; text-decoration:none; font-family:ui-monospace,monospace; }}
a:hover {{ text-decoration:underline; }}
.note {{ color:#666; font-size:12px; }}
hr {{ border:none; border-top:1px solid #2a2d34; margin:32px 0; }}
code {{ background:#1a1d24; padding:2px 6px; border-radius:3px; font-size:12px; color:#4dabf7; }}
</style></head><body>

<h1>AIbroker</h1>
<div class="tag">centralized key broker · v{__version__}</div>

<div class="row"><span class="method">GET</span><a href="/healthz">/healthz</a>
  <span class="note">— liveness probe</span></div>
<div class="row"><span class="method">GET</span><a href="/v1/health">/v1/health</a>
  <span class="note">— per-provider key health</span></div>
<div class="row"><span class="method">GET</span><a href="/docs">/docs</a>
  <span class="note">— Swagger UI (full API)</span></div>
<div class="row"><span class="method">GET</span><a href="/dashboard">/dashboard</a>
  <span class="note">— requires header <code>X-Admin-Key</code></span></div>

<hr>

<h3 style="font-weight:500;font-size:18px;color:#aaa">For clients</h3>
<div class="row"><span class="method">POST</span><span>/v1/chat?capability=chat:fast</span></div>
<div class="row"><span class="method">POST</span><span>/v1/embed?provider=voyage</span></div>
<div class="row"><span class="method">POST</span><span>/v1/key</span>
  <span class="note">— vending mode</span></div>

<p class="note" style="margin-top:32px">
  All client endpoints require <code>X-Project-Key</code>.
  Admin CRUD lives under <code>/admin/*</code> with <code>X-Admin-Key</code>.
</p>

</body></html>""")


@router.get("/healthz")
async def healthz() -> dict:
    return {"ok": True, "service": "aibroker", "ts": datetime.now(timezone.utc).isoformat()}


@router.get("/v1/health")
async def health_summary() -> dict:
    """Per-provider alive/dead/cooldown counts. Surface for ops dashboards."""
    async with get_session() as s:
        rows = (
            await s.execute(
                text(
                    "SELECT provider, "
                    "       COUNT(*) FILTER (WHERE is_active AND is_alive "
                    "                         AND (cooldown_until IS NULL OR cooldown_until < now())) AS alive, "
                    "       COUNT(*) FILTER (WHERE is_active AND is_alive AND cooldown_until > now()) AS cooldown, "
                    "       COUNT(*) FILTER (WHERE NOT is_alive OR NOT is_active) AS dead, "
                    "       COUNT(*) AS total "
                    "FROM api_keys GROUP BY provider ORDER BY provider"
                )
            )
        ).all()
    return {
        "providers": [
            {"provider": r[0], "alive": r[1], "cooldown": r[2], "dead": r[3], "total": r[4]}
            for r in rows
        ],
    }
