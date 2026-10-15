"""App Server protocol fixture: a tool waits for the caller and resumes the same turn."""
from __future__ import annotations

import asyncio
import json
import logging

import pytest

from aibroker.agent import events as ev
from aibroker.agent.adapter import AgentRunSpec
from aibroker.agent.client_tools import ClientToolConflict
from aibroker.agent.codex_app_server import _is_response, run_tool_turn

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


class _ScriptedWriter(_Writer):
    """Server->client requests use their own id counter, unrelated to ours."""

    def __init__(self, reader, server_ids, method="item/tool/call", pre_turn=None):
        super().__init__(reader)
        self.server_ids = list(server_ids)
        self.method = method
        self.pre_turn = pre_turn
        self.answered = 0

    def _server_request(self, index):
        self.reader.put({"id": self.server_ids[index], "method": self.method, "params": {
            "threadId": "thread-1", "turnId": "turn-1", "namespace": None,
            "callId": f"call-{index}", "tool": "lookup", "arguments": {"id": index}}})

    def write(self, data):
        message = json.loads(data)
        method = message.get("method")
        if method == "turn/start":
            self.sent.append(message)
            if self.pre_turn:
                self.reader.put(self.pre_turn)
            self.reader.put({"id": 3, "result": {"turn": {"id": "turn-1"}}})
            self._server_request(0)
        elif "method" not in message and "result" in message and message["id"] in self.server_ids:
            self.sent.append(message)
            self.answered += 1
            if self.answered < len(self.server_ids):
                self._server_request(self.answered)
            else:
                self.reader.put({"method": "item/completed", "params": {
                    "threadId": "thread-1", "turnId": "turn-1",
                    "item": {"type": "agentMessage", "text": "done"}}})
                self.reader.put({"method": "turn/completed", "params": {
                    "threadId": "thread-1", "turn": {"id": "turn-1", "status": "completed"}}})
        else:
            super().write(data)


async def _drive(monkeypatch, tmp_path, server_ids, **kwargs):
    run, home = _run(tmp_path)
    process = _Process()
    process.stdin = _ScriptedWriter(process.stdout, server_ids, **kwargs)
    async def spawn(*_args, **_kw):
        return process
    async def publish(*args):
        return None
    async def take(*args):
        return True, "{}"
    monkeypatch.setattr("aibroker.agent.codex_app_server.asyncio.create_subprocess_exec", spawn)
    monkeypatch.setattr("aibroker.agent.codex_app_server.permission_overrides",
                        lambda _home: ["a=b"])
    monkeypatch.setattr("aibroker.agent.codex_app_server.publish_call", publish)
    monkeypatch.setattr("aibroker.agent.codex_app_server.take_result", take)
    events = [item async for item in run_tool_turn(_Adapter(home), run)]
    return events, process


@pytest.mark.parametrize("server_id", [1, 2, 3])
async def test_server_tool_call_with_our_own_ids_is_not_our_response(
        monkeypatch, tmp_path, server_id):
    events, process = await _drive(monkeypatch, tmp_path, [server_id])
    assert events[-1].type == "run.completed", events[-1]
    assert [e.type for e in events].count("tool.call") == 1
    assert process.stdin.sent[-1]["id"] == server_id


async def test_five_sequential_tool_calls_with_server_ids_0_to_4_complete(monkeypatch, tmp_path):
    events, _ = await _drive(monkeypatch, tmp_path, [0, 1, 2, 3, 4])
    assert events[-1].type == "run.completed", events[-1]
    assert [e.type for e in events].count("tool.call") == 5


async def test_turn_response_after_notification_and_same_id_request(monkeypatch, tmp_path):
    pre = {"method": "thread/status/changed", "params": {"threadId": "thread-1"}}
    events, _ = await _drive(monkeypatch, tmp_path, [3], pre_turn=pre)
    assert events[-1].type == "run.completed", events[-1]


async def test_unapproved_server_request_with_id_3_still_fails_closed(monkeypatch, tmp_path):
    events, _ = await _drive(monkeypatch, tmp_path, [3], method="tool/requestUserInput")
    assert events[-1].type == "run.failed"
    assert "unapproved operation" in events[-1].message


