"""Claude client tools through the broker stdio MCP bridge (no real `claude` binary)."""
from __future__ import annotations

import asyncio
import io
import json
import socket
import subprocess
import sys
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException, Response

from aibroker.agent import claude_tools, client_tools, mcp_bridge, real_cli
from aibroker.agent import events as ev
from aibroker.agent.adapter import AgentRunSpec
from aibroker.agent.claude_tools import ToolRelay, run_claude_tool_turn, write_bridge_files
from aibroker.agent.client_tools import ClientToolConflict, publish_call, submit_result
from aibroker.agent.constants import AGENT_CAPABILITIES
from aibroker.agent.executor import ExecConfig, execute_run
from aibroker.agent.fake import FakeAgentCLI
from aibroker.agent.lock import StaleLease
from aibroker.agent.registry import clear_adapters, register_adapter
from aibroker.auth import generate_project_key, hash_project_key
from aibroker.config import get_settings
from aibroker.db import get_session
from aibroker.db.models import AgentRunRow, DeepJobRow, ProjectRow
from aibroker.providers import catalog
from aibroker.providers.registry import ModelSpec
from aibroker.routes import proxy

TOOLS = [{"type": "function", "function": {
    "name": "lookup", "description": "Find an id",
    "parameters": {"type": "object", "properties": {"id": {"type": "integer"}},
                   "required": ["id"]}}}]
MODEL = "claude-sonnet-5-5"
needs_unix = pytest.mark.skipif(not hasattr(asyncio, "start_unix_server"),
                                reason="Unix sockets (the worker runs on Linux)")


@pytest.fixture(autouse=True)
def _no_announce_wait(monkeypatch):
    monkeypatch.setattr(claude_tools, "ANNOUNCE_WAIT_S", 0.0)


async def _job(capability: str = "agent:claude", tools=TOOLS, choice="auto") -> int:
    key = generate_project_key()
    async with get_session() as s:
        s.add(ProjectRow(id=93, name="claude-tool-test", project_key_hash=hash_project_key(key),
                         project_key_prefix=key[:12], allowed_scopes=["llm:agent"],
                         is_active=True, notes=""))
        await s.flush()
        row = DeepJobRow(project_id=93, capability=capability, status="running",
                         request={"model": MODEL, "account_id": "owner",
                                  "client_job": {"tools": tools, "tool_choice": choice}})
        s.add(row)
        await s.flush()
        s.add(AgentRunSpecRow(row.id, capability))
        return row.id


def AgentRunSpecRow(run_id: int, capability: str) -> AgentRunRow:  # noqa: N802 - fixture factory
    return AgentRunRow(run_id=run_id, project_id=93, capability=capability,
                       task_id="tool-fixture", account_id="owner", model=MODEL)


def _spec(run_id: int = 1, workspace: str = "/w/run-1", **kw) -> AgentRunSpec:
    base = {"run_id": run_id, "capability": "agent:claude", "prompt": "Find id 7",
            "workspace": workspace, "account_id": "owner", "model": MODEL,
            "client_tools": TOOLS, "tool_choice": "auto"}
    base.update(kw)
    return AgentRunSpec(**base)


async def _pending(job_id: int) -> dict:
    for _ in range(200):
        async with get_session() as s:
            call = ((await s.get(DeepJobRow, job_id)).result_meta or {}).get("client_tool")
        if call:
            return call
        await asyncio.sleep(0.01)
    raise AssertionError("no call was published")


# --- MCP bridge protocol -------------------------------------------------------------------------


def _rpc(method: str, params=None, msg_id=1) -> str:
    return json.dumps({"jsonrpc": "2.0", "id": msg_id, "method": method,
                       **({"params": params} if params is not None else {})}) + "\n"


