"""What a subscription CLI says about its own login, parsed from its own output.

Pure functions only: the worker runs the CLI (login_probe.py), the dashboard and the monitor
render the result. Nothing here reads a credential file - only the CLI's own status output.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

APP_LABEL = {"claude-sub": "Claude Code", "codex-sub": "Codex"}
_NAME_PREFIX = {"claude-sub": "CLD", "codex-sub": "GPT"}
_NAME_LETTERS = 4
_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_URL = re.compile(r"https://[^\s\x1b'\"<>]+")
# `codex login` (browser flow) listens here inside the worker; the owner's browser is sent to
# this address after sign-in and cannot reach it, so its full address is pasted instead.
CODEX_CALLBACK_PORT = 1455
CODEX_CALLBACK_PATH = "/auth/callback"


@dataclass(frozen=True)
class LoginReport:
    """`failed`: the CLI could not be asked at all (timeout, no answer). That says nothing about
    the login, so it is never stored as "logged out" - no false alert."""
    logged_in: bool
    email: str | None = None
    plan: str | None = None
    detail: str | None = None
    failed: bool = False


def display_name(provider: str, email: str | None, account_id: str) -> str:
    """CLD_ZAPO / GPT_ZAPO: app prefix + the first letters of the mailbox login. Falls back to
    the worker id while no mailbox is known yet (a login that never reported one)."""
    prefix = _NAME_PREFIX.get(provider, provider.split("-", 1)[0].upper())
    login = (email or "").split("@", 1)[0]
    letters = "".join(ch for ch in login if ch.isalnum())[:_NAME_LETTERS].upper()
    return f"{prefix}_{letters or account_id}"


def strip_ansi(text: str) -> str:
    return _ANSI.sub("", text)


def _clean(value: Any, limit: int) -> str | None:
    return value.strip()[:limit] if isinstance(value, str) and value.strip() else None


def parse_claude_status(output: str) -> LoginReport:
    """`claude auth status` prints one JSON object (CLI 2.1.296 and 2.1.322; 2.1.322 adds `loginExpiresAt`). Anything else is a probe
    problem, reported as logged out with the reason in `detail`."""
    text = strip_ansi(output).strip()
    try:
        data = json.loads(text[text.find("{"):text.rfind("}") + 1])
    except ValueError:
        return LoginReport(False, detail="unreadable `claude auth status` output")
    if not isinstance(data, dict):
        return LoginReport(False, detail="unreadable `claude auth status` output")
    logged_in = data.get("loggedIn") is True and data.get("authMethod") == "claude.ai"
    plan = _clean(data.get("subscriptionType"), 64)
    org = _clean(data.get("orgName"), 120)
    detail = None if logged_in else f"not logged in (authMethod={data.get('authMethod')})"
    return LoginReport(logged_in, _clean(data.get("email"), 320),
                       f"{plan} · {org}" if plan and org else plan, detail)


def parse_codex_account(result: dict[str, Any] | None) -> LoginReport:
    """The `account/read` result of `codex app-server`: a ChatGPT login has
    {"account": {"type": "chatgpt", "email", "planType"}}; logged out has no account."""
    account = (result or {}).get("account")
    if not isinstance(account, dict) or account.get("type") != "chatgpt":
        return LoginReport(False, detail="no ChatGPT login on this worker")
    return LoginReport(True, _clean(account.get("email"), 320),
                       _clean(account.get("planType"), 64))


def parse_login_url(output: str) -> str | None:
    """The sign-in link a login command prints (`claude auth login`, `codex login
    --device-auth`). Only https links on the vendor's own auth hosts are accepted."""
    for url in _URL.findall(strip_ansi(output)):
        host = url.split("/", 3)[2] if url.count("/") >= 2 else ""
        if host in ("claude.com", "claude.ai", "auth.openai.com"):
            return url
    return None


def codex_callback(pasted: str) -> str | None:
    """The local callback to replay inside the worker, rebuilt from the pasted address: only
    http on 127.0.0.1/localhost:1455 and the callback path pass, and only its query is kept,
    so a pasted address can never point the worker at another host. None when it does not fit."""
    try:
        parts = urlsplit(pasted.strip())
    except ValueError:
        return None
    if (parts.scheme != "http" or parts.hostname not in ("127.0.0.1", "localhost")
            or parts.port != CODEX_CALLBACK_PORT or parts.path != CODEX_CALLBACK_PATH
            or "code=" not in parts.query):
        return None
    return f"http://127.0.0.1:{CODEX_CALLBACK_PORT}{CODEX_CALLBACK_PATH}?{parts.query}"