async def test_server_request_before_thread_response_fails_closed(monkeypatch, tmp_path):
    run, home = _run(tmp_path)
    process = _Process()
    class W(_Writer):
        def write(self, data):
            if json.loads(data).get("method") == "thread/start":
                self.reader.put({"id": 2, "method": "item/tool/call", "params": {}})
                return
            super().write(data)
    process.stdin = W(process.stdout)
    async def spawn(*_args, **_kw):
        return process
    monkeypatch.setattr("aibroker.agent.codex_app_server.asyncio.create_subprocess_exec", spawn)
    monkeypatch.setattr("aibroker.agent.codex_app_server.permission_overrides",
                        lambda _home: ["a=b"])
    events = [item async for item in run_tool_turn(_Adapter(home), run)]
    assert events[-1].type == "run.failed"
    assert "unexpected operation" in events[-1].message


SECRET = "PAYLOAD-SENTINEL-123"
_CALL = {"threadId": "thread-1", "turnId": "turn-1", "namespace": None,
         "callId": "call-0", "tool": "lookup", "arguments": {"id": 0}}
_DONE = [{"method": "item/completed", "params": {
              "threadId": "thread-1", "turnId": "turn-1",
              "item": {"type": "agentMessage", "text": "done"}}},
         {"method": "turn/completed", "params": {
              "threadId": "thread-1", "turn": {"id": "turn-1", "status": "completed"}}}]
_TURN_OK = {"id": 3, "result": {"turn": {"id": "turn-1"}}}


class _ScriptWriter(_Writer):
    """Emit ``on_turn`` after turn/start and ``on_reply`` after our first tool reply."""

    def __init__(self, reader, on_turn, on_reply=()):
        super().__init__(reader)
        self.on_turn, self.on_reply = list(on_turn), list(on_reply)

    def write(self, data):
        message = json.loads(data)
        if message.get("method") == "turn/start":
            self.sent.append(message)
            for item in self.on_turn:
                self.reader.put(item)
        elif "method" not in message and "result" in message and message["id"] not in (1, 2, 3):
            self.sent.append(message)
            for item in self.on_reply:
                self.reader.put(item)
            self.on_reply = []
        else:
            super().write(data)


async def _script(monkeypatch, tmp_path, on_turn, on_reply=(), publishes=None, result="{}"):
    run, home = _run(tmp_path)
    process = _Process()
    process.stdin = _ScriptWriter(process.stdout, on_turn, on_reply)

    async def spawn(*_args, **_kw):
        return process

    async def publish(_run_id, call_id, *_rest):
        if publishes is not None:
            publishes.append(call_id)

    async def take(*_args):
        return True, result
    monkeypatch.setattr("aibroker.agent.codex_app_server.asyncio.create_subprocess_exec", spawn)
    monkeypatch.setattr("aibroker.agent.codex_app_server.permission_overrides",
                        lambda _home: ["a=b"])
    monkeypatch.setattr("aibroker.agent.codex_app_server.publish_call", publish)
    monkeypatch.setattr("aibroker.agent.codex_app_server.take_result", take)
    return [item async for item in run_tool_turn(_Adapter(home), run)]


@pytest.mark.parametrize(("message", "expected"), [
    ({"id": 3, "result": {}}, True),
    ({"id": 3, "error": {"code": -1}}, True),
    ({"id": 3, "method": "item/tool/call", "params": {}}, False),
    ({"method": "turn/completed", "params": {}}, False),
    ({"id": 4, "result": {}}, False),
    ({"id": 3, "method": "x", "result": {}}, False),
    ({"id": 3}, True),
    ({}, False),
])
def test_is_response_classification_by_form(message, expected):
    # A reply is matched on id and the ABSENCE of "method"; id+method is a server
    # request, method without id a notification.  A bare {"id": 3} counts as a
    # response here and the caller then refuses it fail-closed (no result to use).
    assert _is_response(message, 3) is expected


async def test_turn_start_error_reply_fails_without_publishing(monkeypatch, tmp_path):
    publishes = []
    events = await _script(monkeypatch, tmp_path,
                           [{"id": 3, "error": {"code": -32000, "message": SECRET}}],
                           publishes=publishes)
    assert events[-1].type == "run.failed"
    assert events[-1].error_class == "client_tool_protocol"
    assert "App Server refused the turn" in events[-1].message
    assert SECRET not in events[-1].message
    assert not publishes
    assert "tool.call" not in [e.type for e in events]


