"""M1: subscription jobs report the model that actually served them, with its evidence level.
Synthetic events only - no real CLI. The requested model is never substituted."""
from __future__ import annotations

import json
import logging
from types import SimpleNamespace

from aibroker.agent import events as ev
from aibroker.agent.codex_app_server import run_tool_turn
from aibroker.agent.parsers import ClaudeParser, CodexParser
from aibroker.agent.runs import (
    add_model_observation,
    append_event,
    create_run,
    get_run,
    served_model_meta,
)
from aibroker.auth import generate_project_key, hash_project_key
from aibroker.db import get_session
from aibroker.db.models import ProjectRow
from aibroker.routes import proxy
from tests.test_agent_codex_app_server import _Adapter, _Process, _run, _Writer

REQUESTED = "codex-sub/gpt-6.1-sol"


def _line(**kw):
    return json.dumps(kw)


# --- parsers -------------------------------------------------------------------------------


def test_claude_parser_emits_init_and_response_observations():
    p = ClaudeParser()
    init = p.feed(_line(type="system", subtype="init", session_id="s1", model="claude-sonnet-5-5"))
    assert [e.type for e in init] == ["session.assigned", "model.observed"]
    assert (init[1].model, init[1].kind, init[1].source) == (
        "claude-sonnet-5-5", "session_init", "claude.system.init")
    out = p.feed(_line(type="assistant", message={
        "model": "claude-sonnet-5-5-20260101", "content": [{"type": "text", "text": "hi"}]}))
    assert [e.type for e in out] == ["model.observed", "message.delta"]
    assert (out[0].kind, out[0].source) == ("response", "claude.assistant.message")


def test_claude_parser_never_fabricates_a_model():
    p = ClaudeParser()
    assert [e.type for e in p.feed(_line(type="system", subtype="init", session_id="s"))] == [
        "session.assigned"]
    assert p.feed(_line(type="assistant", message={"model": "", "content": []})) == []
    assert p.feed(_line(type="assistant", message={"model": 5, "content": []})) == []


def test_codex_parser_logs_key_names_once_and_never_values(caplog):
    secret = "sk-SECRET-looking-value-123"
    p = CodexParser()
    with caplog.at_level(logging.INFO, logger="aibroker.agent.parsers"):
        p.feed(_line(type="thread.started", thread_id=secret, extra_key=secret))
        p.feed(_line(type="thread.started", thread_id="other", extra_key="other"))   # same shape
        p.feed(_line(type="item.completed", item={"type": "agent_message", "text": secret,
                                                  "weird_field": secret}))
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert secret not in text
    assert text.count("type=thread.started") == 1       # deduped per shape
    assert "extra_key" not in text and "weird_field" not in text and "type=item.completed" in text
    assert "thread_id" in text and "<key>x1" in text       # known names kept, unknown only counted
    # no model is ever invented for the exec path, even if a "model" key shows up
    out = p.feed(_line(type="thread.started", thread_id="x", model="m"))
    assert "model.observed" not in [e.type for e in out]


# --- app server ----------------------------------------------------------------------------


class _ThreadWriter(_Writer):
    thread_result: dict = {}

    def write(self, data):
        message = json.loads(data)
        if message.get("method") == "thread/start":
            self.sent.append(message)
            self.reader.put({"id": 2, "result": self.thread_result})
        else:
            super().write(data)


async def _app_server_events(monkeypatch, tmp_path, thread_result):
    run, home = _run(tmp_path)
    process = _Process()
    writer = _ThreadWriter(process.stdout)
    writer.thread_result = thread_result
    process.stdin = writer

    async def spawn(*_a, **_kw):
        return process

    async def publish(*_a):
        return None

    async def take(*_a):
        return True, "{}"
    monkeypatch.setattr("aibroker.agent.codex_app_server.asyncio.create_subprocess_exec", spawn)
    monkeypatch.setattr("aibroker.agent.codex_app_server.permission_overrides", lambda _h: ["a=b"])
    monkeypatch.setattr("aibroker.agent.codex_app_server.publish_call", publish)
    monkeypatch.setattr("aibroker.agent.codex_app_server.take_result", take)
    return [e async for e in run_tool_turn(_Adapter(home), run)]


async def test_app_server_thread_start_model_is_a_thread_config_observation(monkeypatch, tmp_path):
    events = await _app_server_events(monkeypatch, tmp_path, {
        "thread": {"id": "thread-1"}, "model": "gpt-6.1-sol", "modelProvider": "openai"})
    assert [e.type for e in events[:2]] == ["session.assigned", "model.observed"]
    assert (events[1].model, events[1].kind, events[1].source) == (
        "gpt-6.1-sol", "thread_config", "codex.app_server.thread_start")


