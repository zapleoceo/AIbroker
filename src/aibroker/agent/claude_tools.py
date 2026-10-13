"""Claude Code client tools: a per-run stdio MCP bridge feeds the durable client-tool handoff.

The CLI spawns `python -m aibroker.agent.mcp_bridge` from a per-run `--mcp-config`; that shim
relays each call over a Unix socket to ToolRelay here, in the worker, which publishes it exactly
like the Codex path (publish_call -> poll shows tool_calls -> POST tool-results -> take_result).
The CLI never gets a DB URL, and the run's fence leases stay in this process.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import sys
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

from aibroker.agent import events as ev
from aibroker.agent.adapter import AgentRunSpec, StreamEvent
from aibroker.agent.client_tools import (
    MAX_CALLS,
    ClientToolCancelled,
    ClientToolConflict,
    clear_pending_call,
    publish_call,
    take_result,
)
from aibroker.agent.real_cli import build_claude_argv
from aibroker.services.tool_contract import validate_result

log = logging.getLogger(__name__)
BRIDGE_DIR = ".aibroker-mcp"
MAX_INVALID_CALLS = 3     # schema-invalid calls tolerated before the run fails
ANNOUNCE_WAIT_S = 5.0     # how long a call waits for the CLI's own tool_use event (id matching)
BRIDGE_PREFIX = "mcp__broker__"
SERVER_NAME = "broker"
MAX_SOCKET_PATH = 100     # sun_path is 108 bytes on Linux
WITHHELD = "[client tool result withheld]"


def mcp_tool_list(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"name": t["function"]["name"],
             "description": t["function"].get("description") or t["function"]["name"],
             "inputSchema": t["function"]["parameters"]} for t in tools]


def write_bridge_files(workspace: str, tools: list[dict[str, Any]]) -> tuple[Path, Path]:
    """tools file + MCP config (command and args only, no secrets) -> (config path, socket path)."""
    root = Path(workspace) / BRIDGE_DIR
    root.mkdir(mode=0o700, exist_ok=True)
    tools_file, sock = root / "tools.json", root / "b.sock"
    tools_file.write_text(json.dumps(mcp_tool_list(tools), ensure_ascii=False), encoding="utf-8")
    config = root / "mcp.json"
    config.write_text(json.dumps({"mcpServers": {SERVER_NAME: {
        "type": "stdio", "command": sys.executable,
        "args": ["-I", "-m", "aibroker.agent.mcp_bridge", "--socket", str(sock),
                 "--tools", str(tools_file)]}}}), encoding="utf-8")
    for path in (tools_file, config):
        os.chmod(path, 0o600)
    return config, sock


class ToolRelay:
    """Worker side of the bridge: one pending call at a time, same limits as the Codex path."""

    def __init__(self, run: AgentRunSpec, on_failure: Callable[[], None]) -> None:
        self.run = run
        self.calls = 0
        self.invalid = 0
        self.failure: ev.RunFailed | ev.RunCancelled | None = None
        self._on_failure = on_failure
        self._seq = 0
        self._announced: list[tuple[str, str, str]] = []
        self._announce_event = asyncio.Event()
        self._stop = asyncio.Event()
        self._lock = asyncio.Lock()

    def stop(self) -> None:
        self._stop.set()

    def _fail(self, terminal: ev.RunFailed | ev.RunCancelled) -> None:
        if self.failure is None:
            self.failure = terminal
            self._stop.set()
            self._on_failure()

    @staticmethod
    def _key(arguments: Any) -> str:
        return json.dumps(arguments, sort_keys=True, ensure_ascii=False, separators=(",", ":"))

    def announce(self, call_id: str, name: str, arguments: Any) -> None:
        """Remember the CLI's own tool_use id so the published call carries the same id."""
        if 1 <= len(call_id) <= 128:
            self._announced.append((call_id, name, self._key(arguments)))
            self._announce_event.set()

    async def _claim_id(self, name: str, arguments: Any) -> str:
        """The announced tool_use id with this name and arguments (oldest first), else call_N.
        The MCP request carries no tool_use id we can rely on, so the match is by content."""
        key = self._key(arguments)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + ANNOUNCE_WAIT_S
        while True:
            for index, (call_id, tool, announced) in enumerate(self._announced):
                if tool == name and announced == key:
                    del self._announced[index]
                    return call_id
            remaining = deadline - loop.time()
            if remaining <= 0:
                self._seq += 1
                return f"call_{self._seq}"
            self._announce_event.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._announce_event.wait(), timeout=remaining)

    async def _withdraw(self) -> None:
        try:
            await clear_pending_call(self.run.run_id)
        except Exception:
            log.exception("could not withdraw the pending client tool of run %d", self.run.run_id)

    def _argument_problem(self, call_id: str, name: str, raw: str) -> str | None:
        call = {"id": call_id, "type": "function", "function": {"name": name, "arguments": raw}}
        return validate_result("", {"finish_reason": "tool_calls", "tool_calls": [call]},
                               self.run.client_tools, self.run.tool_choice)

    async def call(self, name: str, arguments: Any) -> tuple[bool, str]:
        """-> (is_error, text) for the MCP reply; a protocol fault also ends the run."""
        if self._stop.is_set():
            return True, "the run is stopping"
        async with self._lock:
            call_id = await self._claim_id(name, arguments)
            raw = json.dumps(arguments, ensure_ascii=False, separators=(",", ":"))
            if problem := self._argument_problem(call_id, name, raw):
                self.invalid += 1
                if self.invalid >= MAX_INVALID_CALLS or self.calls + self.invalid > MAX_CALLS:
                    self._fail(ev.RunFailed(error_class="client_tool_protocol",
                                            message="too many invalid client tool calls"))
                    return True, "too many invalid tool calls; the run is ending"
                return True, f"arguments for {name} do not match its schema ({problem})"
            try:
                await publish_call(self.run.run_id, call_id, name, arguments, self.run.fence_leases)
                self.calls += 1
                while True:
                    result = await take_result(self.run.run_id, call_id, self.run.fence_leases)
                    if result is not None:
                        return not result[0], result[1]
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(self._stop.wait(), timeout=1.0)
                    if self._stop.is_set():
                        return True, "the run is stopping"
            except ClientToolCancelled:
                self._fail(ev.RunCancelled())
            except ClientToolConflict as exc:
                self._fail(ev.RunFailed(error_class="client_tool_protocol", message=str(exc)[:500]))
            except Exception as exc:    # StaleLease, DB errors: never leave the call pending
                log.exception("client tool call failed for run %d", self.run.run_id)
                await self._withdraw()
                self._fail(ev.RunFailed(error_class="adapter_error", message=str(exc)[:500]))
            return True, "the tool call could not be completed"

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request = json.loads(await reader.readline())
            name = request["name"]
            if not isinstance(name, str):
                raise TypeError("name")
            is_error, text = await self.call(name, request.get("arguments"))
        except (ValueError, KeyError, TypeError, asyncio.LimitOverrunError):
            is_error, text = True, "malformed relay request"
        with contextlib.suppress(OSError):
            writer.write((json.dumps({"is_error": is_error, "text": text}) + "\n").encode())
            await writer.drain()
        writer.close()

    async def serve(self, path: Path) -> asyncio.AbstractServer:
        server = await asyncio.start_unix_server(self._handle, path=str(path), limit=1 << 20)
        os.chmod(path, 0o600)
        return server


