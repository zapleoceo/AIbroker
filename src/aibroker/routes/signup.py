"""Public self-signup: POST /v1/signup (+ GET /v1/signup/challenge, POST /v1/signup/verify)
and the web form GET /signup. Logic: services/signup.py, services/signup_guard.py."""
from __future__ import annotations

from html import escape

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from aibroker.auth import ProjectCtx, client_ip, require_project
from aibroker.config import get_settings
from aibroker.services.signup import (
    PowInvalid,
    SignupContactRefused,
    SignupDisabled,
    SignupNameInvalid,
    SignupNotEligible,
    SignupRateLimited,
    SignupTelegramInvalid,
    self_signup,
    verify_signup,
)
from aibroker.services.signup_guard import issue_challenge

router = APIRouter(tags=["signup"])
page_router = APIRouter(include_in_schema=False)


class ProofOfWork(BaseModel):
    challenge: str = Field(max_length=200, description="From GET /v1/signup/challenge.")
    nonce: str = Field(max_length=20, description="Decimal integer that solves it.")


class SignupRequest(BaseModel):
    name: str = Field(min_length=2, max_length=100,
                      description="Your service's name; normalised to a lowercase slug.")
    contact: str | None = Field(None, max_length=200, description="Email or handle (optional).")
    purpose: str | None = Field(None, max_length=500, description="What it is for (optional).")
    pow: ProofOfWork | None = Field(None, description="Solved challenge (required).")
    telegram: dict[str, str | int] | None = Field(
        None, description="Telegram Login Widget payload (id, auth_date, hash, ...): "
                          "verifies the project at once.")


class SignupLimits(BaseModel):
    daily_cost_cap_usd: float = Field(description="0 = free providers only.")
    total_request_cap: int = Field(description="Lifetime client requests allowed.")
    scopes: list[str]


class SignupResponse(BaseModel):
    name: str = Field(description="Final project name (a suffix is added on a clash).")
    project_key: str = Field(description="Send as X-Project-Key. Shown ONCE.")
    verified: bool = Field(False, description="Telegram-verified (full request cap).")
    limits: SignupLimits
    docs_url: str
    verify_url: str = Field("", description="Where an unverified project raises its cap.")
    upgrade: str


class VerifyRequest(BaseModel):
    telegram: dict[str, str | int] = Field(description="Telegram Login Widget payload.")


class VerifyResponse(BaseModel):
    name: str
    verified: bool
    limits: SignupLimits


def _error(status: int, code: str, message: str, headers: dict[str, str] | None = None,
           **extra: object) -> JSONResponse:
    return JSONResponse({"error": code, "message": message, **extra},
                        status_code=status, headers=headers)


def _rate_limited(e: SignupRateLimited) -> JSONResponse:
    return _error(429, "signup_rate_limited",
                  f"Too many signups ({e.scope} limit {e.limit}); try again later "
                  "or ask the owner for a key.",
                  headers={"Retry-After": "3600"}, scope=e.scope, limit=e.limit)


@router.get("/signup/challenge")
async def signup_challenge() -> dict[str, object]:
    """A fresh proof-of-work challenge for POST /v1/signup (single use, 10 minutes)."""
    return issue_challenge()


@router.post("/signup", response_model=SignupResponse, responses={
    400: {"description": "pow_invalid / telegram_invalid"},
    403: {"description": "signup_disabled"},
    422: {"description": "invalid_name / disposable_email"},
    429: {"description": "signup_rate_limited (ip, global, email_domain or telegram)"},
})
async def signup_endpoint(body: SignupRequest, request: Request):
    """Get yourself a project key, no approval needed: free providers only ($0/day) and a
    lifetime request cap - small until the project is verified with a Telegram account."""
    s = get_settings()
    try:
        res = await self_signup(
            name=body.name, contact=body.contact, purpose=body.purpose,
            ip=client_ip(request), user_agent=request.headers.get("user-agent", ""),
            pow_challenge=body.pow.challenge if body.pow else None,
            pow_nonce=body.pow.nonce if body.pow else None,
            telegram=body.telegram,
        )
    except SignupDisabled:
        return _error(403, "signup_disabled", "Self-signup is turned off; ask the owner for a key.")
    except SignupRateLimited as e:
        return _rate_limited(e)
    except SignupNameInvalid as e:
        return _error(422, "invalid_name", str(e))
    except SignupContactRefused as e:
        return _error(422, "disposable_email", str(e))
    except PowInvalid as e:
        return _error(400, "pow_invalid", str(e))
    except SignupTelegramInvalid as e:
        return _error(400, "telegram_invalid", str(e))
    return SignupResponse(
        name=res.name, project_key=res.project_key, verified=res.verified,
        limits=SignupLimits(daily_cost_cap_usd=res.daily_cost_cap_usd,
                            total_request_cap=res.total_request_cap, scopes=res.scopes),
        docs_url=f"https://{s.PUBLIC_HOST}/docs",
        verify_url="" if res.verified else f"https://{s.PUBLIC_HOST}/signup#verify",
        upgrade=("Verified projects get the full request cap; need more? Contact the owner "
                 "with your project name to raise the limits."),
    )