@pytest.mark.parametrize("bad", [{"id": 3}, {"id": 3, "result": {}}])
async def test_malformed_turn_reply_fails_closed(monkeypatch, tmp_path, bad):
    events = await _script(monkeypatch, tmp_path, [bad])
    assert events[-1].type == "run.failed"
    assert "did not assign a turn" in events[-1].message


async def test_id_method_result_is_a_request_not_a_turn_reply(monkeypatch, tmp_path):
    events = await _script(monkeypatch, tmp_path,
                           [{"id": 3, "method": "x/unknown", "result": {"turn": {"id": "t"}}}])
    assert events[-1].type == "run.failed"
    assert "unapproved operation" in events[-1].message


async def test_duplicate_turn_response_does_not_reassign_turn(monkeypatch, tmp_path):
    publishes = []
    dup = {"id": 3, "result": {"turn": {"id": "turn-evil"}}}
    request = {"id": 7, "method": "item/tool/call", "params": _CALL}
    events = await _script(monkeypatch, tmp_path, [_TURN_OK, dup, request], publishes=publishes)
    assert events[-1].type == "run.failed"
    assert "duplicate turn response" in events[-1].message
    assert not publishes


async def test_duplicate_thread_response_after_assignment_is_ignored(monkeypatch, tmp_path):
    dup = {"id": 2, "result": {"thread": {"id": "thread-evil"}}}
    request = {"id": 7, "method": "item/tool/call", "params": _CALL}
    events = await _script(monkeypatch, tmp_path, [_TURN_OK, dup, request], on_reply=_DONE)
    assert events[-1].type == "run.completed", events[-1]
    assert [e.session_id for e in events if e.type == "session.assigned"] == ["thread-1"]


async def test_duplicate_tool_call_same_request_id_and_call_id_fails_closed(monkeypatch, tmp_path):
    publishes = []
    request = {"id": 7, "method": "item/tool/call", "params": _CALL}
    events = await _script(monkeypatch, tmp_path, [_TURN_OK, request, request],
                           on_reply=_DONE, publishes=publishes)
    assert events[-1].type == "run.failed"
    assert "duplicate dynamic tool call" in events[-1].message
    assert publishes == ["call-0"]


async def test_duplicate_call_id_with_new_request_id_fails_closed(monkeypatch, tmp_path):
    publishes = []
    events = await _script(
        monkeypatch, tmp_path,
        [_TURN_OK, {"id": 7, "method": "item/tool/call", "params": _CALL},
         {"id": 8, "method": "item/tool/call", "params": _CALL}],
        on_reply=_DONE, publishes=publishes)
    assert events[-1].type == "run.failed"
    assert publishes == ["call-0"]


