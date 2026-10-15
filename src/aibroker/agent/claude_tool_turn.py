"""Claude Code stream-json turn with caller-declared tools (the Claude twin of codex_app_server).

The CLI runs with `--input-format stream-json` and ONE SDK MCP server, `broker`, declared on argv
(`{"type": "sdk"}`: no process, no file, no network). The CLI sends that server's MCP traffic to
us as `control_request` / `mcp_message` lines and waits for our `control_response`, so a
`tools/call` blocks until the caller posts the tool result - the pause headless `-p` lacks.
Wire format: claude-agent-sdk (MIT) `_internal/query.py`. Built-in tools stay off (`--tools ""`,
plus `Read(inputs/**)` only for attachments); the allow-list names only `mcp__broker__<tool>`.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import re
from collections.abc import AsyncIterator
from typing import Any

from aibroker.agent import events as ev
from aibroker.agent.adapter import AgentRunSpec, StreamEvent
from aibroker.agent.client_tools import (
    ClientToolCancelled,
    ClientToolConflict,
    ClientToolInvalidArguments,
    publish_call,
    take_result,
)
from aibroker.agent.codex_app_server import _reminder_text
from aibroker.agent.parsers import ClaudeParser
from aibroker.agent.real_cli import ATTACHMENT_READ, build_claude_prompt
from aibroker.agent.tools import disallowed_tools, tool_names
from aibroker.config import get_settings

SERVER = "broker"
PREFIX = f"mcp__{SERVER}__"
MCP_PROTOCOL = "2025-06-18"
# 64-char API tool-name limit minus the 13-char "mcp__broker__" prefix
_TOOL_NAME = re.compile(r"^[A-Za-z0-9_-]{1,51}$")
_INIT = "broker-init-1"
_INTERRUPT = "broker-interrupt-1"
# After a terminal client result the turn must be confirmed stopped (its `result`) within this.
TERMINAL_STOP_S = 30.0


def _mcp_tools(run: AgentRunSpec) -> list[dict[str, Any]]:
    if run.tool_choice == "none":
        return []
    return [{"name": t["function"]["name"],
             "description": t["function"].get("description") or t["function"]["name"],
             "inputSchema": t["function"]["parameters"]} for t in run.client_tools]


def build_tool_argv(run: AgentRunSpec, binary: str = "claude",
                    config_dir: str | None = None) -> list[str]:
    """argv of a client-tool run; ValueError for a tool name the CLI could not address."""
    names = [t["name"] for t in _mcp_tools(run)]
    if not all(isinstance(n, str) and _TOOL_NAME.fullmatch(n) for n in names):
        raise ValueError("client tool names must match [A-Za-z0-9_-]{1,51}")
    builtin = [ATTACHMENT_READ] if run.attachments else []
    argv = [binary, "-p", "--input-format", "stream-json", "--output-format", "stream-json",
            "--verbose", "--setting-sources=user", "--strict-mcp-config",
            "--mcp-config=" + json.dumps({"mcpServers": {SERVER: {"type": "sdk",
                                                                 "name": SERVER}}})]
    argv += [f"--tools={','.join(tool_names(builtin))}"] if builtin else ["--tools", ""]
    if run.model:
        argv.append(f"--model={run.model}")
    if run.session_id:
        argv.append(f"--resume={run.session_id}")
    allowed = builtin + [PREFIX + n for n in names]
    if allowed:
        argv.append("--allowedTools=" + ",".join(allowed))
    argv.append("--disallowedTools=" + ",".join(disallowed_tools(config_dir)))
    return argv


def _user(text: str) -> dict[str, Any]:
    return {"type": "user", "message": {"role": "user", "content": text},
            "parent_tool_use_id": None, "session_id": "default"}


def _tool_text(text: str, is_error: bool) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def _counted(usage: ev.Usage) -> bool:
    return any(v is not None for v in (usage.input_tokens, usage.output_tokens,
                                       usage.cache_read_tokens))


def _sum(a: int | None, b: int | None) -> int | None:
    return b if a is None else a if b is None else a + b


async def run_tool_turn(adapter: Any, run: AgentRunSpec) -> AsyncIterator[StreamEvent]:
    """Only `broker` MCP tool calls cross into the client; the worker executes nothing."""
    problem = adapter.preflight(run) or await adapter.preflight_async(run)
    if problem:
        yield ev.RunFailed(error_class=problem[0], message=problem[1][:500])
        return
    try:
        argv = build_tool_argv(run, adapter.binary, adapter._env.get("CLAUDE_CONFIG_DIR"))
    except ValueError as exc:
        yield ev.RunFailed(error_class="tool_invalid", message=str(exc)[:500])
        return
    settings = get_settings()
    env = adapter.process_env(run)
    # the CLI's own MCP call timeout must outlast the caller's tool wait
    env["MCP_TOOL_TIMEOUT"] = str(int((settings.AGENT_TOOL_WAIT_S + 60) * 1000))
    proc = await asyncio.create_subprocess_exec(
        *argv, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, cwd=run.workspace, env=env,
        limit=1 << 24, start_new_session=True)
    adapter._proc = proc
    stderr_task = asyncio.create_task(adapter._drain_stderr(proc))
    assert proc.stdin is not None and proc.stdout is not None

    async def send(message: dict[str, Any]) -> None:
        proc.stdin.write((json.dumps(message, ensure_ascii=False) + "\n").encode())
        await proc.stdin.drain()

    tools = _mcp_tools(run)
    parser = ClaudeParser(run.workspace)
    arg_retries = settings.AGENT_TOOL_ARG_RETRIES
    reminders_left = settings.AGENT_TOOL_REQUIRED_REMINDERS
    rejected = calls = seq = 0
    broker_uses: set[str] = set()
    answered: set[str] = set()
    terminal = False
    usage = ev.Usage(turns=0, source="claude_stream_json")
    try:
        await send({"type": "control_request", "request_id": _INIT,
                    "request": {"subtype": "initialize", "hooks": None}})
        await send(_user(build_claude_prompt(run)))
        loop = asyncio.get_running_loop()
        stop_deadline: float | None = None     # set once a terminal result was acknowledged
        interrupt_confirmed = False
        while True:
            if stop_deadline is None:
                raw = await proc.stdout.readline()
            else:
                remaining = stop_deadline - loop.time()
                if remaining <= 0:
                    raise ClientToolConflict("terminal_stop_unconfirmed: the turn did not stop "
                                             "after the terminal result")
                try:
                    raw = await asyncio.wait_for(proc.stdout.readline(),
                                                 remaining)
                except TimeoutError:
                    raise ClientToolConflict("terminal_stop_unconfirmed: the turn did not stop "
                                             "after the terminal result") from None
            if not raw:
                break
            line = raw.decode(errors="replace").strip()
            try:
                data = json.loads(line) if line.startswith("{") else None
            except ValueError:
                data = None                   # not a control line; the parser skips it too
            kind = data.get("type") if isinstance(data, dict) else None
            if kind == "control_response":
                response = data.get("response") or {}
                expected = (_INIT, _INTERRUPT) if stop_deadline is not None else (_INIT,)
                if (response.get("request_id") not in expected
                        or response.get("subtype") != "success"):
                    raise ClientToolConflict("Claude refused the control initialize or interrupt")
                if response.get("request_id") == _INTERRUPT:
                    if interrupt_confirmed:
                        raise ClientToolConflict("duplicate Claude interrupt confirmation")
                    interrupt_confirmed = True
                continue
            if kind == "control_cancel_request" and data.get("request_id") in answered:
                continue                      # the CLI gave up on a call we already answered
            if kind in ("control_request", "control_cancel_request"):
                request = data.get("request") or {}
                message = request.get("message")
                if (kind != "control_request" or request.get("subtype") != "mcp_message"
                        or request.get("server_name") != SERVER
                        or not isinstance(message, dict)):
                    raise ClientToolConflict("Claude requested an unapproved operation")
                method, mid = message.get("method"), message.get("id")
                params = message.get("params") or {}
                reply: dict[str, Any] = {"jsonrpc": "2.0", "result": {}}
                if mid is not None:
                    reply["id"] = mid
                if mid is None:
                    pass                                   # notification: ack only
                elif method == "initialize":
                    version = params.get("protocolVersion")
                    reply["result"] = {
                        "protocolVersion": version if isinstance(version, str) else MCP_PROTOCOL,
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": SERVER, "version": "1"}}
                elif method == "tools/list":
                    reply["result"] = {"tools": tools}
                elif method == "tools/call":
                    if stop_deadline is not None:
                        raise ClientToolConflict("model requested another tool after terminal "
                                                 "output")
                    seq += 1
                    call_id = f"call_claude_{run.run_id}_{seq}"
                    name, arguments = params.get("name"), params.get("arguments")
                    arguments = {} if arguments is None else arguments
                    if not isinstance(name, str):
                        raise ClientToolConflict("invalid MCP tool call")
                    try:
                        await publish_call(run.run_id, call_id, name, arguments,
                                           run.fence_leases)
                    except ClientToolInvalidArguments as exc:
                        if rejected >= arg_retries:
                            raise
                        rejected += 1
                        reply["result"] = _tool_text(
                            f"Tool arguments rejected: {exc.detail or exc.label}. "
                            f"Call {name[:80]} again with arguments that satisfy its schema.", True)
                        yield ev.ToolResult(call_id=call_id, ok=False,
                                            output="[arguments rejected]")
                    else:
                        calls += 1
                        yield ev.ToolCall(call_id=call_id, name=name,
                                          input=arguments if isinstance(arguments, dict) else {})
                        while (result := await take_result(run.run_id, call_id,
                                                           run.fence_leases)) is None:
                            try:
                                await asyncio.wait_for(proc.wait(), timeout=1.0)
                            except TimeoutError:
                                continue
                            raise ClientToolConflict("Claude stopped while waiting for client tool")
                        success, content, *rest = result
                        terminal = bool(rest and rest[0])
                        reply["result"] = _tool_text(content, not success)
                        yield ev.ToolResult(call_id=call_id, ok=success,
                                            output="[client tool result withheld]")
                elif method != "ping":
                    reply.pop("result")
                    reply["error"] = {"code": -32601, "message": "method not found"}
                await send({"type": "control_response", "response": {
                    "subtype": "success", "request_id": data.get("request_id"),
                    "response": {"mcp_response": reply}}})
                answered.add(str(data.get("request_id")))
                if terminal and stop_deadline is None:
                    # terminal output tool: acknowledged above; stop the turn instead of
                    # letting the model write another answer
                    await send({"type": "control_request", "request_id": _INTERRUPT,
                                "request": {"subtype": "interrupt"}})
                    stop_deadline = loop.time() + TERMINAL_STOP_S
                continue
            for item in parser.feed(line):
                if isinstance(item, ev.ToolCall) and item.name.startswith(PREFIX):
                    broker_uses.add(item.call_id)     # reported from tools/call above
                    continue
                if isinstance(item, ev.ToolResult) and item.call_id in broker_uses:
                    continue
                if isinstance(item, ev.Usage):
                    usage = ev.Usage(
                        input_tokens=_sum(usage.input_tokens, item.input_tokens),
                        output_tokens=_sum(usage.output_tokens, item.output_tokens),
                        cache_read_tokens=_sum(usage.cache_read_tokens, item.cache_read_tokens),
                        turns=_sum(usage.turns, item.turns), source="claude_stream_json")
                    continue
                if stop_deadline is not None and item.type in ev.TERMINAL_TYPES:
                    if _counted(usage):
                        yield usage
                    if isinstance(item, ev.RunFailed):
                        # An interrupted Claude turn can report is_error=True. The CLI
                        # identifies that cancellation separately from execution/API
                        # errors via terminal_reason; never treat a generic failure as
                        # confirmation of the output tool's successful result.
                        cancelled = (kind == "result" and isinstance(data, dict)
                                     and data.get("terminal_reason") in
                                     ("aborted_streaming", "aborted_tools")
                                     and not data.get("result") and not data.get("errors")
                                     and data.get("api_error_status") is None)
                        if not (interrupt_confirmed and cancelled):
                            yield item
                            return
                    if not interrupt_confirmed:
                        raise ClientToolConflict("terminal_stop_unconfirmed: Claude did not "
                                                 "confirm the interrupt")
                    yield ev.RunCompleted(final_text="")
                    return
                if stop_deadline is not None and isinstance(item, ev.MessageDelta):
                    continue
                if isinstance(item, ev.RunCompleted):
                    if calls == 0 and (run.tool_choice == "required"
                                       or isinstance(run.tool_choice, dict)):
                        if reminders_left <= 0:
                            raise ClientToolConflict("required client tool was not called")
                        reminders_left -= 1
                        await send(_user(_reminder_text(run)))   # same session, next turn
                        continue
                    yield usage
                    yield item
                    return
                if item.type in ev.TERMINAL_TYPES:
                    if _counted(usage):
                        yield usage           # a failed run still reports what it spent
                    yield item
                    return
                yield item
        rc = await proc.wait()
        await stderr_task
        if _counted(usage):
            yield usage
        yield parser.finish(rc, adapter._stderr.decode(errors="replace"))
    except ClientToolCancelled:
        if _counted(usage):
            yield usage
        yield ev.RunCancelled()
    except (ClientToolConflict, ValueError, OSError) as exc:
        if _counted(usage):
            yield usage
        yield ev.RunFailed(error_class="client_tool_protocol", message=str(exc)[:500])
    finally:
        await adapter.cancel()
        adapter._signal_group(9)
        stderr_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await stderr_task
