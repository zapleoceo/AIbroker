"""Codex App Server stdio turn with real, caller-controlled dynamic tools."""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncIterator
from pathlib import Path
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
from aibroker.agent.codex_cli import CONFIG_OVERRIDES, permission_overrides
from aibroker.config import get_settings

log = logging.getLogger(__name__)

# After a terminal client result the turn must be confirmed stopped (turn/completed) within this.
TERMINAL_STOP_S = 30.0
_INTERRUPT = 1000          # our turn/interrupt request id: above every turn/start id (3..6)


_KNOWN_METHODS = frozenset({
    "item/tool/call", "model/rerouted", "thread/tokenUsage/updated", "item/started",
    "item/updated", "item/completed", "turn/started", "turn/completed", "thread/started", "error",
})


def _log_message(message: dict[str, Any]) -> None:
    """Diagnosis aid, logged before validation: a known method name and a bounded integer id
    only; anything else is a constant, never the peer's own text, params or results."""
    method = message.get("method")
    shown = "-" if method is None else method if method in _KNOWN_METHODS else "other"
    msg_id = message.get("id")
    shown_id = msg_id if type(msg_id) is int and 0 <= msg_id < 1_000_000 else \
        ("-" if msg_id is None else "other")
    log.debug("app-server message method=%s id=%s", shown, shown_id)


def _is_response(message: dict[str, Any], expected: int) -> bool:
    """A reply to OUR request: our id and no ``method``.

    The App Server numbers its own server->client requests with an independent
    counter, so a server request can carry the same numeric id as ours.
    """
    return message.get("id") == expected and "method" not in message


def _tool_specs(run: AgentRunSpec) -> list[dict[str, Any]]:
    if run.tool_choice == "none":
        return []
    return [{"type": "function", "name": tool["function"]["name"],
             "description": tool["function"].get("description") or tool["function"]["name"],
             "inputSchema": tool["function"]["parameters"]} for tool in run.client_tools]


def _input_items(run: AgentRunSpec) -> list[dict[str, str]]:
    items = [{"type": "text", "text": run.prompt}]
    items += [{"type": "localImage", "path": str(Path(run.workspace) / path)}
              for path in run.attachments]
    return items


def _reminder_text(run: AgentRunSpec) -> str:
    choice = run.tool_choice
    name = ((choice.get("function") or {}).get("name") if isinstance(choice, dict)
            else run.client_tools[0]["function"]["name"] if len(run.client_tools) == 1 else None)
    target = f"the {name} tool" if isinstance(name, str) else "one of the declared tools"
    return f"You must now call {target} with your final answer; do not reply with plain text."


def _reported_count(usage: dict[str, Any], name: str) -> int | None:
    value = usage.get(name)
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