def test_stdio_handshake_and_tools_list_in_a_real_subprocess(tmp_path):
    tools_file = tmp_path / "tools.json"
    tools_file.write_text(json.dumps(claude_tools.mcp_tool_list(TOOLS)))
    stdin = (_rpc("initialize", {"protocolVersion": "2025-06-18"}, 1)
             + json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n"
             + _rpc("tools/list", None, 2))
    out = subprocess.run([sys.executable, "-m", "aibroker.agent.mcp_bridge",
                          "--socket", str(tmp_path / "none.sock"), "--tools", str(tools_file)],
                         input=stdin, capture_output=True, text=True, timeout=30, check=True)
    first, second = (json.loads(line) for line in out.stdout.splitlines())
    assert first["result"]["protocolVersion"] == "2025-06-18"
    assert first["result"]["capabilities"] == {"tools": {}}
    assert second["result"]["tools"] == [{
        "name": "lookup", "description": "Find an id",
        "inputSchema": TOOLS[0]["function"]["parameters"]}]
    assert out.stdout.count("\n") == 2          # the notification got no reply


def test_bridge_dispatch_errors_and_forwarding():
    tools = claude_tools.mcp_tool_list(TOOLS)
    seen = []

    def forward(name, arguments):
        seen.append((name, arguments))
        return False, "found"

    out = io.StringIO()
    mcp_bridge.serve([
        "\n", "not json\n", json.dumps([1]) + "\n", _rpc("ping"), _rpc("nope", None, 3),
        _rpc("tools/call", {"name": "other", "arguments": {}}, 4),
        _rpc("tools/call", {"name": "lookup", "arguments": {"id": 1}}, 5),
        _rpc("tools/call", {"name": "lookup"}, 6), _rpc("initialize", {}, 7),
    ], out, tools, forward)
    replies = [json.loads(line) for line in out.getvalue().splitlines()]
    assert [r.get("error", {}).get("code") for r in replies[:3]] == [-32700, -32600, None]
    assert replies[2]["result"] == {}
    assert replies[3]["error"]["code"] == -32601 and replies[4]["error"]["code"] == -32602
    assert replies[5]["result"] == {"content": [{"type": "text", "text": "found"}],
                                    "isError": False}
    assert seen == [("lookup", {"id": 1}), ("lookup", {})]
    assert replies[7]["result"]["protocolVersion"] == mcp_bridge.DEFAULT_PROTOCOL


def test_load_tools_requires_a_list(tmp_path):
    bad = tmp_path / "t.json"
    bad.write_text("{}")
    with pytest.raises(ValueError):
        mcp_bridge.load_tools(str(bad))
    ok = tmp_path / "ok.json"
    ok.write_text("[]")
    assert mcp_bridge.load_tools(str(ok)) == []


def test_main_wires_arguments(tmp_path, monkeypatch):
    tools_file = tmp_path / "t.json"
    tools_file.write_text("[]")
    monkeypatch.setattr(sys, "stdin", io.StringIO(_rpc("ping")))
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    assert mcp_bridge.main(["--socket", "x", "--tools", str(tools_file)]) == 0
    assert json.loads(out.getvalue())["result"] == {}


def test_socket_forward_reports_an_unreachable_relay(tmp_path):
    if not hasattr(socket, "AF_UNIX"):
        pytest.skip("no Unix sockets")
    assert mcp_bridge.socket_forward(str(tmp_path / "gone.sock"))("lookup", {}) == (
        True, "the broker could not deliver this tool call")


# --- ToolRelay: publish -> poll -> result, deadline, cancel, schema ------------------------------


async def test_call_publishes_poll_shows_tool_calls_and_result_flows_back():
    job_id = await _job()
    relay = ToolRelay(_spec(run_id=job_id), lambda: None)
    task = asyncio.create_task(relay.call("lookup", {"id": 3}))
    call = await _pending(job_id)
    assert call["call_id"] == "call_1" and call["name"] == "lookup"
    async with get_session() as s:
        response = proxy._job_response(await s.get(DeepJobRow, job_id))
    assert response.status == "pending" and response.finish_reason == "tool_calls"
    assert response.tool_calls == [{"id": "call_1", "type": "function",
                                    "function": {"name": "lookup", "arguments": '{"id":3}'}}]
    assert await submit_result(job_id, 93, "call_1", True, '{"found":3}') is True
    assert await task == (False, '{"found":3}')
    assert relay.calls == 1 and relay.failure is None
    second = asyncio.create_task(relay.call("lookup", {"id": 4}))
    assert (await _pending(job_id))["call_id"] == "call_2"
    await submit_result(job_id, 93, "call_2", False, "boom")
    assert await second == (True, "boom")        # success=false -> isError


