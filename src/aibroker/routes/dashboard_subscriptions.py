"""Dashboard: login health of every subscription account, and a one-click re-login.

The page only reads what each worker reported (agent_login_status) and drives a re-login
session (agent_login_sessions) that the account's own worker carries out. The api never
touches a CLI or a credential. docs/subscription-logins.md.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote_plus

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse, Response

from aibroker.agent import login_sessions as ls
from aibroker.agent.account_choices import ACCOUNT_CHOICES
from aibroker.agent.login_status import account_logins
from aibroker.auth import client_ip
from aibroker.auth_session import OwnerSession, require_owner_session
from aibroker.db.models import AgentLoginSessionRow
from aibroker.routes.dashboard_pages import _ctx, _guard
from aibroker.telemetry import audit
from aibroker.web.render import render

router = APIRouter()
PAGE = "/dashboard/subscriptions"
# A finished sign-in stays visible this long, so its result is read before it disappears.
SHOW_FINISHED = timedelta(hours=1)
# Re-render while the owner has nothing to type; never while the Claude code box is open.
_POLLING = (ls.REQUESTED, ls.STARTING, ls.CODE_SUBMITTED, ls.VERIFYING)


def _known(provider: str, account_id: str) -> bool:
    return any(c.provider == provider and c.account_id == account_id for c in ACCOUNT_CHOICES)


def _visible(row: AgentLoginSessionRow | None, now: datetime) -> AgentLoginSessionRow | None:
    if row is None or row.state in ls.ACTIVE or now - row.updated_at <= SHOW_FINISHED:
        return row
    return None


def panel_view(provider: str, account_id: str,
               row: AgentLoginSessionRow | None) -> dict[str, Any]:
    """Template data of one account's sign-in panel."""
    state = row.state if row else None
    return {"provider": provider, "account_id": account_id, "row": row, "state": state,
            "active": state in ls.ACTIVE,
            "polling": state in _POLLING or (state == ls.AWAITING and provider == "codex-sub"),
            "wants_code": state == ls.AWAITING and provider == "claude-sub",
            "url": f"{PAGE}/{provider}/{account_id}/login"}


def _back(flash: str, anchor: str = "") -> RedirectResponse:
    return RedirectResponse(f"{PAGE}?flash={quote_plus(flash)}{anchor}", status_code=303)


@router.get(PAGE)
async def subscriptions_page(request: Request) -> Response:
    if (r := _guard(request)):
        return r
    now = datetime.now(UTC).replace(tzinfo=None)
    accounts = []
    for acc in await account_logins(now):
        row = _visible(await ls.latest_session(acc.provider, acc.account_id), now)
        accounts.append({"acc": acc, "panel": panel_view(acc.provider, acc.account_id, row)})
    return render("subscriptions.html",
                  **_ctx(request, "subscriptions", ("Subscriptions", "Подписки")),
                  accounts=accounts, now=now)


@router.get(PAGE + "/{provider}/{account_id}/login")
async def subscription_login_panel(provider: str, account_id: str,
                                   request: Request) -> Response:
    if (r := _guard(request)):
        return r
    if not _known(provider, account_id):
        return Response(status_code=404)
    now = datetime.now(UTC).replace(tzinfo=None)
    row = _visible(await ls.latest_session(provider, account_id), now)
    return render("_login_panel.html", p=panel_view(provider, account_id, row))


@router.post(PAGE + "/{provider}/{account_id}/login")
async def subscription_login_start(
    provider: str, account_id: str, request: Request,
    _: OwnerSession = Depends(require_owner_session),
) -> RedirectResponse:
    if not _known(provider, account_id):
        return _back("!Unknown subscription account")
    session_id = await ls.start_session(provider, account_id)
    await audit(actor="dashboard", action="subscription.relogin",
                target=f"{provider}/{account_id} session={session_id}", ip=client_ip(request))
    return _back(f"Sign-in started for {provider}/{account_id}",
                 f"#acc-{provider}-{account_id}")


@router.post(PAGE + "/login/{session_id}/code")
async def subscription_login_code(
    session_id: int, request: Request, code: str = Form(""),
    _: OwnerSession = Depends(require_owner_session),
) -> RedirectResponse:
    problem = await ls.submit_code(session_id, code)
    # The code itself is never logged or audited - only that one was handed over.
    await audit(actor="dashboard", action="subscription.relogin.code",
                target=f"session={session_id} ok={problem is None}", ip=client_ip(request))
    return _back(f"!{problem}" if problem else "Code sent, the worker is checking it")


@router.post(PAGE + "/login/{session_id}/cancel")
async def subscription_login_cancel(
    session_id: int, request: Request,
    _: OwnerSession = Depends(require_owner_session),
) -> RedirectResponse:
    done = await ls.cancel_session(session_id)
    await audit(actor="dashboard", action="subscription.relogin.cancel",
                target=f"session={session_id}", ip=client_ip(request))
    return _back("Sign-in cancelled" if done else "!This sign-in had already finished")