async def test_app_server_without_a_thread_model_observes_nothing(monkeypatch, tmp_path):
    events = await _app_server_events(monkeypatch, tmp_path, {"thread": {"id": "thread-1"}})
    assert "model.observed" not in [e.type for e in events]


# --- aggregation ---------------------------------------------------------------------------


def test_served_model_meta_strongest_evidence_wins_and_all_are_kept():
    obs = []
    for model, kind, src in (("init-m", "session_init", "a"), ("cfg-m", "thread_config", "b"),
                             ("resp-m", "response", "c"), ("init-m", "session_init", "a")):
        obs = add_model_observation(obs, model, kind, src)
    assert len(obs) == 3                                 # deduped
    meta = served_model_meta(obs)
    assert (meta["model_served"], meta["model_served_kind"], meta["model_served_source"]) == (
        "resp-m", "response", "c")
    assert meta["model_observations"] == obs             # disagreement is kept, not overwritten
    only_init = served_model_meta(add_model_observation([], "init-m", "session_init", "a"))
    # strict top-level: weaker evidence is visible only as the labelled best observation
    assert only_init["model_served"] is None and only_init["model_observed_best"] == "init-m"
    assert only_init["model_served_kind"] == "session_init"


def test_served_model_meta_nothing_observed_is_unknown_and_null():
    meta = served_model_meta([])
    assert meta == {"model_served": None, "model_observed_best": None,
                    "model_served_kind": "unknown",
                    "model_served_source": "none", "model_observations": [],
                    "observations_truncated": False, "observations_complete": True}


def test_observations_are_capped():
    obs = []
    for i in range(25):
        obs = add_model_observation(obs, f"m{i}", "response", "s")
    assert len(obs) == 10


async def _job(pid):
    plain = generate_project_key()
    async with get_session() as session:
        session.add(ProjectRow(id=pid, name=f"ev-{pid}", project_key_hash=hash_project_key(plain),
                               project_key_prefix=plain[:12],
                               allowed_scopes=["llm:chat", "llm:agent"], is_active=True, notes=""))
    run_id, _ = await create_run(
        project=SimpleNamespace(id=pid), capability="agent:codex", prompt="p", task_id="t",
        account_id="owner", session_id=None, idempotency_key=None, wall_timeout_s=30,
        idle_timeout_s=30, allowed_tools=[], model="gpt-6.1-sol",
        client_job={"provider": "codex-sub", "model": REQUESTED, "response_format": None})
    return run_id


async def test_poll_reports_evidence_for_a_subscription_job():
    rid = await _job(31)
    await append_event(rid, ev.ModelObserved(model="gpt-6.1-sol", kind="thread_config",
                                             source="codex.app_server.thread_start"))
    await append_event(rid, ev.ModelObserved(model="claude-x-resp", kind="response", source="r"))
    await append_event(rid, ev.RunCompleted(final_text="ok"))
    result = proxy._job_response((await get_run(rid, 31))[1])
    assert result.model_served == "claude-x-resp"
    e = result.evidence
    assert e.model_requested == REQUESTED and e.model_served == "claude-x-resp"
    assert e.model_served_kind == "response" and e.provider == "codex-sub"
    assert e.account_id == "owner" and e.model_rerouted is None   # no app-server path: unknown
    assert [o.kind for o in e.model_observations] == ["thread_config", "response"]


async def test_unobserved_job_is_unknown_and_never_falls_back_to_requested():
    rid = await _job(32)
    await append_event(rid, ev.RunCompleted(final_text="ok"))
    result = proxy._job_response((await get_run(rid, 32))[1])
    assert result.model_served is None
    assert result.evidence.model_served is None and result.evidence.model_served_kind == "unknown"
    assert result.evidence.model_served_source == "none"
    assert result.evidence.model_requested == REQUESTED
    assert result.evidence.model_served != result.evidence.model_requested


def test_plain_job_has_no_evidence():
    assert proxy._job_evidence({"model": "m", "provider": "gemini"}) is None


def test_claude_parser_ignores_synthetic_messages():
    p = ClaudeParser()
    out = p.feed(_line(type="assistant", message={
        "model": "<synthetic>", "content": [{"type": "text", "text": "x"}]}))
    assert [e.type for e in out] == ["message.delta"]
    assert p.feed(_line(type="system", subtype="init", session_id="s", model="<synthetic>")) == [
        ev.SessionAssigned(session_id="s")]


