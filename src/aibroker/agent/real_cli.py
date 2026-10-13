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
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Mapping
from typing import Any

from aibroker.agent import events as ev
from aibroker.agent.adapter import (
    AgentRunSpec,
    AuthAttestation,
    BillingMode,
    ModelInfo,
    StreamEvent,
)
from aibroker.agent.events import FileRef
from aibroker.agent.parsers import ClaudeParser, parse_claude_auth_details, parse_claude_auth_status
from aibroker.agent.registry import register_adapter
from aibroker.agent.settings_guard import settings_problem
from aibroker.agent.tools import disallowed_tools, tool_names, validate_tools

Probe = Callable[[list[str], Mapping[str, str]], Awaitable[str]]

ATTACHMENT_READ = "Read(inputs/**)"
MCP_ALLOW = "mcp__broker__*"     # server name "broker" is fixed in claude_tools.SERVER_NAME
# Bare names remove a tool from the model's context; belt and braces next to `--tools ""`.
BUILTIN_DENY = ("Bash", "Read", "Write", "Edit", "MultiEdit", "NotebookEdit", "Glob", "Grep",
                "WebFetch", "WebSearch", "Agent")
CLI_ENV_KEYS = ("PATH", "HOME", "CLAUDE_CONFIG_DIR", "DISABLE_AUTOUPDATER", "LANG", "TMPDIR")


def cli_env(source: Mapping[str, str] | None = None) -> dict[str, str]:
    """The ONLY environment the CLI and its auth probe ever see (no DB URL, no broker secret)."""
    src = os.environ if source is None else source
    env = {k: src[k] for k in CLI_ENV_KEYS if k in src}
    env.setdefault("DISABLE_AUTOUPDATER", "1")
    return env


def build_claude_argv(run: AgentRunSpec, binary: str = "claude",
                      config_dir: str | None = None, mcp_config: str | None = None) -> list[str]:
    """`claude -p --output-format stream-json --verbose` with validated, `=`-joined options.
    ValueError when a requested tool is not acceptable. With `mcp_config` (client tools) the
    broker MCP server is the ONLY tool source: no built-in tool, only `mcp__broker__*` allowed."""
    tools = list(run.allowed_tools)
    validate_tools(tools)
    if mcp_config and tools:
        raise ValueError("client tools exclude CLI tools")
    if run.attachments and ATTACHMENT_READ not in tools:
        tools.append(ATTACHMENT_READ)   # scoped: the CLI may open ONLY the attached files
    argv = [binary, "-p", "--output-format", "stream-json", "--verbose",
            "--setting-sources=user", "--strict-mcp-config"]
    if mcp_config:
        argv += ["--mcp-config", mcp_config]
    # Restrict the built-in tool set itself to the requested ones (empty = no tools at all).
    argv += [f"--tools={','.join(tool_names(tools))}"] if tools else ["--tools", ""]
    if run.model:
        argv.append(f"--model={run.model}")
    if run.session_id:
        argv.append(f"--resume={run.session_id}")
    if tools:
        argv.append("--allowedTools=" + ",".join(tools))
    elif mcp_config:
        argv.append(f"--allowedTools={MCP_ALLOW}")
    denied = disallowed_tools(config_dir) + (list(BUILTIN_DENY) if mcp_config else [])
    argv.append("--disallowedTools=" + ",".join(denied))
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


