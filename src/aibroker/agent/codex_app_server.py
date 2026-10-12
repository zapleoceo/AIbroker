"""Codex App Server stdio turn with real, caller-controlled dynamic tools."""
from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from aibroker.agent import events as ev
from aibroker.agent.adapter import AgentRunSpec, StreamEvent
from aibroker.agent.client_tools import ClientToolConflict, publish_call, take_result
from aibroker.agent.codex_cli import CONFIG_OVERRIDES, permission_overrides


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
            if message.get("id") == expected:
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
        thread = (await read_response(2)).get("thread") or {}
        thread_id = thread.get("id")
        if not isinstance(thread_id, str) or not thread_id:
            raise ClientToolConflict("App Server did not assign a thread")
        yield ev.SessionAssigned(session_id=thread_id)
        await send({"id": 3, "method": "turn/start", "params": {
            "threadId": thread_id, "input": _input_items(run), "cwd": run.workspace,
            "approvalPolicy": "never", "model": run.model}})
        final_text = ""
        calls = 0
        turn_id: str | None = None
        async for raw in proc.stdout:
            message = json.loads(raw)
            if message.get("id") == 3:
                if message.get("error"):
                    raise ClientToolConflict("App Server refused the turn")
                turn_id = ((message.get("result") or {}).get("turn") or {}).get("id")
                if not isinstance(turn_id, str) or not turn_id:
                    raise ClientToolConflict("App Server did not assign a turn")
                continue
            method = message.get("method")
            params = message.get("params") or {}
            if method == "item/tool/call":
                if ("id" not in message or params.get("threadId") != thread_id
                        or params.get("turnId") != turn_id or params.get("namespace") is not None):
                    raise ClientToolConflict("invalid dynamic tool request identity")
                call_id, name = params.get("callId"), params.get("tool")
                if not isinstance(name, str) or not isinstance(call_id, str):
                    raise ClientToolConflict("invalid dynamic tool call")
                await publish_call(run.run_id, call_id, name, params.get("arguments"),
                                   run.fence_leases)
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
                success, content = result
                await send({"id": message["id"], "result": {
                    "contentItems": [{"type": "inputText", "text": content}],
                    "success": success}})
                yield ev.ToolResult(call_id=call_id, ok=success,
                                    output="[client tool result withheld]")
            elif "id" in message and method:
                raise ClientToolConflict("App Server requested an unapproved operation")
            elif method == "model/rerouted":
                raise ClientToolConflict("App Server reported a model reroute")
            elif method == "item/completed":
                if (params.get("threadId") not in (None, thread_id)
                        or params.get("turnId") not in (None, turn_id)):
                    raise ClientToolConflict("item notification has wrong turn identity")
                item = params.get("item") or {}
                if item.get("type") == "agentMessage":
                    final_text = str(item.get("text") or "")
            elif method == "turn/completed":
                if (params.get("threadId") not in (None, thread_id)
                        or (params.get("turn") or {}).get("id") != turn_id):
                    raise ClientToolConflict("completion has wrong turn identity")
                turn = params.get("turn") or {}
                if turn.get("status") != "completed":
                    raise ClientToolConflict("App Server turn did not complete")
                if calls == 0 and (run.tool_choice == "required"
                                   or isinstance(run.tool_choice, dict)):
                    raise ClientToolConflict("required client tool was not called")
                yield ev.RunCompleted(final_text=final_text)
                return
        raise ClientToolConflict("App Server closed before turn completion")
    except (ClientToolConflict, ValueError, OSError, json.JSONDecodeError) as exc:
        yield ev.RunFailed(error_class="client_tool_protocol", message=str(exc)[:500])
    finally:
        await adapter.cancel()
        adapter._signal_group(9)
        stderr_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await stderr_task
