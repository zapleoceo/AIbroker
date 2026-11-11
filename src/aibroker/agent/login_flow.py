"""Runs a dashboard re-login inside the account's own worker, then re-checks the login.

Claude: `claude auth login --claudeai` prints a sign-in link and waits for the code on stdin;
the code arrives sealed to a key pair made for this one login (login_seal.py). Codex:
`codex login --device-auth` prints a link and a one-time code the owner types into the
browser; the CLI finishes by itself, so nothing secret travels through the dashboard at all.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable

from aibroker.agent import login_sessions as ls
from aibroker.agent.login_identity import (
    LoginReport,
    parse_device_code,
    parse_login_url,
    strip_ansi,
)
from aibroker.agent.login_probe import codex_env, codex_overrides, probe
from aibroker.agent.login_seal import new_key, public_pem, unseal
from aibroker.agent.login_status import record_status
from aibroker.agent.real_cli import cli_env
from aibroker.config import get_settings

log = logging.getLogger(__name__)
LINK_WAIT_S = 45.0          # the CLI prints its link within seconds
EXIT_WAIT_S = 120.0         # code exchange + writing the credentials
DEVICE_WAIT_S = 15 * 60.0   # codex: "expires in 15 minutes"
POLL_S = 1.0


class _Output:
    """Drains a process's merged stdout/stderr in the background: a CLI that blocks on stdin
    after printing must never also block on a full pipe."""

    def __init__(self, proc: asyncio.subprocess.Process) -> None:
        self.proc = proc
        self.text = ""
        self._task = asyncio.create_task(self._drain())

    async def _drain(self) -> None:
        assert self.proc.stdout is not None
        while chunk := await self.proc.stdout.read(4096):
            self.text = (self.text + chunk.decode(errors="replace"))[-20000:]

    async def until(self, found: Callable[[str], object], timeout: float) -> bool:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            if found(self.text):
                return True
            if self.proc.returncode is not None or self._task.done():
                return bool(found(self.text))
            await asyncio.sleep(0.2)
        return False

    def tail(self, secret: str | None = None) -> str:
        text = " ".join(strip_ansi(self.text).split())
        return (text.replace(secret, "***") if secret else text)[-240:]

    async def stop(self) -> None:
        if self.proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                self.proc.kill()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(self.proc.wait(), 10)
        self._task.cancel()


async def _cancelled(session_id: int) -> bool:
    row = await ls.get_login(session_id)
    return row is None or row.state not in ls.ACTIVE


async def _wait_exit(out: _Output, session_id: int, timeout: float) -> str | None:
    """None once the CLI exited; else 'cancelled' or 'expired'."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if out.proc.returncode is not None:
            return None
        if await _cancelled(session_id):
            return "cancelled"
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(asyncio.shield(out.proc.wait()), POLL_S)
    return "expired"


async def _await_code(session_id: int) -> str | None:
    """The sealed code once the owner submits it; None when cancelled or out of time."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + get_settings().AGENT_LOGIN_CODE_WAIT_S
    while loop.time() < deadline:
        if await _cancelled(session_id):
            return None
        if sealed := await ls.take_code(session_id):
            return sealed
        await asyncio.sleep(POLL_S)
    return None


async def _claude(session_id: int, email: str | None) -> str | None:
    """-> None when the CLI finished, else why it stopped. stdin is not a terminal, so the
    code is never echoed into the output; it is scrubbed from the reported tail anyway."""
    argv = ["claude", "auth", "login", "--claudeai"] + (["--email", email] if email else [])
    proc = await asyncio.create_subprocess_exec(
        *argv, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT, env=cli_env(), start_new_session=True)
    out = _Output(proc)
    try:
        if not await out.until(parse_login_url, LINK_WAIT_S):
            return f"the CLI printed no sign-in link: {out.tail()}"
        key = new_key()
        if not await ls.advance(session_id, (ls.STARTING,), state=ls.AWAITING,
                                verify_url=parse_login_url(out.text), public_key=public_pem(key),
                                message="open the link, sign in, then paste the code here"):
            return "cancelled"
        sealed = await _await_code(session_id)
        if sealed is None:
            return "cancelled" if await _cancelled(session_id) else "expired"
        code = unseal(key, sealed)
        assert proc.stdin is not None
        proc.stdin.write((code + "\n").encode())
        await proc.stdin.drain()
        proc.stdin.close()
        stopped = await _wait_exit(out, session_id, EXIT_WAIT_S)
        if stopped or proc.returncode == 0:
            return stopped
        return f"the CLI refused the code: {out.tail(code)}"
    finally:
        await out.stop()


async def _codex(session_id: int) -> str | None:
    proc = await asyncio.create_subprocess_exec(
        "codex", "login", "--device-auth", *codex_overrides(), stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, env=codex_env(),
        cwd="/tmp", start_new_session=True)
    out = _Output(proc)
    try:
        if not await out.until(lambda t: parse_login_url(t) and parse_device_code(t),
                               LINK_WAIT_S):
            return f"the CLI printed no device code: {out.tail()}"
        if not await ls.advance(session_id, (ls.STARTING,), state=ls.AWAITING,
                                verify_url=parse_login_url(out.text),
                                user_code=parse_device_code(out.text),
                                message="open the link, sign in and type the code shown here"):
            return "cancelled"
        stopped = await _wait_exit(out, session_id, DEVICE_WAIT_S)
        if stopped or proc.returncode == 0:
            return stopped
        return f"sign-in did not complete: {out.tail()}"
    finally:
        await out.stop()


async def run_login(provider: str, account_id: str, session_id: int,
                    email: str | None) -> LoginReport:
    """Carry one session to a terminal state; the login is always re-probed afterwards."""
    try:
        stopped = await (_claude(session_id, email) if provider == "claude-sub"
                         else _codex(session_id))
    except Exception as exc:  # noqa: BLE001 - any failure must still end the session
        log.exception("login session %s failed", session_id)
        stopped = f"the worker failed: {exc.__class__.__name__}"
    report = await probe(provider)
    await record_status(provider, account_id, report)
    if stopped == "expired":
        await ls.finish(session_id, ls.EXPIRED, "no sign-in within the time limit")
    elif stopped == "cancelled":
        await ls.finish(session_id, ls.CANCELLED, "cancelled")
    elif report.logged_in:
        await ls.finish(session_id, ls.DONE, f"signed in as {report.email or 'unknown'}")
    else:
        await ls.finish(session_id, ls.FAILED, stopped or report.detail or "still not signed in")
    return report
