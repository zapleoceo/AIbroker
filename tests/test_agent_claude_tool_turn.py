"""Claude stream-json control channel: caller tools served as the SDK MCP server `broker`."""
from __future__ import annotations

import asyncio
import json
from dataclasses import replace

import pytest

from aibroker.agent import events as ev
from aibroker.agent.adapter import AgentRunSpec
from aibroker.agent.claude_tool_turn import build_tool_argv, run_tool_turn
from aibroker.agent.client_tools import ClientToolConflict, ClientToolInvalidArguments

TOOL = {"type": "function", "function": {"name": "lookup", "description": "Find an id",
        "parameters": {"type": "object", "properties": {"id": {"type": "integer"}}}}}
SECRET = "SYNTHETIC-ARG-VALUE"


class _Reader:
    def __init__(self):
        self.queue = asyncio.Queue()

    def __aiter__(self):
        return self

    async def __anext__(self):
        value = await self.queue.get()
        if not value:
            self.on_eof()                 # the CLI closed stdout: its process exits too
            raise StopAsyncIteration
        return value

    def put(self, value):
        raw = value if isinstance(value, str) else json.dumps(value)
        self.queue.put_nowait((raw + "\n").encode())


class _Writer:
    """Answer each line we send with ``react(message)`` -> CLI lines to emit."""

    def __init__(self, reader, react):
        self.reader, self.react, self.sent = reader, react, []

    def write(self, data):
        message = json.loads(data)
        self.sent.append(message)
        for item in self.react(message):
            if item is None:
                self.reader.queue.put_nowait(b"")
            else:
                self.reader.put(item)

    async def drain(self):
        return None


class _Process:
    def __init__(self, react):
        self.stdout = _Reader()
        self.stdin = _Writer(self.stdout, react)
        self.finished = asyncio.Event()
        self.stdout.on_eof = self.finished.set

    async def wait(self):
        await self.finished.wait()
        return 0


class _Adapter:
    binary = "claude"

    def __init__(self):
        self._env = {"CLAUDE_CONFIG_DIR": "/home/agent/.claude"}
        self._proc = None
        self._stderr = bytearray(b"boom")

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


def _run(tmp_path, **kw):
    return AgentRunSpec(run_id=42, capability="agent:claude", prompt="Find id 7",
                        workspace=str(tmp_path), account_id="owner", model="claude-sonnet-5-5",
                        client_tools=[TOOL], tool_choice="required", **kw)


def _mcp(req_id, method, params=None, mid=1):
    message = {"jsonrpc": "2.0", "method": method, "params": params or {}}
    if mid is not None:
        message["id"] = mid
    return {"type": "control_request", "request_id": req_id,
            "request": {"subtype": "mcp_message", "server_name": "broker", "message": message}}


_INIT = {"type": "system", "subtype": "init", "session_id": "sess-1", "model": "claude-sonnet-5-5",
         "apiKeySource": "none"}


def _result(text="done", is_error=False, out=5):
    return {"type": "result", "subtype": "success", "is_error": is_error, "result": text,
            "num_turns": 1, "usage": {"input_tokens": 10, "output_tokens": out,
                                      "cache_read_input_tokens": 4}}


def _assistant_use(name="mcp__broker__lookup", use_id="toolu_1"):
    return {"type": "assistant", "message": {"model": "claude-sonnet-5-5", "content": [
        {"type": "tool_use", "id": use_id, "name": name, "input": {"id": 7}}]}}


def _reply(sent, request_id):
    return next(m["response"]["response"]["mcp_response"] for m in sent
                if m.get("type") == "control_response"
                and m["response"]["request_id"] == request_id)


async def _drive(monkeypatch, tmp_path, react, *, publish=None, results=(("ok", True),),
                 run=None, arg_retries=2, reminders=1):
    from aibroker.config import get_settings
    process = _Process(react)
    spawned = {}

    async def spawn(*argv, **kwargs):
        spawned["argv"], spawned["env"] = argv, kwargs["env"]
        return process
    published = []

    async def default_publish(_run, call_id, name, arguments, _fences):
        published.append((call_id, name, arguments))
    pending = list(results)

    async def take(*_a):
        content, ok = pending.pop(0)
        return ok, content
    settings = get_settings()
    monkeypatch.setattr(settings, "AGENT_TOOL_ARG_RETRIES", arg_retries)
    monkeypatch.setattr(settings, "AGENT_TOOL_REQUIRED_REMINDERS", reminders)
    monkeypatch.setattr("aibroker.agent.claude_tool_turn.asyncio.create_subprocess_exec", spawn)
    monkeypatch.setattr("aibroker.agent.claude_tool_turn.publish_call",
                        publish or default_publish)
    monkeypatch.setattr("aibroker.agent.claude_tool_turn.take_result", take)
    events = [item async for item in run_tool_turn(_Adapter(), run or _run(tmp_path))]
    return events, process.stdin.sent, published, spawned