async def test_schema_invalid_arguments_are_an_error_and_never_published(monkeypatch):
    job_id = await _job()
    publish = AsyncMock()
    monkeypatch.setattr(claude_tools, "publish_call", publish)
    relay = ToolRelay(_spec(run_id=job_id), lambda: None)
    is_error, text = await relay.call("lookup", {"id": "not-an-int"})
    assert is_error and "schema" in text and "InvalidToolArguments" in text
    assert (await relay.call("ghost", {}))[0] is True
    publish.assert_not_awaited()
    assert relay.failure is None and relay.calls == 0      # the model may retry


async def test_invalid_calls_are_capped_then_the_run_fails(monkeypatch):
    job_id = await _job()
    killed = []
    relay = ToolRelay(_spec(run_id=job_id), lambda: killed.append(True))
    for _ in range(claude_tools.MAX_INVALID_CALLS - 1):
        assert "schema" in (await relay.call("lookup", {"id": "x"}))[1]
        assert relay.failure is None
    is_error, text = await relay.call("lookup", {"id": "x"})
    assert is_error and "too many" in text and killed == [True]
    assert relay.failure.error_class == "client_tool_protocol"
    assert relay.invalid == claude_tools.MAX_INVALID_CALLS


async def test_invalid_calls_count_toward_the_total_call_cap():
    job_id = await _job()
    relay = ToolRelay(_spec(run_id=job_id), lambda: None)
    relay.calls = client_tools.MAX_CALLS          # 16 valid calls already made
    assert "too many" in (await relay.call("lookup", {"id": "x"}))[1]
    assert relay.failure.error_class == "client_tool_protocol"


@pytest.mark.parametrize("target", ["take_result", "publish_call"])
async def test_stale_lease_or_db_error_fails_the_run_and_withdraws_the_call(monkeypatch, target):
    job_id = await _job()
    real_publish = claude_tools.publish_call

    async def boom(*args, **kwargs):
        raise StaleLease("taken over") if target == "take_result" else RuntimeError("db down")

    if target == "take_result":
        monkeypatch.setattr(claude_tools, "take_result", boom)
    else:
        async def publish_then_boom(*args, **kwargs):
            await real_publish(*args, **kwargs)
            raise RuntimeError("db down")
        monkeypatch.setattr(claude_tools, "publish_call", publish_then_boom)
    killed = []
    relay = ToolRelay(_spec(run_id=job_id), lambda: killed.append(True))
    is_error, _ = await relay.call("lookup", {"id": 1})
    assert is_error and killed == [True]
    assert relay.failure.error_class == "adapter_error"
    async with get_session() as s:
        assert proxy._job_response(await s.get(DeepJobRow, job_id)).tool_calls is None