async def run_tool_turn(adapter: Any, run: AgentRunSpec) -> AsyncIterator[StreamEvent]:
    """Only dynamic tool callbacks cross into the client; no worker tool execution."""
    home = adapter._env.get("CODEX_HOME")
    skills = Path(home or "") / "skills"
    workspace = Path(run.workspace).resolve()
    inherited_rules = any((parent / name).exists() for parent in workspace.parents
                          for name in ("AGENTS.md", ".codex"))
    if (not home or any((Path(home) / name).exists() for name in
                        ("config.toml", "AGENTS.md", "rules"))
            or inherited_rules
            or (skills.exists() and any(p.name != ".system" for p in skills.iterdir()))):
        yield ev.RunFailed(error_class="settings_refused",
                           message="App Server requires an inert Codex home")
        return
    problem = adapter.preflight(run) or await adapter.preflight_async(run)
    if problem:
        yield ev.RunFailed(error_class=problem[0], message=problem[1][:500])
        return
    argv = [adapter.binary, "app-server"]
    for override in (*CONFIG_OVERRIDES, *permission_overrides(home)):
        argv += ["-c", override]
    proc = await asyncio.create_subprocess_exec(
        *argv, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, cwd=run.workspace,
        env=adapter.process_env(run), limit=1 << 20, start_new_session=True)
    adapter._proc = proc
    stderr_task = asyncio.create_task(adapter._drain_stderr(proc))
    assert proc.stdin is not None and proc.stdout is not None

    async def send(message: dict[str, Any]) -> None:
        proc.stdin.write((json.dumps(message, ensure_ascii=False) + "\n").encode())
        await proc.stdin.drain()

    async def read_response(expected: int) -> dict[str, Any]:
        while raw := await proc.stdout.readline():
            message = json.loads(raw)
            _log_message(message)
            if _is_response(message, expected):
                if message.get("error") or not isinstance(message.get("result"), dict):
                    raise ClientToolConflict("App Server refused a required protocol request")
                return message["result"]
            if "id" in message and "method" in message:
                raise ClientToolConflict("App Server requested an unexpected operation")
        raise ClientToolConflict("App Server closed before answering")

    try:
        await send({"id": 1, "method": "initialize", "params": {
            "clientInfo": {"name": "aibroker", "title": "AIbroker", "version": "1"},
            "capabilities": {"experimentalApi": True}}})
        await read_response(1)
        await send({"method": "initialized", "params": {}})
        await send({"id": 2, "method": "thread/start", "params": {
            "model": run.model, "cwd": run.workspace, "approvalPolicy": "never",
            "permissions": "broker", "dynamicTools": _tool_specs(run)}})
        started = await read_response(2)
        thread = started.get("thread") or {}
        thread_id = thread.get("id")
        if not isinstance(thread_id, str) or not thread_id:
            raise ClientToolConflict("App Server did not assign a thread")
        yield ev.SessionAssigned(session_id=thread_id)
        configured = started.get("model")
        if isinstance(configured, str) and configured.strip():
            yield ev.ModelObserved(model=configured.strip()[:200], kind="thread_config",
                                   source="codex.app_server.thread_start")
        await send({"id": 3, "method": "turn/start", "params": {
            "threadId": thread_id, "input": _input_items(run), "cwd": run.workspace,
            "approvalPolicy": "never", "model": run.model}})
        settings = get_settings()
        arg_retries = settings.AGENT_TOOL_ARG_RETRIES
        reminders_left = settings.AGENT_TOOL_REQUIRED_REMINDERS
        rejected = 0
        turn_request = 3
        old_turn_requests: set[int] = set()
        final_text = ""
        calls = 0
        turn_id: str | None = None
        seen_requests: set[Any] = set()
        seen_calls: set[str] = set()
        thread_usage: dict[str, Any] | None = None
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
            message = json.loads(raw)
            _log_message(message)
            if _is_response(message, _INTERRUPT):
                if (stop_deadline is None or "result" not in message
                        or "error" in message):
                    raise ClientToolConflict("App Server refused the turn interrupt")
                if interrupt_confirmed:
                    raise ClientToolConflict("duplicate App Server interrupt confirmation")
                interrupt_confirmed = True
                continue
            if any(_is_response(message, old) for old in old_turn_requests):
                raise ClientToolConflict("duplicate turn response")
            if _is_response(message, turn_request):
                if turn_id is not None:
                    raise ClientToolConflict("duplicate turn response")
                if message.get("error"):
                    raise ClientToolConflict("App Server refused the turn")
                turn_id = ((message.get("result") or {}).get("turn") or {}).get("id")
                if not isinstance(turn_id, str) or not turn_id:
                    raise ClientToolConflict("App Server did not assign a turn")
                continue
            method = message.get("method")
            params = message.get("params") or {}
            if method == "item/tool/call":
                if stop_deadline is not None:
                    raise ClientToolConflict("model requested another tool after terminal output")
                if ("id" not in message or params.get("threadId") != thread_id
                        or params.get("turnId") != turn_id or params.get("namespace") is not None):
                    raise ClientToolConflict("invalid dynamic tool request identity")
                call_id, name = params.get("callId"), params.get("tool")
                if not isinstance(name, str) or not isinstance(call_id, str):
                    raise ClientToolConflict("invalid dynamic tool call")
                if message["id"] in seen_requests or call_id in seen_calls:
                    raise ClientToolConflict("duplicate dynamic tool call")
                seen_requests.add(message["id"])
                seen_calls.add(call_id)
                try:
                    await publish_call(run.run_id, call_id, name, params.get("arguments"),
                                       run.fence_leases)
                except ClientToolInvalidArguments as exc:
                    if rejected >= arg_retries:
                        raise
                    rejected += 1
                    # model-retry: tell the model which rule broke (never a value), same turn;
                    # nothing is published to the client and the call is not counted
                    await send({"id": message["id"], "result": {"contentItems": [{
                        "type": "inputText",
                        "text": f"Tool arguments rejected: {exc.detail or exc.label}. "
                                f"Call {name[:80]} again with arguments that satisfy its schema."}],
                        "success": False}})
                    yield ev.ToolResult(call_id=call_id, ok=False,
                                        output="[arguments rejected]")
                    continue
                calls += 1
                arguments = params["arguments"]
                yield ev.ToolCall(call_id=call_id, name=name,
                                  input=arguments if isinstance(arguments, dict) else {})
                while True:
                    result = await take_result(run.run_id, call_id, run.fence_leases)
                    if result is not None:
                        break
                    try:
                        await asyncio.wait_for(proc.wait(), timeout=1.0)
                    except TimeoutError:
                        continue
                    raise ClientToolConflict("App Server stopped while waiting for client tool")
                success, content, *rest = result
                await send({"id": message["id"], "result": {
                    "contentItems": [{"type": "inputText", "text": content}],
                    "success": success}})
                yield ev.ToolResult(call_id=call_id, ok=success,
                                    output="[client tool result withheld]")
                if rest and rest[0]:
                    # terminal output tool: acknowledged above; stop the turn instead of
                    # letting the model write another answer
                    await send({"id": _INTERRUPT, "method": "turn/interrupt",
                                "params": {"threadId": thread_id, "turnId": turn_id}})
                    stop_deadline = loop.time() + TERMINAL_STOP_S
            elif "id" in message and method:
                raise ClientToolConflict("App Server requested an unapproved operation")
            elif method == "model/rerouted":
                if (params.get("threadId") != thread_id or params.get("turnId") != turn_id
                        or not isinstance(params.get("fromModel"), str)
                        or not isinstance(params.get("toModel"), str)):
                    raise ClientToolConflict("invalid model reroute notification")
                yield ev.ModelRerouted(from_model=params["fromModel"],
                                       to_model=params["toModel"])
                raise ClientToolConflict("App Server reported a model reroute")
            elif method == "thread/tokenUsage/updated":
                if params.get("threadId") != thread_id or params.get("turnId") != turn_id:
                    raise ClientToolConflict("usage notification has wrong turn identity")
                observed = (params.get("tokenUsage") or {}).get("total")
                if isinstance(observed, dict):
                    thread_usage = observed
            elif method == "item/completed":
                if (params.get("threadId") not in (None, thread_id)
                        or params.get("turnId") not in (None, turn_id)):
                    raise ClientToolConflict("item notification has wrong turn identity")
                item = params.get("item") or {}
                if item.get("type") == "agentMessage" and stop_deadline is None:
                    final_text = str(item.get("text") or "")
            elif method == "turn/completed":
                if (params.get("threadId") not in (None, thread_id)
                        or (params.get("turn") or {}).get("id") != turn_id):
                    raise ClientToolConflict("completion has wrong turn identity")
                turn = params.get("turn") or {}
                stopped = stop_deadline is not None
                if turn.get("status") not in (("completed", "interrupted") if stopped
                                              else ("completed",)):
                    raise ClientToolConflict("App Server turn did not complete")
                if stopped:
                    if not interrupt_confirmed:
                        raise ClientToolConflict("terminal_stop_unconfirmed: App Server did "
                                                 "not confirm the interrupt")
                    final_text = ""      # the terminal tool result IS the answer
                elif calls == 0 and (run.tool_choice == "required"
                                   or isinstance(run.tool_choice, dict)):
                    if reminders_left <= 0:
                        raise ClientToolConflict("required client tool was not called")
                    reminders_left -= 1
                    old_turn_requests.add(turn_request)
                    turn_request += 1
                    turn_id = None
                    await send({"id": turn_request, "method": "turn/start", "params": {
                        "threadId": thread_id, "cwd": run.workspace, "approvalPolicy": "never",
                        "model": run.model,
                        "input": [{"type": "text", "text": _reminder_text(run)}]}})
                    continue
                if thread_usage is not None:
                    usage = ev.Usage(input_tokens=_reported_count(thread_usage, "inputTokens"),
                                     output_tokens=_reported_count(thread_usage, "outputTokens"),
                                     total_tokens=_reported_count(thread_usage, "totalTokens"),
                                     cache_read_tokens=_reported_count(thread_usage, "cachedInputTokens"),
                                     reasoning_tokens=_reported_count(thread_usage, "reasoningOutputTokens"),
                                     turns=1, source="codex_app_server")
                    if any(getattr(usage, key) is not None for key in
                           ("input_tokens", "output_tokens", "total_tokens",
                            "cache_read_tokens", "reasoning_tokens")):
                        yield usage
                yield ev.RunCompleted(final_text=final_text)
                return
        raise ClientToolConflict("App Server closed before turn completion")
    except ClientToolCancelled:
        yield ev.RunCancelled()
    except (ClientToolConflict, ValueError, OSError, json.JSONDecodeError) as exc:
        yield ev.RunFailed(error_class="client_tool_protocol", message=str(exc)[:500])
    finally:
        await adapter.cancel()
        adapter._signal_group(9)
        stderr_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await stderr_task