def test_argv_names_only_the_broker_sdk_server_and_its_tools(tmp_path):
    argv = build_tool_argv(_run(tmp_path), "claude", "/home/agent/.claude")
    assert argv[:7] == ["claude", "-p", "--input-format", "stream-json", "--output-format",
                        "stream-json", "--verbose"]
    assert "--strict-mcp-config" in argv and "--setting-sources=user" in argv
    config = json.loads(next(a for a in argv if a.startswith("--mcp-config="))[13:])
    assert config == {"mcpServers": {"broker": {"type": "sdk", "name": "broker"}}}
    assert argv[argv.index("--tools") + 1] == ""
    assert "--allowedTools=mcp__broker__lookup" in argv
    assert "--model=claude-sonnet-5-5" in argv
    assert any(a.startswith("--disallowedTools=") for a in argv)
    with_file = build_tool_argv(_run(tmp_path, attachments=["inputs/a.png"]), "claude")
    assert "--tools=Read" in with_file
    assert "--allowedTools=Read(inputs/**),mcp__broker__lookup" in with_file
    none = build_tool_argv(replace(_run(tmp_path), tool_choice="none"), "claude")
    assert not any(a.startswith("--allowedTools=") for a in none)


def test_argv_refuses_a_tool_name_the_cli_cannot_address(tmp_path):
    bad = {"type": "function", "function": {"name": "a b", "parameters": {"type": "object"}}}
    with pytest.raises(ValueError, match="client tool names"):
        build_tool_argv(replace(_run(tmp_path), client_tools=[bad]), "claude")


async def test_handshake_then_call_waits_for_the_caller_and_completes(monkeypatch, tmp_path):
    def react(m):
        if m.get("type") == "control_request":
            return [{"type": "control_response", "response": {
                "subtype": "success", "request_id": m["request_id"], "response": {}}},
                _mcp("c1", "initialize", {"protocolVersion": "2025-06-18"}, mid=0),
                _mcp("c2", "notifications/initialized", mid=None),
                _mcp("c3", "tools/list", mid=1)]
        if m.get("type") == "user":
            return [_INIT, _assistant_use(),
                    _mcp("c4", "tools/call", {"name": "lookup",
                                              "arguments": {"id": 7, "note": SECRET}}, mid=2)]
        if m.get("type") == "control_response" and m["response"]["request_id"] == "c4":
            return [{"type": "user", "message": {"content": [
                        {"type": "tool_result", "tool_use_id": "toolu_1", "content": "x"}]}},
                    {"type": "assistant", "message": {"model": "claude-sonnet-5-5", "content": [
                        {"type": "text", "text": "Found 7"}]}},
                    _result("Found 7")]
        return []
    events, sent, published, spawned = await _drive(
        monkeypatch, tmp_path, react, results=[("{\"name\":\"seven\"}", True)])
    assert events[-1] == ev.RunCompleted(final_text="Found 7")
    assert published == [("call_claude_42_1", "lookup", {"id": 7, "note": SECRET})]
    assert _reply(sent, "c1")["result"]["serverInfo"]["name"] == "broker"
    assert _reply(sent, "c1")["id"] == 0
    assert _reply(sent, "c2") == {"jsonrpc": "2.0", "result": {}}
    tools = _reply(sent, "c3")["result"]["tools"]
    assert tools == [{"name": "lookup", "description": "Find an id",
                      "inputSchema": TOOL["function"]["parameters"]}]
    assert _reply(sent, "c4") == {"jsonrpc": "2.0", "id": 2, "result": {
        "content": [{"type": "text", "text": "{\"name\":\"seven\"}"}], "isError": False}}
    calls = [e for e in events if e.type == "tool.call"]
    assert [(c.call_id, c.name) for c in calls] == [("call_claude_42_1", "lookup")]
    results = [e for e in events if e.type == "tool.result"]
    assert [(r.call_id, r.ok, r.output) for r in results] == [
        ("call_claude_42_1", True, "[client tool result withheld]")]
    usage = next(e for e in events if e.type == "usage")
    assert (usage.input_tokens, usage.output_tokens, usage.cache_read_tokens) == (10, 5, 4)
    assert spawned["env"]["MCP_TOOL_TIMEOUT"].isdigit()
    first = sent[0]
    assert first["type"] == "control_request" and first["request"]["subtype"] == "initialize"
    assert sent[1]["type"] == "user" and sent[1]["message"]["content"] == "Find id 7"
    for e in events:
        if e.type != "tool.call":
            assert SECRET not in e.model_dump_json()


