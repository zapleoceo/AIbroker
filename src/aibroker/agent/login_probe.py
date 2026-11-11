"""Asks the worker's own CLI whether its subscription login works.

Claude: `claude auth status` (JSON). Codex: `account/read` over `codex app-server`, the
official account query, so the mailbox and plan are known without opening auth.json.
Both run with the same minimal environment as a real run (no DB URL, no broker secret).
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
from collections.abc import Mapping
from typing import Any

from aibroker.agent.codex_cli import CODEX_ENV_KEYS, CONFIG_OVERRIDES
from aibroker.agent.login_identity import LoginReport, parse_claude_status, parse_codex_account
from aibroker.agent.real_cli import cli_env

PROBE_TIMEOUT_S = 40.0
_INITIALIZE = {"id": 1, "method": "initialize", "params": {
    "clientInfo": {"name": "aibroker", "title": "AIbroker", "version": "1"},
    "capabilities": {"experimentalApi": True}}}


def codex_env(source: Mapping[str, str] | None = None) -> dict[str, str]:
    src = os.environ if source is None else source
    return {k: src[k] for k in CODEX_ENV_KEYS if k in src}


def codex_overrides() -> list[str]:
    """`-c` pairs that make a login/probe use the runs' file credential store and ChatGPT-only
    login (never an API key)."""
    out: list[str] = []
    for override in CONFIG_OVERRIDES:
        if override.startswith(("cli_auth_credentials_store", "forced_login_method")):
            out += ["-c", override]
    return out


async def kill_group(proc: asyncio.subprocess.Process) -> None:
    """Kill the CLI and everything it spawned. `codex app-server` starts helpers that inherit our
    pipes; killing only the main pid left them open, so `wait()` never returned (prod
    2026-11-11: every Codex probe hit the timeout although the answer came in < 1 s)."""
    with contextlib.suppress(ProcessLookupError, PermissionError, OSError, AttributeError):
        os.killpg(proc.pid, signal.SIGKILL)   # AttributeError: no process groups on Windows
    with contextlib.suppress(ProcessLookupError):
        proc.kill()
    with contextlib.suppress(Exception):
        await asyncio.wait_for(proc.wait(), 5)


async def probe_claude(binary: str = "claude") -> LoginReport:
    proc = await asyncio.create_subprocess_exec(
        binary, "auth", "status", stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, env=cli_env(),
        start_new_session=True)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=PROBE_TIMEOUT_S)
    except TimeoutError:
        await kill_group(proc)
        return LoginReport(False, detail="`claude auth status` timed out", failed=True)
    return parse_claude_status(out.decode(errors="replace"))


async def _codex_account(binary: str) -> dict[str, Any] | None:
    proc = await asyncio.create_subprocess_exec(
        binary, "app-server", *codex_overrides(), stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        env=codex_env(), cwd="/tmp", start_new_session=True)
    assert proc.stdin is not None and proc.stdout is not None
    try:
        for message in (_INITIALIZE, {"method": "initialized", "params": {}},
                        {"id": 2, "method": "account/read", "params": {}}):
            proc.stdin.write((json.dumps(message) + "\n").encode())
        await proc.stdin.drain()
        while raw := await proc.stdout.readline():
            with contextlib.suppress(ValueError):
                message = json.loads(raw)
                if isinstance(message, dict) and message.get("id") == 2 and "method" not in message:
                    result = message.get("result")
                    return result if isinstance(result, dict) else None
        return None
    finally:
        await kill_group(proc)


async def probe_codex(binary: str = "codex") -> LoginReport:
    try:
        result = await asyncio.wait_for(_codex_account(binary), timeout=PROBE_TIMEOUT_S)
    except TimeoutError:
        return LoginReport(False, detail="`codex app-server` account/read timed out", failed=True)
    except OSError as exc:
        return LoginReport(False, detail=f"codex not runnable: {exc.__class__.__name__}",
                           failed=True)
    if result is None:
        return LoginReport(False, detail="`codex app-server` gave no account/read answer",
                           failed=True)
    return parse_codex_account(result)


async def probe(provider: str) -> LoginReport:
    return await (probe_claude() if provider == "claude-sub" else probe_codex())
