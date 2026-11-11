"""Runs a dashboard re-login inside the account's own worker, then re-checks the login.

Both apps use the same owner steps: open the sign-in link, sign in, paste one line back.
Claude: `claude auth login --claudeai` prints a link and reads the code on stdin. Codex:
`codex login` (browser flow) prints a link and listens on 127.0.0.1:1455 inside the worker;
the owner's browser is sent there after sign-in, cannot reach it, and the owner pastes that
address, which the worker replays locally. The pasted line arrives sealed to a key pair made
for this one login (login_seal.py).

Codex signs in to a STAGING home and its auth.json replaces the live one only after a
successful sign-in. Running `codex login` straight in the live CODEX_HOME deleted the working
login the moment the sign-in started (prod 2026-11-11 16:48: a cancelled re-login left the
worker logged out).
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
from collections.abc import Callable
from pathlib import Path

import httpx

from aibroker.agent import login_sessions as ls
from aibroker.agent.login_identity import LoginReport, codex_callback, parse_login_url, strip_ansi
from aibroker.agent.login_probe import codex_env, codex_overrides, kill_group, probe
from aibroker.agent.login_seal import new_key, public_pem, unseal
from aibroker.agent.login_status import record_status
from aibroker.agent.real_cli import cli_env
from aibroker.config import get_settings

log = logging.getLogger(__name__)
LINK_WAIT_S = 45.0          # the CLI prints its link within seconds
EXIT_WAIT_S = 120.0         # code exchange + writing the credentials
CALLBACK_TIMEOUT_S = 30.0
MAX_LOCAL_REDIRECTS = 3
SETTLE_S = 1.0
POLL_S = 1.0
_STEPS = {
    "claude-sub": "open the link, sign in, then paste the code here",
    "codex-sub": "open the link and sign in; then paste the full address of the page that "
                 "fails to open (http://127.0.0.1:1455/auth/callback?...) here",
}


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
            await kill_group(self.proc)
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
        await asyncio.sleep(POLL_S)
    return "expired"


async def _await_paste(session_id: int, provider: str, url: str) -> tuple[str | None, str | None]:
    """Publish the link and a one-time public key, then wait for the owner's pasted line.
    -> (secret, None) or (None, 'cancelled' | 'expired')."""
    key = new_key()
    if not await ls.advance(session_id, (ls.STARTING,), state=ls.AWAITING, verify_url=url,
                            public_key=public_pem(key), message=_STEPS[provider]):
        return None, "cancelled"
    loop = asyncio.get_running_loop()
    deadline = loop.time() + get_settings().AGENT_LOGIN_CODE_WAIT_S
    while loop.time() < deadline:
        if await _cancelled(session_id):
            return None, "cancelled"
        if sealed := await ls.take_code(session_id):
            return unseal(key, sealed), None
        await asyncio.sleep(POLL_S)
    return None, "expired"


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
        code, stopped = await _await_paste(session_id, "claude-sub", parse_login_url(out.text) or "")
        if code is None:
            return stopped
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


async def _replay_callback(url: str) -> str | None:
    """GET the pasted callback against the CLI's local listener, then follow its redirects on
    that SAME listener. `codex login` saves auth.json, answers 302 to its own /success page and
    exits only once /success is requested (codex-rs/login/src/server.rs); not following it left
    the CLI waiting forever (prod 2026-11-11 session 4). -> an error text, or None."""
    origin = httpx.URL(url)
    try:
        async with httpx.AsyncClient(timeout=CALLBACK_TIMEOUT_S, trust_env=False) as client:
            response = await client.get(url)
            for _ in range(MAX_LOCAL_REDIRECTS):
                if not response.is_redirect:
                    break
                target = origin.join(response.headers.get("location", ""))
                if (target.host, target.port) != (origin.host, origin.port):
                    break           # a redirect off the local listener is never followed
                response = await client.get(target)
    except httpx.HTTPError as exc:
        return f"the local sign-in listener did not answer: {exc.__class__.__name__}"
    return None


async def _await_credentials(out: _Output, staging: Path, session_id: int) -> str | None:
    """After the callback: wait until the CLI exits OR its auth.json is written (it is saved
    before the 302, so a CLI still waiting for /success must not cost the sign-in).
    -> None when ready to install, else why not."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + EXIT_WAIT_S
    while loop.time() < deadline:
        if out.proc.returncode is not None:
            return None
        if (staging / "auth.json").is_file():
            await asyncio.sleep(SETTLE_S)      # the 302 follows the completed write; be safe
            return None
        if await _cancelled(session_id):
            return "cancelled"
        await asyncio.sleep(POLL_S)
    return f"the CLI did not finish after the sign-in: {out.tail()}"


def _install(staging: Path, live: Path) -> str | None:
    """Move the freshly written auth.json over the live one (same volume, atomic)."""
    new = staging / "auth.json"
    if not new.is_file():
        return "the CLI finished but wrote no credentials"
    os.chmod(new, 0o600)
    os.replace(new, live / "auth.json")
    return None


async def _codex(session_id: int) -> str | None:
    home = os.environ.get("CODEX_HOME", "")
    if not home:
        return "CODEX_HOME is not set on this worker"
    live = Path(home)
    staging = live / f".login-{session_id}"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(mode=0o700, parents=True)
    proc = await asyncio.create_subprocess_exec(
        "codex", "login", *codex_overrides(), stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        env={**codex_env(), "CODEX_HOME": str(staging)}, cwd=str(staging),
        start_new_session=True)
    out = _Output(proc)
    pasted: str | None = None
    try:
        if not await out.until(parse_login_url, LINK_WAIT_S):
            return f"the CLI printed no sign-in link: {out.tail()}"
        pasted, stopped = await _await_paste(session_id, "codex-sub",
                                             parse_login_url(out.text) or "")
        if pasted is None:
            return stopped
        callback = codex_callback(pasted)
        if callback is None:
            return ("that is not the http://127.0.0.1:1455/auth/callback?... address the "
                    "browser was sent to after sign-in")
        if problem := await _replay_callback(callback):
            return problem
        if stopped := await _await_credentials(out, staging, session_id):
            return stopped
        if not (staging / "auth.json").is_file() and proc.returncode not in (None, 0):
            return f"sign-in did not complete: {out.tail(pasted)}"
        return _install(staging, live)
    finally:
        await out.stop()
        shutil.rmtree(staging, ignore_errors=True)


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
    elif stopped is None and report.failed:
        await ls.finish(session_id, ls.DONE,
                        "signed in; the login check could not confirm it yet")
    else:
        await ls.finish(session_id, ls.FAILED, stopped or report.detail or "still not signed in")
    return report