async def test_a_failed_client_result_reaches_the_model_as_an_mcp_error(monkeypatch, tmp_path):
    def react(m):
        if m.get("type") == "user":
            return [_INIT, _mcp("c4", "tools/call", {"name": "lookup", "arguments": {"id": 7}})]
        if m.get("type") == "control_response" and m["response"]["request_id"] == "c4":
            return [_result("gave up")]
        return []
    events, sent, *_ = await _drive(monkeypatch, tmp_path, react, results=[("not found", False)])
    assert _reply(sent, "c4")["result"] == {"content": [{"type": "text", "text": "not found"}],
                                            "isError": True}
    assert events[-1].type == "run.completed"


async def test_failed_runs_still_report_usage_and_bad_lines_are_skipped(monkeypatch, tmp_path):
    def react(m):
        if m.get("type") == "user":
            return [_INIT, {"type": "x"}, "{not json", _result("Overloaded", is_error=True)]
        return []
    events, *_ = await _drive(monkeypatch, tmp_path, react)
    assert [e.type for e in events][-2:] == ["usage", "run.failed"]
    assert events[-2].output_tokens == 5


async def test_a_cancel_of_an_answered_call_is_ignored(monkeypatch, tmp_path):
    def react(m):
        if m.get("type") == "user":
            return [_INIT, _mcp("c1", "tools/call", {"name": "lookup", "arguments": {"id": 1}})]
        if m.get("type") == "control_response" and m["response"]["request_id"] == "c1":
            return [{"type": "control_cancel_request", "request_id": "c1"}, _result()]
        return []
    events, *_ = await _drive(monkeypatch, tmp_path, react)
    assert events[-1].type == "run.completed"


def test_a_tool_name_longer_than_the_prefixed_api_limit_is_refused(tmp_path):
    long = {"type": "function", "function": {"name": "a" * 52, "parameters": {"type": "object"}}}
    with pytest.raises(ValueError, match="1,51"):
        build_tool_argv(replace(_run(tmp_path), client_tools=[long]), "claude")
    ok = {"type": "function", "function": {"name": "a" * 51, "parameters": {"type": "object"}}}
    assert f"--allowedTools=mcp__broker__{'a' * 51}" in build_tool_argv(
        replace(_run(tmp_path), client_tools=[ok]), "claude")


@pytest.mark.parametrize("request_line", [
    {"type": "control_request", "request_id": "p1",
     "request": {"subtype": "can_use_tool", "tool_name": "Bash", "input": {}}},
    {"type": "control_request", "request_id": "p2", "request": {
        "subtype": "mcp_message", "server_name": "other",
        "message": {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}}},
    {"type": "control_cancel_request", "request_id": "p3"},
])
async def test_any_other_control_request_fails_closed(monkeypatch, tmp_path, request_line):
    def react(m):
        return [_INIT, request_line] if m.get("type") == "user" else []
    events, sent, published, _ = await _drive(monkeypatch, tmp_path, react)
    assert events[-1].type == "run.failed"
    assert events[-1].error_class == "client_tool_protocol"
    assert not published
    assert not [m for m in sent if m.get("type") == "control_response"]


async def test_an_unknown_mcp_method_gets_a_json_rpc_error(monkeypatch, tmp_path):
    def react(m):
        if m.get("type") == "user":
            return [_INIT, _mcp("c1", "resources/list", mid=5), _mcp("c2", "ping", mid=6),
                    _mcp("c3", "tools/call", {"name": "lookup", "arguments": {"id": 1}}, mid=7)]
        if m.get("type") == "control_response" and m["response"]["request_id"] == "c3":
            return [_result()]
        return []
    events, sent, *_ = await _drive(monkeypatch, tmp_path, react)
    assert _reply(sent, "c1") == {"jsonrpc": "2.0", "id": 5,
                                  "error": {"code": -32601, "message": "method not found"}}
    assert _reply(sent, "c2") == {"jsonrpc": "2.0", "id": 6, "result": {}}
    assert events[-1].type == "run.completed"