@router.post("/signup/verify", response_model=VerifyResponse, responses={
    400: {"description": "telegram_invalid"}, 409: {"description": "not_eligible"},
    429: {"description": "signup_rate_limited (telegram)"},
})
async def signup_verify(body: VerifyRequest, request: Request,
                        ctx: ProjectCtx = Depends(require_project)):
    """Verify an unverified self-signup project with a Telegram account: its lifetime
    request cap rises to the verified cap. Send the project key as X-Project-Key."""
    try:
        res = await verify_signup(ctx.project.id, body.telegram, client_ip(request))
    except SignupTelegramInvalid as e:
        return _error(400, "telegram_invalid", str(e))
    except SignupNotEligible as e:
        return _error(409, "not_eligible", str(e))
    except SignupRateLimited as e:
        return _rate_limited(e)
    return VerifyResponse(name=res.name, verified=True, limits=SignupLimits(
        daily_cost_cap_usd=res.daily_cost_cap_usd, total_request_cap=res.total_request_cap,
        scopes=res.scopes))


_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Sign up · AIbroker</title>
<style>
body{font:16px/1.5 system-ui,sans-serif;max-width:560px;margin:40px auto;padding:0 16px;color:#111}
label{display:block;margin-top:12px;font-weight:600}input,textarea{width:100%;padding:8px;
box-sizing:border-box;font:inherit}button{margin-top:16px;padding:10px 16px;font:inherit}
pre{background:#f4f4f4;padding:12px;white-space:pre-wrap;word-break:break-all}
.muted{color:#666;font-size:14px}
@media (prefers-color-scheme:dark){body{background:#111;color:#eee}pre{background:#222}}
</style></head><body>
<h1>Get an AIbroker project key</h1>
<p>One key, one API in front of many LLM and media providers. New keys use free providers
only. Sign in with Telegram to verify the project and get the full request cap
(__FULL__ requests); without it the key works for __SMALL__ requests.</p>
<form id="f">
<label>Project name<input name="name" required minlength="2" maxlength="100"></label>
<label>Contact (email or handle, optional)<input name="contact" maxlength="200"></label>
<label>What is it for? (optional)<textarea name="purpose" maxlength="500"></textarea></label>
<p class="muted">Verify with Telegram (recommended):</p>
<script async src="https://telegram.org/js/telegram-widget.js?22" data-telegram-login="__BOT__"
 data-size="medium" data-request-access="" data-onauth="onTelegramAuth(user)"></script>
<p class="muted" id="tg">Not verified yet.</p>
<button type="submit">Create project key</button>
</form>
<h2 id="verify">Verify an existing key</h2>
<p class="muted">Paste the key, sign in with Telegram above, then press Verify.</p>
<input id="key" placeholder="X-Project-Key"><button id="vb" type="button">Verify</button>
<pre id="out" hidden></pre>
<script>
let tg = null;
function onTelegramAuth(user){tg = user; document.getElementById("tg").textContent =
  "Telegram: " + (user.username || user.first_name) + " - will verify.";}
const out = document.getElementById("out");
function show(t){out.hidden = false; out.textContent = t;}
async function solve(ch){
  const bits = ch.difficulty_bits, enc = new TextEncoder();
  for (let base = 0; ; base += 512) {
    const batch = [];
    for (let i = 0; i < 512; i++) batch.push(crypto.subtle.digest("SHA-256",
      enc.encode(ch.challenge + ":" + (base + i))));
    const res = await Promise.all(batch);
    for (let i = 0; i < res.length; i++) {
      const b = new Uint8Array(res[i]); let z = 0;
      for (const x of b) { if (x === 0) { z += 8; continue; } z += Math.clz32(x) - 24; break; }
      if (z >= bits) return String(base + i);
    }
  }
}
document.getElementById("f").addEventListener("submit", async (e) => {
  e.preventDefault(); show("Solving the anti-bot challenge...");
  const ch = await (await fetch("/v1/signup/challenge")).json();
  const nonce = ch.difficulty_bits > 0 ? await solve(ch) : "0";
  const fd = new FormData(e.target);
  const body = {name: fd.get("name"), contact: fd.get("contact") || null,
    purpose: fd.get("purpose") || null, pow: {challenge: ch.challenge, nonce}, telegram: tg};
  const r = await fetch("/v1/signup", {method: "POST",
    headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)});
  show(JSON.stringify(await r.json(), null, 2));
});
document.getElementById("vb").addEventListener("click", async () => {
  if (!tg) { show("Sign in with Telegram first."); return; }
  const r = await fetch("/v1/signup/verify", {method: "POST", headers: {
    "Content-Type": "application/json", "X-Project-Key": document.getElementById("key").value},
    body: JSON.stringify({telegram: tg})});
  show(JSON.stringify(await r.json(), null, 2));
});
</script></body></html>"""


@page_router.get("/signup", response_class=HTMLResponse)
async def signup_page() -> HTMLResponse:
    s = get_settings()
    return HTMLResponse(_PAGE.replace("__BOT__", escape(s.TELEGRAM_BOT_USERNAME or "telegram"))
                        .replace("__FULL__", str(s.SIGNUP_REQUEST_CAP))
                        .replace("__SMALL__", str(s.SIGNUP_UNVERIFIED_REQUEST_CAP)))