def test_cap_never_drops_the_best_observation():
    obs = []
    for i in range(10):
        obs = add_model_observation(obs, f"init-{i}", "session_init", "s")
    obs = add_model_observation(obs, "resp-last", "response", "r")     # 11th, strongest, last
    assert len(obs) == 10
    meta = served_model_meta(obs)
    assert meta["model_served"] == "resp-last" and meta["model_served_kind"] == "response"
    # a weaker kind never evicts anything once full
    assert add_model_observation(obs, "late-init", "session_init", "s") == obs
    # same (kind, model) from another source is not a second entry
    assert len(add_model_observation([], "m", "response", "a") + []) == 1
    assert len(add_model_observation(add_model_observation([], "m", "response", "a"),
                                     "m", "response", "b")) == 1


def test_codex_parser_logs_only_allowlisted_types_and_safe_keys(caplog):
    p = CodexParser()
    token = "sk-tok_ABCDEF0123456789"
    with caplog.at_level(logging.INFO, logger="aibroker.agent.parsers"):
        p.feed(json.dumps({"type": token, token: 1, "bad key!": 2, "x" * 80: 3, "usage": 4}))
        p.feed(json.dumps({"type": ["not", "a", "string"]}))
    text = chr(10).join(r.getMessage() for r in caplog.records)
    assert token not in text and "bad key" not in text and "x" * 41 not in text
    assert "type=<other>" in text and "usage" in text and "<key>x3" in text


def test_codex_parser_caps_logged_shapes(caplog):
    p = CodexParser()
    with caplog.at_level(logging.INFO, logger="aibroker.agent.parsers"):
        for i in range(1000):        # 50 distinct shapes (unknown-key counts 1..50), cycling
            p.feed(json.dumps({"type": "item.completed", **{f"k{j}": 1 for j in range(i % 50 + 1)}}))
        p.finish(0)
    lines = [r.getMessage() for r in caplog.records]
    assert len(lines) == 21
    assert lines[-1] == "codex exec event shapes capped at 20 (dropped 600)"


async def test_thread_config_only_job_keeps_top_level_model_served_null():
    rid = await _job(33)
    await append_event(rid, ev.ModelObserved(model="gpt-6.1-sol", kind="thread_config",
                                             source="codex.app_server.thread_start"))
    await append_event(rid, ev.RunCompleted(final_text="ok"))
    result = proxy._job_response((await get_run(rid, 33))[1])
    assert result.model_served is None
    assert result.evidence.model_served == "gpt-6.1-sol"
    assert result.evidence.model_served_kind == "thread_config"


async def test_observations_survive_failed_cancelled_and_timed_out_runs():
    for pid, terminal in ((34, ev.RunFailed(error_class="x", message="m")),
                          (35, ev.RunCancelled()), (36, ev.RunTimedOut(limit="wall"))):
        rid = await _job(pid)
        await append_event(rid, ev.ModelObserved(model="m1", kind="response", source="r"))
        await append_event(rid, terminal)
        meta = (await get_run(rid, pid))[1].result_meta
        assert meta["model_observations"] == [{"model": "m1", "kind": "response", "source": "r"}],             terminal.type


async def _subscription_job(pid, *, tools=None, capability="agent:codex", provider="codex-sub"):
    plain = generate_project_key()
    async with get_session() as session:
        session.add(ProjectRow(id=pid, name=f"ev-{pid}", project_key_hash=hash_project_key(plain),
                               project_key_prefix=plain[:12],
                               allowed_scopes=["llm:chat", "llm:agent"], is_active=True, notes=""))
    run_id, _ = await create_run(
        project=SimpleNamespace(id=pid), capability=capability, prompt="p", task_id="t",
        account_id="owner", session_id=None, idempotency_key=None, wall_timeout_s=30,
        idle_timeout_s=30, allowed_tools=[], model="gpt-6.1-sol",
        client_job={"provider": provider, "model": REQUESTED, "response_format": None,
                    "tools": tools or []})
    return run_id


async def test_evidence_is_returned_for_every_terminal_outcome():
    for pid, terminal, status, outcome in (
            (41, ev.RunFailed(error_class="x", message="m"), "error", "failed"),
            (42, ev.RunCancelled(), "cancelled", "cancelled"),
            (43, ev.RunTimedOut(limit="wall"), "error", "timed_out")):
        rid = await _job(pid)
        await append_event(rid, ev.ModelObserved(model="gpt-6.1-sol", kind="thread_config",
                                                 source="s"))
        await append_event(rid, terminal)
        result = proxy._job_response((await get_run(rid, pid))[1])
        assert result.status == status
        e = result.evidence
        assert e.run_status == outcome and e.provider == "codex-sub" and e.account_id == "owner"
        assert e.model_requested == REQUESTED and e.model_served == "gpt-6.1-sol"
        assert e.model_served_kind == "thread_config" and e.model_rerouted is None