async def test_a_refused_initialize_fails_closed(monkeypatch, tmp_path):
    def react(m):
        if m.get("type") == "control_request":
            return [{"type": "control_response", "response": {
                "subtype": "error", "request_id": m["request_id"], "error": "nope"}}]
        return []
    events, *_ = await _drive(monkeypatch, tmp_path, react)
    assert events[-1].type == "run.failed" and "initialize" in events[-1].message


async def test_invalid_arguments_are_rejected_to_the_model_then_retried(monkeypatch, tmp_path):
    published = []

    async def publish(_run, call_id, _name, arguments, _fences):
        if arguments.get("id") == "bad":
            raise ClientToolInvalidArguments("InvalidToolArguments",
                                             "id: type integer (got type str)")
        published.append(call_id)

    def react(m):
        if m.get("type") == "user":
            return [_INIT, _mcp("c1", "tools/call", {"name": "lookup",
                                                     "arguments": {"id": "bad"}}, mid=1)]
        if m.get("type") == "control_response" and m["response"]["request_id"] == "c1":
            return [_mcp("c2", "tools/call", {"name": "lookup", "arguments": {"id": 1}}, mid=2)]
        if m.get("type") == "control_response" and m["response"]["request_id"] == "c2":
            return [_result()]
        return []
    events, sent, *_ = await _drive(monkeypatch, tmp_path, react, publish=publish)
    rejected = _reply(sent, "c1")["result"]
    assert rejected["isError"] is True
    assert "id: type integer (got type str)" in rejected["content"][0]["text"]
    assert published == ["call_claude_42_2"]
    assert ("tool.result", "call_claude_42_1", False) in [
        (e.type, getattr(e, "call_id", None), getattr(e, "ok", None)) for e in events]
    assert events[-1].type == "run.completed"


async def test_invalid_arguments_beyond_the_budget_fail_the_run(monkeypatch, tmp_path):
    async def publish(*_a):
        raise ClientToolInvalidArguments("InvalidToolArguments", "id: type integer")

    def react(m):
        if m.get("type") == "user":
            return [_INIT, _mcp("c1", "tools/call", {"name": "lookup", "arguments": {}})]
        return []
    events, sent, *_ = await _drive(monkeypatch, tmp_path, react, publish=publish,
                                    arg_retries=0)
    assert events[-1].type == "run.failed"
    assert "[InvalidToolArguments]" in events[-1].message
    assert not [m for m in sent if m.get("type") == "control_response"]


async def test_a_non_schema_conflict_fails_closed(monkeypatch, tmp_path):
    async def publish(*_a):
        raise ClientToolConflict("tool call is outside the declared schema [UnknownToolCall]")

    def react(m):
        if m.get("type") == "user":
            return [_INIT, _mcp("c1", "tools/call", {"name": "nope", "arguments": {}})]
        return []
    events, *_ = await _drive(monkeypatch, tmp_path, react, publish=publish)
    assert events[-1].type == "run.failed" and "UnknownToolCall" in events[-1].message


@pytest.mark.parametrize("tool_choice", [
    "required", {"type": "function", "function": {"name": "lookup"}}])
async def test_required_tool_not_called_sends_one_reminder_then_completes(
        monkeypatch, tmp_path, tool_choice):
    users = []

    def react(m):
        if m.get("type") == "user":
            users.append(m["message"]["content"])
            if len(users) == 1:
                return [_INIT, _result("plain text, no tool")]
            return [_mcp("c1", "tools/call", {"name": "lookup", "arguments": {"id": 1}})]
        if m.get("type") == "control_response" and m["response"]["request_id"] == "c1":
            return [_result("done", out=3)]
        return []
    events, *_ = await _drive(monkeypatch, tmp_path, react,
                              run=replace(_run(tmp_path), tool_choice=tool_choice))
    assert events[-1] == ev.RunCompleted(final_text="done")
    assert len(users) == 2 and "call the lookup tool" in users[1]
    usage = next(e for e in events if e.type == "usage")
    assert (usage.output_tokens, usage.turns) == (8, 2)


async def test_required_tool_reminder_budget_exhausted_fails(monkeypatch, tmp_path):
    def react(m):
        return [_INIT, _result("plain")] if m.get("type") == "user" else []
    events, *_ = await _drive(monkeypatch, tmp_path, react, reminders=0)
    assert events[-1].type == "run.failed"
    assert "required client tool was not called" in events[-1].message


