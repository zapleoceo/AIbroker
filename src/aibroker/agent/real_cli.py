"""Real adapter: ClaudeCLI drives the OFFICIAL Claude Code CLI as a subprocess.

UNVERIFIED against the real CLI (phase 4): argv and parser come from the public docs
only. Nothing here runs unless AGENT_WORKER_ENABLED registers it (register_real_adapters).
Codex / model catalog / multi-account are later work.

Safety, by construction:
- permission safeguards stay ON: no --dangerously-skip-permissions, no bypass flags (a test
  scans every argv for them); allowed tools are passed explicitly;
- `--model <id>` is passed through exactly; `--resume <id>` only with an exact session id,
  never "continue" / "last";
- the prompt goes through stdin, not argv;
- credentials are never read or handled here: the CLI uses its own subscription login.
"""
from __future__ import annotations

import asyncio
import contextlib
import os
from collections.abc import AsyncIterator, Awaitable, Callable

from aibroker.agent import events as ev
from aibroker.agent.adapter import AgentRunSpec, BillingMode, ModelInfo, StreamEvent
from aibroker.agent.parsers import ClaudeParser, parse_claude_auth_status
from aibroker.agent.registry import register_adapter

Probe = Callable[[list[str]], Awaitable[str]]


def build_claude_argv(run: AgentRunSpec, binary: str = "claude") -> list[str]:
    """`claude -p --output-format stream-json --verbose`; tools allowed explicitly only."""
    argv = [binary, "-p", "--output-format", "stream-json", "--verbose"]
    if run.model:
        argv += ["--model", run.model]
    if run.session_id:
        argv += ["--resume", run.session_id]
    if run.allowed_tools:
        argv += ["--allowedTools", *run.allowed_tools]
    return argv


async def _run_probe(argv: list[str]) -> str:  # pragma: no cover - spawns the real CLI
    proc = await asyncio.create_subprocess_exec(
        *argv, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT)
    out, _ = await asyncio.wait_for(proc.communicate(), timeout=20)
    return out.decode(errors="replace")


class ClaudeCLI:
    """Anthropic Claude Code CLI (`claude -p`). UNVERIFIED against the real CLI."""
    supports_resume = True

    def __init__(self, binary: str = "claude", probe: Probe | None = None,
                 kill_grace_s: float = 5.0) -> None:
        self.binary = binary
        self._probe = probe or _run_probe
        self.kill_grace_s = kill_grace_s
        self._proc: asyncio.subprocess.Process | None = None
        self._stderr = bytearray()

    def build_argv(self, run: AgentRunSpec) -> list[str]:
        return build_claude_argv(run, self.binary)

    async def billing_mode(self) -> BillingMode:
        """`claude auth status`; anything but a positive claude.ai login is not subscription."""
        try:
            return parse_claude_auth_status(await self._probe([self.binary, "auth", "status"]))
        except Exception:  # noqa: BLE001 - cannot tell -> unknown -> the guard refuses
            return "unknown"

    async def list_models(self) -> list[ModelInfo] | None:
        return None   # catalog from the CLI itself is later work

    async def cancel(self) -> None:
        """SIGTERM, wait `kill_grace_s`, then SIGKILL. Idempotent."""
        proc = self._proc
        if proc is None or proc.returncode is not None:
            return
        with contextlib.suppress(ProcessLookupError):
            proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=self.kill_grace_s)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            await proc.wait()

    async def _drain_stderr(self, proc: asyncio.subprocess.Process) -> None:
        assert proc.stderr is not None
        while chunk := await proc.stderr.read(4096):
            self._stderr.extend(chunk)
            del self._stderr[:-8192]

    async def start(self, run: AgentRunSpec) -> AsyncIterator[StreamEvent]:
        proc = await asyncio.create_subprocess_exec(
            *self.build_argv(run), stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, cwd=run.workspace, env=dict(os.environ), limit=1 << 24)
        self._proc = proc
        stderr_task = asyncio.create_task(self._drain_stderr(proc))
        parser = ClaudeParser(run.workspace)
        try:
            assert proc.stdin is not None and proc.stdout is not None
            proc.stdin.write(run.prompt.encode())
            await proc.stdin.drain()
            proc.stdin.close()
            async for raw in proc.stdout:
                for item in parser.feed(raw.decode(errors="replace")):
                    yield item
                    if item.type in ev.TERMINAL_TYPES:
                        return
            rc = await proc.wait()
            await stderr_task
            yield parser.finish(rc, self._stderr.decode(errors="replace"))
        finally:
            await self.cancel()
            stderr_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await stderr_task


def register_real_adapters() -> None:
    """Called by the worker lane at startup (only when AGENT_WORKER_ENABLED)."""
    register_adapter("agent:claude", ClaudeCLI)