async def test_withdraw_failure_is_logged_not_raised(monkeypatch, caplog):
    job_id = await _job()

    async def boom(*args, **kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(claude_tools, "take_result", boom)
    monkeypatch.setattr(claude_tools, "clear_pending_call", boom)
    relay = ToolRelay(_spec(run_id=job_id), lambda: None)
    with caplog.at_level("ERROR"):
        assert (await relay.call("lookup", {"id": 1}))[0] is True
    assert relay.failure.error_class == "adapter_error"
    assert any("could not withdraw" in r.message for r in caplog.records)


async def test_call_adopts_the_cli_tool_use_id_announced_for_the_same_arguments():
    job_id = await _job()
    relay = ToolRelay(_spec(run_id=job_id), lambda: None)
    relay.announce("toolu_other", "lookup", {"id": 99})
    relay.announce("toolu_mine", "lookup", {"id": 3})
    relay.announce("x" * 200, "lookup", {"id": 3})        # over-long ids are ignored
    task = asyncio.create_task(relay.call("lookup", {"id": 3}))
    call = await _pending(job_id)
    assert call["call_id"] == "toolu_mine"                # the id the event stream carries
    await submit_result(job_id, 93, "toolu_mine", True, "ok")
    assert await task == (False, "ok")
    assert [a[0] for a in relay._announced] == ["toolu_other"]


async def test_call_waits_briefly_for_a_late_announcement(monkeypatch):
    job_id = await _job()
    monkeypatch.setattr(claude_tools, "ANNOUNCE_WAIT_S", 5.0)
    relay = ToolRelay(_spec(run_id=job_id), lambda: None)
    task = asyncio.create_task(relay.call("lookup", {"id": 3}))
    await asyncio.sleep(0.05)
    relay.announce("toolu_late", "lookup", {"id": 3})
    assert (await _pending(job_id))["call_id"] == "toolu_late"
    relay.stop()
    await task


async def test_expired_call_fails_the_run_like_codex(monkeypatch):
    job_id = await _job()
    monkeypatch.setattr(client_tools, "TOOL_WAIT_S", 0)
    killed = []
    relay = ToolRelay(_spec(run_id=job_id), lambda: killed.append(True))
    is_error, _ = await relay.call("lookup", {"id": 1})
    assert is_error and killed == [True]
    assert isinstance(relay.failure, ev.RunFailed)
    assert relay.failure.error_class == "client_tool_protocol" and "expired" in relay.failure.message
    assert (await relay.call("lookup", {"id": 1})) == (True, "the run is stopping")


async def test_cancel_during_the_wait_cancels_the_run():
    job_id = await _job()
    killed = []
    relay = ToolRelay(_spec(run_id=job_id), lambda: killed.append(True))
    task = asyncio.create_task(relay.call("lookup", {"id": 1}))
    await _pending(job_id)
    async with get_session() as s:
        (await s.get(AgentRunRow, job_id)).cancel_requested = True
    assert (await task)[0] is True
    assert isinstance(relay.failure, ev.RunCancelled) and killed == [True]


async def test_publish_refusal_is_a_protocol_failure(monkeypatch):
    job_id = await _job()
    await publish_call(job_id, "call_1", "lookup", {"id": 1})     # a call is already pending
    relay = ToolRelay(_spec(run_id=job_id), lambda: None)
    assert (await relay.call("lookup", {"id": 2}))[0] is True
    assert relay.failure.error_class == "client_tool_protocol"


async def test_stop_releases_a_waiting_call():
    job_id = await _job()
    relay = ToolRelay(_spec(run_id=job_id), lambda: None)
    task = asyncio.create_task(relay.call("lookup", {"id": 1}))
    await _pending(job_id)
    relay.stop()
    assert await task == (True, "the run is stopping")


@needs_unix
async def test_socket_round_trip_through_the_real_shim_forward(tmp_path):
    job_id = await _job()
    relay = ToolRelay(_spec(run_id=job_id), lambda: None)
    sock = tmp_path / "b.sock"
    server = await relay.serve(sock)
    try:
        forward = mcp_bridge.socket_forward(str(sock))
        box: list = []
        thread = threading.Thread(target=lambda: box.append(forward("lookup", {"id": 5})))
        thread.start()
        await _pending(job_id)
        await submit_result(job_id, 93, "call_1", True, "five")
        await asyncio.to_thread(thread.join, 10)
        assert box == [(False, "five")]
        reader, writer = await asyncio.open_unix_connection(str(sock))
        writer.write(b"garbage\n")
        reply = json.loads(await reader.readline())
        assert reply["is_error"] is True and "malformed" in reply["text"]
        writer.close()
    finally:
        relay.stop()
        server.close()
        await server.wait_closed()


# --- run_claude_tool_turn: argv, files, events ---------------------------------------------------


class _FakeServer:
    def close(self):
        return None

    async def wait_closed(self):
        return None


class _Adapter:
    binary = "claude"

    def __init__(self, items, problem=None, hook=None):
        self._env = {"CLAUDE_CONFIG_DIR": "/home/app/cs"}
        self.items, self.problem, self.hook = items, problem, hook
        self.argv = None
        self.cancelled = 0

    def preflight(self, run):
        return self.problem

    async def preflight_async(self, run):
        return None

    async def cancel(self):
        self.cancelled += 1

    async def exec_argv(self, argv, run):
        self.argv = argv
        if self.hook:
            self.hook()
        for item in self.items:
            yield item


@pytest.fixture
def relays(monkeypatch):
    made = []

    class Capturing(ToolRelay):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            made.append(self)

        async def serve(self, path):
            return _FakeServer()

    monkeypatch.setattr(claude_tools, "ToolRelay", Capturing)
    return made


async def _drive(adapter, run):
    return [item async for item in run_claude_tool_turn(adapter, run)]


async def test_turn_runs_with_mcp_only_withholds_results_and_completes(tmp_path, relays):
    adapter = _Adapter([ev.SessionAssigned(session_id="s1"),
                        ev.ToolCall(call_id="t1", name="mcp__broker__lookup", input={"id": 7}),
                        ev.ToolResult(call_id="t1", ok=True, output="SENSITIVE CLIENT DATA"),
                        ev.RunCompleted(final_text="Found 7")],
                       hook=lambda: setattr(relays[0], "calls", 1))
    events = await _drive(adapter, _spec(workspace=str(tmp_path), tool_choice="required"))
    assert [e.type for e in events] == ["session.assigned", "tool.call", "tool.result",
                                        "run.completed"]
    assert events[2].output == claude_tools.WITHHELD
    assert (events[1].name, events[1].call_id) == ("lookup", "t1")   # bare name, CLI id
    assert relays[0]._announced == [("t1", "lookup", '{"id":7}')]    # the call will adopt "t1"
    assert "SENSITIVE" not in json.dumps([e.__dict__ for e in events], default=str)
    argv = adapter.argv
    assert argv[argv.index("--mcp-config") + 1].endswith("mcp.json")
    assert "--strict-mcp-config" in argv and "--dangerously-skip-permissions" not in argv
    assert (tmp_path / claude_tools.BRIDGE_DIR / "tools.json").is_file()


async def test_cli_failure_after_relay_cancel_is_reported_as_cancelled(tmp_path, relays):
    adapter = _Adapter([ev.RunFailed(error_class="cli_error", message="killed")],
                       hook=lambda: relays[0]._fail(ev.RunCancelled()))
    events = await _drive(adapter, _spec(workspace=str(tmp_path)))
    assert [e.type for e in events] == ["run.cancelled"]
    assert adapter.cancelled == 1            # the relay killed the CLI group


async def test_required_tool_not_called_fails_like_codex(tmp_path, relays):
    adapter = _Adapter([ev.RunCompleted(final_text="no tool")])
    events = await _drive(adapter, _spec(workspace=str(tmp_path), tool_choice="required"))
    assert events[-1].error_class == "client_tool_protocol"
    adapter = _Adapter([ev.RunCompleted(final_text="fine")])
    assert (await _drive(adapter, _spec(workspace=str(tmp_path))))[-1].type == "run.completed"


async def test_turn_refuses_cli_tools_attachments_settings_and_long_socket(tmp_path, relays):
    adapter = _Adapter([])
    for kw in ({"allowed_tools": ["Read(src/**)"]}, {"attachments": ["inputs/a.png"]}):
        events = await _drive(adapter, _spec(workspace=str(tmp_path), **kw))
        assert events[0].error_class == "tool_invalid"
    refused = await _drive(_Adapter([], problem=("settings_refused", "bad")),
                           _spec(workspace=str(tmp_path)))
    assert refused[0].error_class == "settings_refused"
    deep = tmp_path / ("d" * 120)
    deep.mkdir()
    assert (await _drive(adapter, _spec(workspace=str(deep))))[0].error_class == "tool_invalid"
    assert adapter.argv is None


async def test_claude_adapter_routes_tools_to_the_bridge_and_none_to_plain(tmp_path, monkeypatch):
    plain, bridged = [], []

    async def fake_plain(self, run):
        plain.append(run)
        yield ev.RunCompleted(final_text="p")

    async def fake_bridge(adapter, run):
        bridged.append(run)
        yield ev.RunCompleted(final_text="b")

    monkeypatch.setattr(real_cli.SubprocessCLI, "start", fake_plain)
    monkeypatch.setattr(claude_tools, "run_claude_tool_turn", fake_bridge)
    cli = real_cli.ClaudeCLI(env={"CLAUDE_CONFIG_DIR": "/c"})
    assert [e async for e in cli.start(_spec(tool_choice="none"))][0].final_text == "p"
    assert [e async for e in cli.start(_spec(client_tools=[]))][0].final_text == "p"
    assert [e async for e in cli.start(_spec())][0].final_text == "b"
    assert len(plain) == 2 and len(bridged) == 1


# --- argv and config file ------------------------------------------------------------------------


def test_argv_allows_only_the_broker_mcp_tools():
    argv = real_cli.build_claude_argv(_spec(), config_dir="/home/app/cs", mcp_config="/w/m.json")
    assert argv[argv.index("--tools") + 1] == ""
    assert argv[argv.index("--mcp-config") + 1] == "/w/m.json"
    allowed = [a for a in argv if a.startswith("--allowedTools")]
    assert allowed == ["--allowedTools=mcp__broker__*"]
    denied = next(a for a in argv if a.startswith("--disallowedTools=")).split("=", 1)[1].split(",")
    assert {"Bash", "Read", "Write", "Edit", "WebFetch", "Agent", "Task", "Skill", "TodoWrite",
            "LS", "NotebookRead"} <= set(denied)
    assert "ToolSearch" not in denied
    assert not any(t in allowed[0] for t in ("Bash", "Read", "Write", "Edit"))
    with pytest.raises(ValueError, match="exclude"):
        real_cli.build_claude_argv(_spec(allowed_tools=["Read(src/**)"]), mcp_config="/w/m.json")
    plain = real_cli.build_claude_argv(_spec(), config_dir="/c")
    assert "--mcp-config" not in plain and "--strict-mcp-config" in plain
    assert not any(a.startswith("--allowedTools") for a in plain)


def test_mcp_config_holds_command_and_args_only_no_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:hunter2@db/prod")
    monkeypatch.setenv("INTERNAL_SECRET", "hunter2")
    config, sock = write_bridge_files(str(tmp_path), TOOLS)
    text = config.read_text()
    data = json.loads(text)
    server = data["mcpServers"]["broker"]
    assert set(data) == {"mcpServers"} and set(server) == {"type", "command", "args"}
    assert server["args"][:3] == ["-I", "-m", "aibroker.agent.mcp_bridge"]
    assert "hunter2" not in text and "postgresql" not in text
    assert sock.parent == config.parent and sock.name == "b.sock"
    assert f"mcp__{claude_tools.SERVER_NAME}__*" == real_cli.MCP_ALLOW


# --- gates ---------------------------------------------------------------------------------------


def _ctx() -> SimpleNamespace:
    return SimpleNamespace(project=SimpleNamespace(id=11, allowed_scopes=["llm:agent", "llm:chat"]),
                           has_scope=lambda _s: True)


def _body() -> proxy.ChatRequest:
    return proxy.ChatRequest.model_validate({
        "messages": [{"role": "user", "content": "hi"}], "model": f"claude-sub/{MODEL}",
        "tools": TOOLS})


async def test_submit_with_tools_is_503_while_the_gate_is_off(monkeypatch):
    create = AsyncMock()
    monkeypatch.setattr(proxy, "create_run", create)
    monkeypatch.setattr(proxy, "project_allowed", lambda _id: True)
    monkeypatch.setattr(proxy, "policy_violation", lambda *_: None)
    with pytest.raises(HTTPException) as exc:
        await proxy.jobs_submit(_body(), Response(), capability="chat:fast", ctx=_ctx())
    assert exc.value.status_code == 503 and "Claude" in exc.value.detail
    monkeypatch.setattr(get_settings(), "AGENT_SUBSCRIPTION_CLAUDE_TOOLS_ENABLED", True)
    with pytest.raises(HTTPException) as exc:           # enabled, but the model is not verified
        await proxy.jobs_submit(_body(), Response(), capability="chat:fast", ctx=_ctx())
    assert exc.value.status_code == 503
    create.assert_not_awaited()


async def test_submit_with_tools_enqueues_when_enabled_and_verified(monkeypatch):
    create = AsyncMock(return_value=(7, True))
    monkeypatch.setattr(proxy, "create_run", create)
    monkeypatch.setattr(proxy, "admit_request", AsyncMock())
    monkeypatch.setattr(proxy, "wake_worker", AsyncMock())
    monkeypatch.setattr(proxy, "project_allowed", lambda _id: True)
    monkeypatch.setattr(proxy, "policy_violation", lambda *_: None)
    monkeypatch.setattr(get_settings(), "AGENT_SUBSCRIPTION_CLAUDE_TOOLS_ENABLED", True)
    monkeypatch.setattr(get_settings(), "AGENT_SUBSCRIPTION_CLAUDE_TOOLS_VERIFIED_MODELS",
                        f'["claude-sub/{MODEL}"]')
    result = await proxy.jobs_submit(_body(), Response(), capability="chat:fast", ctx=_ctx())
    assert result.job_id == 7
    kwargs = create.await_args.kwargs
    assert kwargs["capability"] == "agent:claude" and kwargs["allowed_tools"] == []
    assert kwargs["client_job"]["tools"][0]["function"]["name"] == "lookup"


def test_catalog_supports_tools_needs_enabled_and_verified(monkeypatch):
    spec = ModelSpec(id=f"claude-sub/{MODEL}", provider="claude-sub", transport="agent",
                     capabilities=frozenset({"chat:fast"}), pricing="subscription",
                     register=False)
    assert catalog.supports_tools(spec) is False
    monkeypatch.setattr(get_settings(), "AGENT_SUBSCRIPTION_CLAUDE_TOOLS_ENABLED", True)
    assert catalog.supports_tools(spec) is False
    monkeypatch.setattr(get_settings(), "AGENT_SUBSCRIPTION_CLAUDE_TOOLS_VERIFIED_MODELS",
                        f'["claude-sub/{MODEL}"]')
    assert catalog.supports_tools(spec) is True
    monkeypatch.setattr(get_settings(), "AGENT_SUBSCRIPTION_CLAUDE_TOOLS_VERIFIED_MODELS",
                        '["codex-sub/gpt-6.1-sol"]')       # wrong provider id: fails closed
    assert catalog.supports_tools(spec) is False


async def test_executor_refuses_claude_tools_at_run_time_when_gate_is_off(tmp_path):
    job_id = await _job()
    clear_adapters()
    fake = FakeAgentCLI([ev.RunCompleted(final_text="must not run")])
    register_adapter("agent:claude", lambda: fake)
    assert "agent:claude" in AGENT_CAPABILITIES
    assert await execute_run(job_id, ExecConfig(workspace_root=str(tmp_path))) == "failed"
    assert fake.started is None
    clear_adapters()


def test_client_tools_conflict_type_is_reused():
    assert issubclass(client_tools.ClientToolCancelled, ClientToolConflict)