async def run_claude_tool_turn(adapter: Any, run: AgentRunSpec) -> AsyncIterator[StreamEvent]:
    """Run the CLI with the broker MCP server as its ONLY tool source."""
    problem = adapter.preflight(run) or await adapter.preflight_async(run)
    if not problem and (run.allowed_tools or run.attachments):
        problem = ("tool_invalid", "client tools exclude CLI tools and attachments")
    if problem:
        yield ev.RunFailed(error_class=problem[0], message=problem[1][:500])
        return
    try:
        config, sock = write_bridge_files(run.workspace, run.client_tools)
        if len(str(sock)) > MAX_SOCKET_PATH:
            raise ValueError("run workspace path is too long for the bridge socket")
        argv = build_claude_argv(run, adapter.binary, adapter._env.get("CLAUDE_CONFIG_DIR"),
                                 mcp_config=str(config))
    except (ValueError, OSError) as exc:
        yield ev.RunFailed(error_class="tool_invalid", message=str(exc)[:500])
        return
    killers: set[asyncio.Task[None]] = set()

    def kill() -> None:
        task = asyncio.ensure_future(adapter.cancel())
        killers.add(task)
        task.add_done_callback(killers.discard)

    relay = ToolRelay(run, kill)
    server = await relay.serve(sock)
    try:
        async for item in adapter.exec_argv(argv, run):
            if isinstance(item, ev.ToolResult):
                item = ev.ToolResult(call_id=item.call_id, ok=item.ok, output=WITHHELD)
            elif isinstance(item, ev.ToolCall) and item.name.startswith(BRIDGE_PREFIX):
                bare = item.name[len(BRIDGE_PREFIX):]
                relay.announce(item.call_id, bare, item.input)
                item = ev.ToolCall(call_id=item.call_id, name=bare, input=item.input)
            if item.type in ev.TERMINAL_TYPES:
                if relay.failure is not None:
                    yield relay.failure
                elif (isinstance(item, ev.RunCompleted) and relay.calls == 0
                      and (run.tool_choice == "required" or isinstance(run.tool_choice, dict))):
                    yield ev.RunFailed(error_class="client_tool_protocol",
                                       message="required client tool was not called")
                else:
                    yield item
                return
            yield item
    finally:
        relay.stop()
        server.close()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(server.wait_closed(), timeout=5.0)
        if killers:
            await asyncio.gather(*killers, return_exceptions=True)
