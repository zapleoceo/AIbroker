"""POST /v1/signup - no-auth self-service project + key. Logic: services/signup.py."""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from aibroker.auth import client_ip
from aibroker.config import get_settings
from aibroker.services.signup import (
    SignupDisabled,
    SignupNameInvalid,
    SignupRateLimited,
    self_signup,
)

router = APIRouter(tags=["signup"])


class SignupRequest(BaseModel):
    name: str = Field(min_length=2, max_length=100,
                      description="Your service's name; normalised to a lowercase slug.")
    contact: str | None = Field(None, max_length=200, description="Email or handle (optional).")
    purpose: str | None = Field(None, max_length=500, description="What it is for (optional).")


class SignupLimits(BaseModel):
    daily_cost_cap_usd: float = Field(description="0 = free providers only.")
    total_request_cap: int = Field(description="Lifetime client requests allowed.")
    scopes: list[str]


class SignupResponse(BaseModel):
    name: str = Field(description="Final project name (a suffix is added on a clash).")
    project_key: str = Field(description="Send as X-Project-Key. Shown ONCE.")
    limits: SignupLimits
    docs_url: str
    upgrade: str


def _error(status: int, code: str, message: str, headers: dict[str, str] | None = None,
           **extra: object) -> JSONResponse:
    return JSONResponse({"error": code, "message": message, **extra},
                        status_code=status, headers=headers)


@router.post("/signup", response_model=SignupResponse, responses={
    403: {"description": "signup_disabled"}, 422: {"description": "invalid_name"},
    429: {"description": "signup_rate_limited (per IP or global, per day)"},
})
async def signup_endpoint(body: SignupRequest, request: Request):
    """Get yourself a project key, no approval needed: free providers only
    ($0/day) and a lifetime request cap. Ask the owner to raise either."""
    try:
        res = await self_signup(
            name=body.name, contact=body.contact, purpose=body.purpose,
            ip=client_ip(request), user_agent=request.headers.get("user-agent", ""),
        )
    except SignupDisabled:
        return _error(403, "signup_disabled", "Self-signup is turned off; ask the owner for a key.")
    except SignupRateLimited as e:
        return _error(429, "signup_rate_limited",
                      f"Too many signups ({e.scope} limit {e.limit}/day); try again tomorrow "
                      "or ask the owner for a key.",
                      headers={"Retry-After": "3600"}, scope=e.scope, limit=e.limit)
    except SignupNameInvalid as e:
        return _error(422, "invalid_name", str(e))
    return SignupResponse(
        name=res.name, project_key=res.project_key,
        limits=SignupLimits(daily_cost_cap_usd=res.daily_cost_cap_usd,
                            total_request_cap=res.total_request_cap, scopes=res.scopes),
        docs_url=f"https://{get_settings().PUBLIC_HOST}/docs",
        upgrade="Need more? Contact the owner with your project name to raise the limits.",
    )