async def test_evidence_is_available_while_the_run_is_in_flight():
    rid = await _job(44)
    await append_event(rid, ev.ModelObserved(model="m1", kind="response", source="r"))
    result = proxy._job_response((await get_run(rid, 44))[1])
    assert result.status == "pending" and result.evidence.model_served == "m1"
    assert result.evidence.run_status in ("pending", "running")


async def test_model_rerouted_is_false_only_for_a_completed_app_server_run():
    app = await _subscription_job(45, tools=[{"type": "function"}])
    await append_event(app, ev.RunCompleted(final_text="ok"))
    assert proxy._job_response((await get_run(app, 45))[1]).evidence.model_rerouted is False
    exec_run = await _subscription_job(46)                       # Codex exec: no tools
    await append_event(exec_run, ev.RunCompleted(final_text="ok"))
    assert proxy._job_response((await get_run(exec_run, 46))[1]).evidence.model_rerouted is None
    failed = await _subscription_job(47, tools=[{"type": "function"}])
    await append_event(failed, ev.RunFailed(error_class="x", message="m"))
    assert proxy._job_response((await get_run(failed, 47))[1]).evidence.model_rerouted is None


async def test_eleven_distinct_responses_are_flagged_truncated_and_fail_closed():
    rid = await _job(48)
    for i in range(11):
        await append_event(rid, ev.ModelObserved(model=f"resp-{i}", kind="response", source="r"))
    await append_event(rid, ev.RunCompleted(final_text="ok"))
    result = proxy._job_response((await get_run(rid, 48))[1])
    e = result.evidence
    assert e.observations_truncated is True and e.observations_complete is False
    assert len(e.model_observations) == 10 and e.model_served_kind == "response"
    assert result.model_served is None                      # fail closed: the 11th was dropped
    assert (await get_run(rid, 48))[1].result_meta["observations_truncated"] is True


async def test_untruncated_single_response_still_fills_top_level():
    rid = await _job(49)
    await append_event(rid, ev.ModelObserved(model="only", kind="response", source="r"))
    await append_event(rid, ev.RunCompleted(final_text="ok"))
    result = proxy._job_response((await get_run(rid, 49))[1])
    assert result.model_served == "only" and result.evidence.observations_complete is True


async def test_a_completed_claude_run_with_tools_is_not_an_app_server_run():
    """model_rerouted=false needs the Codex App Server path; tools alone (a Claude job) are not it."""
    rid = await _subscription_job(48, tools=[{"type": "function"}], capability="agent:claude",
                                  provider="claude-sub")
    await append_event(rid, ev.RunCompleted(final_text="ok"))
    assert proxy._job_response((await get_run(rid, 48))[1]).evidence.model_rerouted is None


async def test_terminal_outcomes_keep_files_tool_history_and_usage():
    """The evidence fields are merged into result_meta, never replace it."""
    for pid, terminal in ((49, ev.RunCompleted(final_text="ok", files=[ev.FileRef(path="a.txt", size=1)])),
                          (50, ev.RunFailed(error_class="x", message="m"))):
        rid = await _subscription_job(pid)
        await append_event(rid, ev.Usage(total_tokens=7, reasoning_tokens=3))
        await append_event(rid, ev.ModelObserved(model="m1", kind="response", source="r"))
        await append_event(rid, terminal)
        meta = (await get_run(rid, pid))[1].result_meta
        assert meta["usage_observed"] == {"total_tokens": 7, "reasoning_tokens": 3}, terminal.type
        assert "client_tool_history" in meta and meta["model_observations"], terminal.type
        assert meta["provider"] == "codex-sub", terminal.type
        if terminal.type == "run.completed":
            assert [f["path"] for f in meta["files"]] == ["a.txt"]


def test_codex_parser_never_logs_token_like_key_names(caplog):
    """Identifier-shaped secrets used as key names (no dash, so any name regex would pass them)
    must not reach the log, at the top level or inside `item`: only allowlisted names are echoed."""
    names = ["AIzaSYNTHETICPRIVATE0123456789", "Bearer_SYNTHETICPRIVATE", "ghp_SYNTHETICPRIVATE01"]
    p = CodexParser()
    with caplog.at_level(logging.INFO, logger="aibroker.agent.parsers"):
        p.feed(json.dumps({"type": "thread.started", "thread_id": "t", **dict.fromkeys(names, 1)}))
        p.feed(json.dumps({"type": "item.completed",
                           "item": {"type": "agent_message", "text": "x", **dict.fromkeys(names, 1)}}))
    text = chr(10).join(r.getMessage() for r in caplog.records)
    for n in names:
        assert n not in text and "SYNTHETICPRIVATE" not in text
    assert "<key>x3" in text and "thread_id" in text and "item=" in text