async def test_payloads_never_reach_logs_or_result_events(monkeypatch, tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    call = dict(_CALL, arguments={"id": 0, "token": SECRET})
    events = await _script(monkeypatch, tmp_path,
                           [_TURN_OK, {"id": 7, "method": "item/tool/call", "params": call}],
                           on_reply=_DONE, result=json.dumps({"out": SECRET}))
    assert events[-1].type == "run.completed", events[-1]
    assert caplog.records
    assert SECRET not in caplog.text
    for event in events:
        if event.type != "tool.call":  # the call itself carries its arguments by design
            assert SECRET not in event.model_dump_json()


async def test_payloads_never_reach_logs_or_errors_on_failure(monkeypatch, tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    events = await _script(monkeypatch, tmp_path, [
        _TURN_OK, {"id": 9, "method": "tool/requestUserInput",
                   "params": {"secret": SECRET, "arguments": {"k": SECRET}}}])
    assert events[-1].type == "run.failed"
    assert SECRET not in events[-1].model_dump_json()
    assert SECRET not in caplog.text


# --- model-retry inside one run: rejected arguments, reminder turn -------------------------

class _Dyn(_Writer):
    """Answer each of OUR messages with ``react(message)`` -> server messages to emit."""

    def __init__(self, reader, react):
        super().__init__(reader)
        self.react = react

    def write(self, data):
        message = json.loads(data)
        self.sent.append(message)
        if message.get("method") == "initialize":
            self.reader.put({"id": 1, "result": {}})
        elif message.get("method") == "thread/start":
            self.reader.put({"id": 2, "result": {"thread": {"id": "thread-1"}}})
        else:
            for item in self.react(message):
                self.reader.put(item)


def _call(req_id, call_id, args, turn="turn-1", tool="lookup"):
    return {"id": req_id, "method": "item/tool/call", "params": {
        "threadId": "thread-1", "turnId": turn, "namespace": None, "callId": call_id,
        "tool": tool, "arguments": args}}


def _done(turn):
    return [{"method": "item/completed", "params": {"threadId": "thread-1", "turnId": turn,
             "item": {"type": "agentMessage", "text": "done"}}},
            {"method": "turn/completed", "params": {
                "threadId": "thread-1", "turn": {"id": turn, "status": "completed"}}}]


async def _dyn(monkeypatch, tmp_path, react, publish, *, arg_retries=2, reminders=1,
               tool_choice="required", taken=(True, "{}")):
    from dataclasses import replace

    from aibroker.config import get_settings
    run, home = _run(tmp_path)
    run = replace(run, tool_choice=tool_choice)
    process = _Process()
    process.stdin = _Dyn(process.stdout, react)

    async def spawn(*_a, **_k):
        return process

    async def take(*_a):
        return taken
    settings = get_settings()
    monkeypatch.setattr(settings, "AGENT_TOOL_ARG_RETRIES", arg_retries)
    monkeypatch.setattr(settings, "AGENT_TOOL_REQUIRED_REMINDERS", reminders)
    monkeypatch.setattr("aibroker.agent.codex_app_server.asyncio.create_subprocess_exec", spawn)
    monkeypatch.setattr("aibroker.agent.codex_app_server.permission_overrides", lambda _h: ["a=b"])
    monkeypatch.setattr("aibroker.agent.codex_app_server.publish_call", publish)
    monkeypatch.setattr("aibroker.agent.codex_app_server.take_result", take)
    events = [item async for item in run_tool_turn(_Adapter(home), run)]
    return events, process.stdin.sent


def _bad_then_good(published):
    from aibroker.agent.client_tools import ClientToolInvalidArguments

    async def publish(_run, call_id, _name, arguments, _fences):
        if arguments.get("id") == "bad":
            raise ClientToolInvalidArguments("InvalidToolArguments",
                                             "id: type integer (got type str)")
        published.append(call_id)
    return publish


def _replies(sent):
    return [m for m in sent if "result" in m and "method" not in m and m["id"] not in (1, 2, 3)]


async def test_invalid_arguments_are_rejected_to_the_model_and_the_run_continues(
        monkeypatch, tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    published = []

    def react(m):
        if m.get("method") == "turn/start":
            return [_TURN_OK, _call(7, "c-bad", {"id": "bad", "token": SECRET})]
        if m.get("id") == 7:
            return [_call(8, "c-good", {"id": 1})]
        if m.get("id") == 8:
            return _done("turn-1")
        return []
    events, sent = await _dyn(monkeypatch, tmp_path, react, _bad_then_good(published))
    assert events[-1].type == "run.completed", events[-1]
    assert published == ["c-good"]
    first = _replies(sent)[0]
    assert first["id"] == 7 and first["result"]["success"] is False
    text = first["result"]["contentItems"][0]["text"]
    assert "id: type integer (got type str)" in text and "lookup" in text and SECRET not in text
    types = [(e.type, getattr(e, "call_id", None), getattr(e, "ok", None)) for e in events]
    assert ("tool.call", "c-bad", None) not in types
    assert ("tool.result", "c-bad", False) in types
    assert ("tool.call", "c-good", None) in types
    for e in events:
        if e.type != "tool.call":
            assert SECRET not in e.model_dump_json()
    assert SECRET not in caplog.text


async def test_invalid_arguments_beyond_the_retry_budget_fail_with_the_labelled_error(
        monkeypatch, tmp_path):
    published = []

    def react(m):
        if m.get("method") == "turn/start":
            return [_TURN_OK, _call(7, "c1", {"id": "bad"})]
        if m.get("id") in (7, 8):
            return [_call(m["id"] + 1, f"c{m['id']}", {"id": "bad"})]
        return []
    events, sent = await _dyn(monkeypatch, tmp_path, react, _bad_then_good(published),
                              arg_retries=2)
    assert events[-1].type == "run.failed"
    assert ("outside the declared schema [InvalidToolArguments]: id: type integer"
            in events[-1].message)
    assert len(_replies(sent)) == 2 and not published


async def test_zero_arg_retries_fails_on_the_first_invalid_call(monkeypatch, tmp_path):
    def react(m):
        return [_TURN_OK, _call(7, "c1", {"id": "bad"})] if m.get("method") == "turn/start" else []
    events, sent = await _dyn(monkeypatch, tmp_path, react, _bad_then_good([]), arg_retries=0)
    assert events[-1].type == "run.failed" and not _replies(sent)


async def test_a_non_schema_conflict_still_fails_closed_without_a_retry(monkeypatch, tmp_path):
    async def publish(*_a):
        raise ClientToolConflict("job was cancelled")

    def react(m):
        return [_TURN_OK, _call(7, "c1", {"id": 1})] if m.get("method") == "turn/start" else []
    events, sent = await _dyn(monkeypatch, tmp_path, react, publish)
    assert events[-1].type == "run.failed" and "job was cancelled" in events[-1].message
    assert not _replies(sent)


@pytest.mark.parametrize("tool_choice", [
    "required", {"type": "function", "function": {"name": "lookup"}}])
async def test_required_tool_not_called_sends_one_reminder_turn_then_completes(
        monkeypatch, tmp_path, tool_choice):
    published = []

    async def publish(_run, call_id, *_rest):
        published.append(call_id)

    def react(m):
        if m.get("method") == "turn/start" and m["id"] == 3:
            return [_TURN_OK, *_done("turn-1")]
        if m.get("method") == "turn/start" and m["id"] == 4:
            return [{"id": 4, "result": {"turn": {"id": "turn-2"}}},
                    _call(9, "c-late", {"id": 1}, turn="turn-2")]
        if m.get("id") == 9:
            return _done("turn-2")
        return []
    events, sent = await _dyn(monkeypatch, tmp_path, react, publish, tool_choice=tool_choice)
    assert events[-1].type == "run.completed", events[-1]
    starts = [m for m in sent if m.get("method") == "turn/start"]
    assert [m["id"] for m in starts] == [3, 4]
    assert starts[1]["params"]["threadId"] == "thread-1"
    assert "call the lookup tool" in starts[1]["params"]["input"][0]["text"]
    assert published == ["c-late"]


async def test_required_tool_reminder_budget_exhausted_fails_as_before(monkeypatch, tmp_path):
    def react(m):
        if m.get("method") == "turn/start":
            turn = f"turn-{m['id'] - 2}"
            return [{"id": m["id"], "result": {"turn": {"id": turn}}}, *_done(turn)]
        return []
    events, sent = await _dyn(monkeypatch, tmp_path, react, _bad_then_good([]), reminders=1)
    assert events[-1].type == "run.failed"
    assert "required client tool was not called" in events[-1].message
    assert len([m for m in sent if m.get("method") == "turn/start"]) == 2


async def test_zero_reminders_fails_immediately(monkeypatch, tmp_path):
    def react(m):
        return [_TURN_OK, *_done("turn-1")] if m.get("method") == "turn/start" else []
    events, sent = await _dyn(monkeypatch, tmp_path, react, _bad_then_good([]), reminders=0)
    assert events[-1].type == "run.failed"
    assert len([m for m in sent if m.get("method") == "turn/start"]) == 1


async def test_old_turn_response_replayed_after_a_reminder_fails_closed(monkeypatch, tmp_path):
    def react(m):
        if m.get("method") == "turn/start" and m["id"] == 3:
            return [_TURN_OK, *_done("turn-1")]
        if m.get("method") == "turn/start" and m["id"] == 4:
            return [{"id": 3, "result": {"turn": {"id": "turn-evil"}}}]
        return []
    events, _ = await _dyn(monkeypatch, tmp_path, react, _bad_then_good([]))
    assert events[-1].type == "run.failed" and "duplicate turn response" in events[-1].message


def _usage(turn, out):
    return {"method": "thread/tokenUsage/updated", "params": {
        "threadId": "thread-1", "turnId": turn, "tokenUsage": {"total": {
            "inputTokens": 40, "outputTokens": out, "totalTokens": 40 + out,
            "cachedInputTokens": 8, "reasoningOutputTokens": 2}}}}


async def _ok_publish(*_a):
    return None


async def test_terminal_result_acks_then_interrupts_and_completes_with_empty_text(
        monkeypatch, tmp_path):
    def react(m):
        if m.get("method") == "turn/start":
            return [_TURN_OK, _usage("turn-1", 12), _call(7, "c-final", {"id": 1})]
        if m.get("method") == "turn/interrupt":
            return [{"id": 1000, "result": {}},
                    {"method": "item/completed", "params": {"threadId": "thread-1",
                     "turnId": "turn-1", "item": {"type": "agentMessage", "text": "echo"}}},
                    {"method": "turn/completed", "params": {"threadId": "thread-1",
                     "turn": {"id": "turn-1", "status": "interrupted"}}}]
        return []
    events, sent = await _dyn(monkeypatch, tmp_path, react, _ok_publish,
                              taken=(True, "ack", True))
    ack = next(m for m in sent if m.get("id") == 7)
    assert ack["result"]["success"] is True
    interrupt = next(m for m in sent if m.get("method") == "turn/interrupt")
    assert interrupt["id"] == 1000
    assert interrupt["params"] == {"threadId": "thread-1", "turnId": "turn-1"}
    assert sent.index(ack) < sent.index(interrupt)
    assert events[-1] == ev.RunCompleted(final_text="")
    usage = next(e for e in events if e.type == "usage")
    assert (usage.input_tokens, usage.output_tokens, usage.cache_read_tokens) == (40, 12, 8)


async def test_terminal_without_usage_reports_no_usage_not_zero(monkeypatch, tmp_path):
    def react(m):
        if m.get("method") == "turn/start":
            return [_TURN_OK, _call(7, "c-final", {"id": 1})]
        if m.get("method") == "turn/interrupt":
            return [{"id": 1000, "result": {}}, {"method": "turn/completed", "params": {
                "threadId": "thread-1", "turn": {"id": "turn-1", "status": "completed"}}}]
        return []
    events, _ = await _dyn(monkeypatch, tmp_path, react, _ok_publish, taken=(True, "ack", True))
    assert events[-1] == ev.RunCompleted(final_text="")
    assert not [e for e in events if e.type == "usage"]


async def test_terminal_stop_not_confirmed_fails_instead_of_done(monkeypatch, tmp_path):
    monkeypatch.setattr("aibroker.agent.codex_app_server.TERMINAL_STOP_S", 0.05)

    def react(m):
        if m.get("method") == "turn/start":
            return [_TURN_OK, _call(7, "c-final", {"id": 1})]
        return []                                   # the interrupt is never confirmed
    events, _ = await _dyn(monkeypatch, tmp_path, react, _ok_publish, taken=(True, "ack", True))
    assert events[-1].type == "run.failed"
    assert "terminal_stop_unconfirmed" in events[-1].message


async def test_another_tool_call_after_a_terminal_result_fails_closed(monkeypatch, tmp_path):
    def react(m):
        if m.get("method") == "turn/start":
            return [_TURN_OK, _call(7, "c-final", {"id": 1})]
        if m.get("method") == "turn/interrupt":
            return [_call(8, "c-more", {"id": 2})]
        return []
    events, _ = await _dyn(monkeypatch, tmp_path, react, _ok_publish, taken=(True, "ack", True))
    assert events[-1].type == "run.failed"
    assert "another tool after terminal output" in events[-1].message


async def test_a_refused_interrupt_fails_closed(monkeypatch, tmp_path):
    def react(m):
        if m.get("method") == "turn/start":
            return [_TURN_OK, _call(7, "c-final", {"id": 1})]
        if m.get("method") == "turn/interrupt":
            return [{"id": 1000, "error": {"code": -1, "message": "no"}}]
        return []
    events, _ = await _dyn(monkeypatch, tmp_path, react, _ok_publish, taken=(True, "ack", True))
    assert events[-1].type == "run.failed" and "interrupt" in events[-1].message