async def test_auto_choice_completes_without_a_call(monkeypatch, tmp_path):
    def react(m):
        return [_INIT, _result("just text")] if m.get("type") == "user" else []
    events, *_ = await _drive(monkeypatch, tmp_path, react,
                              run=replace(_run(tmp_path), tool_choice="auto"))
    assert events[-1] == ev.RunCompleted(final_text="just text")


async def test_an_api_key_billing_route_is_refused(monkeypatch, tmp_path):
    def react(m):
        return [{**_INIT, "apiKeySource": "ANTHROPIC_API_KEY"}] if m.get("type") == "user" else []
    events, _, published, _ = await _drive(monkeypatch, tmp_path, react)
    assert events[-1].type == "run.failed" and events[-1].error_class == "billing_mode_refused"
    assert not published


async def test_cli_exit_without_a_result_reports_the_stderr_tail(monkeypatch, tmp_path):
    def react(m):
        return [_INIT, None] if m.get("type") == "user" else []
    process_events, *_ = await _drive(monkeypatch, tmp_path, react)
    assert process_events[-1].type == "run.failed" and "boom" in process_events[-1].message


async def test_claude_adapter_routes_client_tool_runs_to_the_control_channel(monkeypatch,
                                                                            tmp_path):
    from aibroker.agent.real_cli import ClaudeCLI
    seen = []

    async def fake_turn(adapter, run):
        seen.append(run.run_id)
        yield ev.RunCompleted(final_text="ok")
    monkeypatch.setattr("aibroker.agent.claude_tool_turn.run_tool_turn", fake_turn)
    adapter = ClaudeCLI(env={"CLAUDE_CONFIG_DIR": str(tmp_path / "cfg")})
    events = [e async for e in adapter.start(_run(tmp_path))]
    assert seen == [42] and events == [ev.RunCompleted(final_text="ok")]


async def test_worker_runs_a_claude_tool_job_only_when_switched_on_and_verified(monkeypatch,
                                                                                 tmp_path):
    from types import SimpleNamespace

    from sqlalchemy import insert

    from aibroker.agent.executor import ExecConfig, execute_run
    from aibroker.agent.fake import FakeAgentCLI
    from aibroker.agent.registry import clear_adapters, register_adapter
    from aibroker.agent.runs import create_run, list_events
    from aibroker.auth import generate_project_key, hash_project_key
    from aibroker.config import get_settings
    from aibroker.db import get_session
    from aibroker.db.models import DeepJobRow, ProjectRow
    plain = generate_project_key()
    async with get_session() as s:
        await s.execute(insert(ProjectRow).values(
            id=77, name="claude_tools", project_key_hash=hash_project_key(plain),
            project_key_prefix=plain[:12], allowed_scopes=["llm:agent"], is_active=True,
            notes=""))
    settings = get_settings()
    monkeypatch.setattr(settings, "AGENT_ALLOWED_PROJECT_IDS", "77")

    async def queued():
        rid, _ = await create_run(
            project=SimpleNamespace(id=77), capability="agent:claude", prompt="p",
            task_id="jobs", account_id="owner", session_id=None, idempotency_key=None,
            wall_timeout_s=30, idle_timeout_s=30, allowed_tools=[], model="claude-sonnet-5-5",
            client_job={"capability": "chat:fast", "provider": "claude-sub",
                        "model": "claude-sub/claude-sonnet-5-5", "tools": [TOOL],
                        "tool_choice": "required"})
        async with get_session() as s:
            (await s.get(DeepJobRow, rid)).status = "running"
        return rid
    cfg = ExecConfig(workspace_root=str(tmp_path), env={}, paid_continuation_verified_off=True)
    clear_adapters()
    try:
        fake = FakeAgentCLI([ev.RunCompleted(final_text="must not run")])
        register_adapter("agent:claude", lambda: fake)
        rid = await queued()
        assert await execute_run(rid, cfg) == "failed"
        assert fake.started is None
        assert (await list_events(rid))[-1].payload["error_class"] == "route_disabled"
        monkeypatch.setattr(settings, "AGENT_SUBSCRIPTION_CLAUDE_TOOLS_ENABLED", True)
        monkeypatch.setattr(settings, "AGENT_SUBSCRIPTION_CLAUDE_TOOLS_VERIFIED_MODELS",
                            '["claude-sub/claude-sonnet-5-5"]')
        ran = FakeAgentCLI([ev.RunCompleted(final_text="ok")])
        register_adapter("agent:claude", lambda: ran)
        rid = await queued()
        assert await execute_run(rid, cfg) == "completed"
        assert ran.started is not None and ran.started.client_tools == [TOOL]
    finally:
        clear_adapters()
