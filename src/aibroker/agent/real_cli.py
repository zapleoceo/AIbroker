"""Real adapter: ClaudeCLI drives the OFFICIAL Claude Code CLI as a subprocess.

UNVERIFIED against the real CLI (phase 4): argv and parser come from the public docs only
(plus `claude --help` / `claude auth status` of the pinned image). Nothing here runs unless
AGENT_WORKER_ENABLED registers it (register_real_adapters). Codex / catalog / multi-account
are later work.

Safety, by construction:
- permission safeguards stay ON: no --dangerously-skip-permissions, no bypass flags; the
  allowed tools are an allow-list (agent.tools), passed as ONE `--allowedTools=A,B` argument,
  plus an always-on `--disallowedTools=` for /proc and the CLI auth dir; every option uses the
  `--opt=value` form, so no caller-controlled value can ever become a separate flag, and there
  are no positional arguments at all (the prompt goes on stdin);
- `--model=<id>` is passed exactly; `--resume=<id>` only with an exact session id;
- the CLI and the auth probe get a MINIMAL environment (cli_env), never the worker's secrets;
- settings files are validated before every run (agent.settings_guard); only the `user`
  setting source is loaded (`--setting-sources=user`);
- the CLI runs in its own process group and cancel kills the whole group (SIGTERM, grace,
  SIGKILL), so tool children cannot outlive a cancelled or timed-out run;
- credentials are never read or handled here: the CLI uses its own subscription login.
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import signal
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping

from aibroker.agent import events as ev
from aibroker.agent.adapter import AgentRunSpec, BillingMode, ModelInfo, StreamEvent
from aibroker.agent.parsers import ClaudeParser, parse_claude_auth_status
from aibroker.agent.registry import register_adapter
from aibroker.agent.settings_guard import settings_problem
from aibroker.agent.tools import disallowed_tools, tool_names, validate_tools

Probe = Callable[[list[str], Mapping[str, str]], Awaitable[str]]

ATTACHMENT_READ = "Read(inputs/**)"
CLI_ENV_KEYS = ("PATH", "HOME", "CLAUDE_CONFIG_DIR", "DISABLE_AUTOUPDATER", "LANG", "TMPDIR")


def cli_env(source: Mapping[str, str] | None = None) -> dict[str, str]:
    """The ONLY environment the CLI and its auth probe ever see (no DB URL, no broker secret)."""
    src = os.environ if source is None else source
    env = {k: src[k] for k in CLI_ENV_KEYS if k in src}
    env.setdefault("DISABLE_AUTOUPDATER", "1")
    return env


def build_claude_argv(run: AgentRunSpec, binary: str = "claude",
                      config_dir: str | None = None) -> list[str]:
    """`claude -p --output-format stream-json --verbose` with validated, `=`-joined options.
    ValueError when a requested tool is not acceptable."""
    tools = list(run.allowed_tools)
    validate_tools(tools)
    if run.attachments and ATTACHMENT_READ not in tools:
        tools.append(ATTACHMENT_READ)   # scoped: the CLI may open ONLY the attached files
    argv = [binary, "-p", "--output-format", "stream-json", "--verbose",
            "--setting-sources=user", "--strict-mcp-config"]
    # Restrict the built-in tool set itself to the requested ones (empty = no tools at all).
    argv += [f"--tools={','.join(tool_names(tools))}"] if tools else ["--tools", ""]
    if run.model:
        argv.append(f"--model={run.model}")
    if run.session_id:
        argv.append(f"--resume={run.session_id}")
    if tools:
        argv.append("--allowedTools=" + ",".join(tools))
    argv.append("--disallowedTools=" + ",".join(disallowed_tools(config_dir)))
    return argv


def build_claude_prompt(run: AgentRunSpec) -> str:
    """The prompt, plus the REAL attached files named by path (read them with Read)."""
    if not run.attachments:
        return run.prompt
    listing = "\n".join(f"- {p}" for p in run.attachments)
    return (f"{run.prompt}\n\nAttached input files (real files in the working directory; "
            f"open them with the Read tool):\n{listing}\n")


async def _run_probe(argv: list[str], env: Mapping[str, str]) -> str:  # pragma: no cover - real CLI
    proc = await asyncio.create_subprocess_exec(
        *argv, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT, env=dict(env))
    out, _ = await asyncio.wait_for(proc.communicate(), timeout=20)
    return out.decode(errors="replace")


class ClaudeCLI:
    """Anthropic Claude Code CLI (`claude -p`). UNVERIFIED against the real CLI."""
    supports_resume = True

    def __init__(self, binary: str = "claude", probe: Probe | None = None,
                 kill_grace_s: float = 5.0, env: Mapping[str, str] | None = None) -> None:
        self.binary = binary
        self._probe = probe or _run_probe
        self.kill_grace_s = kill_grace_s
        self._env = cli_env(env)
        self._proc: asyncio.subprocess.Process | None = None
        self._stderr = bytearray()

    def build_argv(self, run: AgentRunSpec) -> list[str]:
        return build_claude_argv(run, self.binary, self._env.get("CLAUDE_CONFIG_DIR"))

    async def billing_mode(self) -> BillingMode:
        """`claude auth status`; anything but a positive claude.ai login is not subscription."""
        try:
            return parse_claude_auth_status(
                await self._probe([self.binary, "auth", "status"], self._env))
        except Exception:  # noqa: BLE001 - cannot tell -> unknown -> the guard refuses
            return "unknown"

    async def list_models(self) -> list[ModelInfo] | None:
        return None   # catalog from the CLI itself is later work

    def _signal_group(self, sig: int) -> None:
        proc = self._proc
        if proc is None:
            return
        with contextlib.suppress(ProcessLookupError, PermissionError):
            if hasattr(os, "killpg"):
                os.killpg(proc.pid, sig)          # the CLI leads its own session/group
            elif sig == signal.SIGTERM:
                proc.terminate()
            else:
                proc.kill()

    async def cancel(self) -> None:
        """SIGTERM to the whole process group, wait `kill_grace_s`, then SIGKILL. Idempotent."""
        proc = self._proc
        if proc is None or proc.returncode is not None:
            return
        self._signal_group(signal.SIGTERM)
        try:
            await asyncio.wait_for(proc.wait(), timeout=self.kill_grace_s)
        except TimeoutError:
            self._signal_group(getattr(signal, "SIGKILL", signal.SIGTERM))
            await proc.wait()

    async def _drain_stderr(self, proc: asyncio.subprocess.Process) -> None:
        assert proc.stderr is not None
        while chunk := await proc.stderr.read(4096):
            self._stderr.extend(chunk)
            del self._stderr[:-8192]

    async def start(self, run: AgentRunSpec) -> AsyncIterator[StreamEvent]:
        problem = settings_problem(self._env.get("CLAUDE_CONFIG_DIR"), run.workspace)
        if problem:
            yield ev.RunFailed(error_class="settings_refused", message=problem[:500])
            return
        try:
            argv = self.build_argv(run)
        except ValueError as exc:
            yield ev.RunFailed(error_class="tool_invalid", message=str(exc)[:500])
            return
        proc = await asyncio.create_subprocess_exec(
            *argv, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, cwd=run.workspace, env=dict(self._env),
            limit=1 << 24, start_new_session=True)
        self._proc = proc
        stderr_task = asyncio.create_task(self._drain_stderr(proc))
        parser = ClaudeParser(run.workspace)
        try:
            assert proc.stdin is not None and proc.stdout is not None
            proc.stdin.write(build_claude_prompt(run).encode())
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
            self._signal_group(getattr(signal, "SIGKILL", signal.SIGTERM))   # stray children
            stderr_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await stderr_task


def register_real_adapters() -> None:
    """Called by the worker lane at startup (only when AGENT_WORKER_ENABLED)."""
    register_adapter("agent:claude", ClaudeCLI)