class SubprocessCLI:
    """Shared subprocess plumbing of the real adapters (Claude, Codex): own process group, group
    kill on cancel, stderr tail, JSON-lines parser loop. Subclasses give the argv, the parser,
    the stdin payload, the environment and the billing probe."""
    supports_resume = True

    def __init__(self, binary: str, probe: Probe | None = None, kill_grace_s: float = 5.0,
                 env: Mapping[str, str] | None = None) -> None:
        self.binary = binary
        self._probe = probe or _run_probe
        self.kill_grace_s = kill_grace_s
        self._env = self.make_env(env)
        self._auth: AuthAttestation | None = None
        self._proc: asyncio.subprocess.Process | None = None
        self._stderr = bytearray()

    # --- hooks -------------------------------------------------------------------------------
    def make_env(self, source: Mapping[str, str] | None) -> dict[str, str]:
        raise NotImplementedError

    def build_argv(self, run: AgentRunSpec) -> list[str]:
        raise NotImplementedError

    def new_parser(self, run: AgentRunSpec) -> Any:
        raise NotImplementedError

    def stdin_payload(self, run: AgentRunSpec) -> str:
        return run.prompt

    def preflight(self, run: AgentRunSpec) -> tuple[str, str] | None:
        """(error_class, message) when the run must be refused before any process starts."""
        return None

    async def preflight_async(self, run: AgentRunSpec) -> tuple[str, str] | None:
        """Like preflight, for checks that run a process (e.g. the sandbox self-test)."""
        return None

    def process_env(self, run: AgentRunSpec) -> dict[str, str]:
        """The environment of THIS run's process (default: the adapter's minimal env)."""
        return dict(self._env)

    # --- shared ------------------------------------------------------------------------------
    def attestation(self) -> AuthAttestation | None:
        """Method/provider of the last successful subscription probe (None otherwise)."""
        return self._auth

    def export_artifacts(self, run: AgentRunSpec, since: float) -> list[FileRef]:
        return []

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
        problem = self.preflight(run) or await self.preflight_async(run)
        if problem:
            yield ev.RunFailed(error_class=problem[0], message=problem[1][:500])
            return
        try:
            argv = self.build_argv(run)
        except ValueError as exc:
            yield ev.RunFailed(error_class="tool_invalid", message=str(exc)[:500])
            return
        async for item in self.exec_argv(argv, run):
            yield item

    async def exec_argv(self, argv: list[str], run: AgentRunSpec) -> AsyncIterator[StreamEvent]:
        """Spawn `argv` in its own process group and stream the parsed events."""
        proc = await asyncio.create_subprocess_exec(
            *argv, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, cwd=run.workspace, env=self.process_env(run),
            limit=1 << 24, start_new_session=True)
        self._proc = proc
        stderr_task = asyncio.create_task(self._drain_stderr(proc))
        parser = self.new_parser(run)
        try:
            assert proc.stdin is not None and proc.stdout is not None
            proc.stdin.write(self.stdin_payload(run).encode())
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


class ClaudeCLI(SubprocessCLI):
    """Anthropic Claude Code CLI (`claude -p`). UNVERIFIED against the real CLI."""

    def __init__(self, binary: str = "claude", probe: Probe | None = None,
                 kill_grace_s: float = 5.0, env: Mapping[str, str] | None = None) -> None:
        super().__init__(binary, probe, kill_grace_s, env)

    def make_env(self, source: Mapping[str, str] | None) -> dict[str, str]:
        return cli_env(source)

    def build_argv(self, run: AgentRunSpec) -> list[str]:
        return build_claude_argv(run, self.binary, self._env.get("CLAUDE_CONFIG_DIR"))

    def new_parser(self, run: AgentRunSpec) -> ClaudeParser:
        return ClaudeParser(run.workspace)

    def stdin_payload(self, run: AgentRunSpec) -> str:
        return build_claude_prompt(run)

    async def start(self, run: AgentRunSpec) -> AsyncIterator[StreamEvent]:
        if run.client_tools and run.tool_choice != "none":
            from aibroker.agent.claude_tools import run_claude_tool_turn
            stream = run_claude_tool_turn(self, run)
        else:
            stream = super().start(run)
        async for item in stream:
            yield item

    def preflight(self, run: AgentRunSpec) -> tuple[str, str] | None:
        problem = settings_problem(self._env.get("CLAUDE_CONFIG_DIR"), run.workspace)
        return ("settings_refused", problem) if problem else None

    async def billing_mode(self) -> BillingMode:
        """`claude auth status`; anything but a positive claude.ai login is not subscription."""
        self._auth = None
        try:
            text = await self._probe([self.binary, "auth", "status"], self._env)
            mode = parse_claude_auth_status(text)
            details = parse_claude_auth_details(text)
        except Exception:  # noqa: BLE001 - cannot tell -> unknown -> the guard refuses
            return "unknown"
        if mode == "subscription" and details is not None:
            self._auth = AuthAttestation(*details)
        return mode


def register_real_adapters(capabilities: Iterable[str] = ("agent:claude", "agent:codex")) -> None:
    """Called by the worker lane at startup (only when AGENT_WORKER_ENABLED)."""
    from aibroker.agent.codex_cli import CodexCLI  # local import: codex_cli builds on this module
    caps = set(capabilities)
    if "agent:claude" in caps:
        register_adapter("agent:claude", ClaudeCLI)
    if "agent:codex" in caps:
        register_adapter("agent:codex", CodexCLI)
