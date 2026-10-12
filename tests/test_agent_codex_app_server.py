"""App Server protocol fixture: a tool waits for the caller and resumes the same turn."""
from __future__ import annotations

import asyncio
import json

from aibroker.agent.adapter import AgentRunSpec
from aibroker.agent.codex_app_server import run_tool_turn

TOOL = {"type": "function", "function": {"name": "lookup", "description": "Find an id",
        "parameters": {"type": "object", "properties": {"id": {"type": "integer"}}}}}


class _Reader:
    def __init__(self):
        self.queue = asyncio.Queue()

    async def readline(self):
        return await self.queue.get()

    def __aiter__(self):
        return self

    async def __anext__(self):
        value = await self.readline()
        if not value:
            raise StopAsyncIteration
        return value

    def put(self, value):
        self.queue.put_nowait((json.dumps(value) + "\n").encode())


class _Writer:
    def __init__(self, reader, unexpected=False):
        self.reader = reader
        self.sent = []
        self.unexpected = unexpected

    def write(self, data):
        message = json.loads(data)
        self.sent.append(message)
        method = message.get("method")
        if method == "initialize":
            self.reader.put({"id": 1, "result": {}})
        elif method == "thread/start":
            self.reader.put({"id": 2, "result": {"thread": {"id": "thread-1"}}})
        elif method == "turn/start":
            self.reader.put({"id": 3, "result": {"turn": {"id": "turn-1"}}})
            if self.unexpected:
                self.reader.put({"id": 44, "method": "tool/requestUserInput", "params": {}})
            else:
                self.reader.put({"id": 99, "method": "item/tool/call", "params": {
                    "threadId": "thread-1", "turnId": "turn-1", "namespace": None,
                    "callId": "call-1", "tool": "lookup", "arguments": {"id": 7}}})
        elif message.get("id") == 99 and "result" in message:
            self.reader.put({"method": "thread/tokenUsage/updated", "params": {
                "threadId": "thread-1", "turnId": "turn-1", "tokenUsage": {
                    "total": {"inputTokens": 11, "outputTokens": 5, "totalTokens": 16,
                             "cachedInputTokens": 2, "reasoningOutputTokens": 3}}}})
            self.reader.put({"method": "item/completed", "params": {
                "threadId": "thread-1", "turnId": "turn-1",
                "item": {"type": "agentMessage", "text": "Found 7"}}})
            self.reader.put({"method": "turn/completed", "params": {
                "threadId": "thread-1", "turn": {"id": "turn-1", "status": "completed"}}})

    async def drain(self):
        return None


class _Process:
    def __init__(self, unexpected=False):
        self.stdout = _Reader()
        self.stdin = _Writer(self.stdout, unexpected)
        self.stderr = _Reader()
        self.finished = asyncio.Event()

    async def wait(self):
        await self.finished.wait()
        return 0


class _Adapter:
    binary = "codex"

    def __init__(self, home):
        self._env = {"CODEX_HOME": str(home)}
        self._proc = None

    def preflight(self, run):
        return None

    async def preflight_async(self, run):
        return None

    def process_env(self, run):
        return dict(self._env)

    async def _drain_stderr(self, proc):
        return None

    async def cancel(self):
        self._proc.finished.set()

    def _signal_group(self, signal):
        return None


def _run(tmp_path):
    home = tmp_path / "codex-home"
    home.mkdir()
    return AgentRunSpec(run_id=42, capability="agent:codex", prompt="Find id 7",
                        workspace=str(tmp_path), account_id="owner", model="gpt-6.1-sol",
                        client_tools=[TOOL], tool_choice="required"), home


async def test_dynamic_tool_callback_waits_for_caller_then_completes(monkeypatch, tmp_path):
    run, home = _run(tmp_path)
    process = _Process()
    async def spawn(*args, **kwargs):
        assert args[:2] == ("codex", "app-server")
        assert kwargs["env"] == {"CODEX_HOME": str(home)}
        return process
    async def publish(run_id, call_id, name, arguments, fences):
        assert (run_id, call_id, name, arguments) == (42, "call-1", "lookup", {"id": 7})
    async def take(run_id, call_id, fences):
        return True, "{" + '"found":7}'
    monkeypatch.setattr("aibroker.agent.codex_app_server.asyncio.create_subprocess_exec", spawn)
    monkeypatch.setattr("aibroker.agent.codex_app_server.publish_call", publish)
    monkeypatch.setattr("aibroker.agent.codex_app_server.take_result", take)
    events = [item async for item in run_tool_turn(_Adapter(home), run)]
    assert [item.type for item in events] == ["session.assigned", "tool.call",
                                               "tool.result", "usage", "run.completed"]
    assert events[-2].total_tokens == 16
    assert events[-2].reasoning_tokens == 3
    assert events[-1].final_text == "Found 7"
    assert process.stdin.sent[0]["params"]["capabilities"]["experimentalApi"] is True
    assert process.stdin.sent[2]["params"]["permissions"] == "broker"
    assert process.stdin.sent[-1]["result"]["contentItems"][0]["text"] == '{"found":7}'


async def test_unapproved_server_request_fails_closed(monkeypatch, tmp_path):
    run, home = _run(tmp_path)
    process = _Process(unexpected=True)
    async def spawn(*args, **kwargs):
        return process
    monkeypatch.setattr("aibroker.agent.codex_app_server.asyncio.create_subprocess_exec", spawn)
    events = [item async for item in run_tool_turn(_Adapter(home), run)]
    assert events[-1].type == "run.failed"
    assert "unapproved operation" in events[-1].message


async def test_app_server_refuses_user_config_in_auth_home(tmp_path):
    run, home = _run(tmp_path)
    (home / "config.toml").write_text("unknown = true")
    events = [item async for item in run_tool_turn(_Adapter(home), run)]
    assert events[0].type == "run.failed"
    assert events[0].error_class == "settings_refused"
