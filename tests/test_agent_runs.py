"""Agent runs C1: event schema, API validation/scope, dedupe, resume rules, cancel,
adapter contract (fake), queue-lane exclusion. SQLite-runnable."""
from __future__ import annotations

import asyncio
import inspect
import os

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError
from sqlalchemy import insert, select

from aibroker.agent import events as ev
from aibroker.agent.adapter import AgentRunSpec
from aibroker.agent.constants import AGENT_CAPABILITIES, AGENT_SCOPE, OWNER_ONLY_SCOPES
from aibroker.agent.fake import FakeAgentCLI
from aibroker.agent.runs import RunConflict, append_event
from aibroker.auth import generate_project_key, hash_project_key
from aibroker.config import Settings
from aibroker.db import get_session
from aibroker.db.models import AgentRunEventRow, AgentRunRow, DeepJobRow, ProjectRow
from aibroker.main import app
from aibroker.services import job_queue

_PID = [5000]


async def _project(scopes: list[str]) -> tuple[str, int]:
    plain = generate_project_key()
    _PID[0] += 1
    async with get_session() as s:
        await s.execute(insert(ProjectRow).values(
            id=_PID[0], name=f"agent_{_PID[0]}", project_key_hash=hash_project_key(plain),
            project_key_prefix=plain[:12], allowed_scopes=scopes, is_active=True, notes=""))
    return plain, _PID[0]


def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://t")


def _body(**kw):
    base = {"capability": "agent:codex", "prompt": "do it", "task_id": "T-1", "account_id": "acc1"}
    base.update(kw)
    return base


async def _submit(key: str, **kw):
    async with _client() as c:
        return await c.post("/v1/agent/runs", json=_body(**kw), headers={"X-Project-Key": key})


async def _get(key: str, path: str):
    async with _client() as c:
        return await c.get(f"/v1/agent/runs{path}", headers={"X-Project-Key": key})


async def _cancel(key, rid):
    async with _client() as c:
        return await c.post(f"/v1/agent/runs/{rid}/cancel", headers={"X-Project-Key": key})


# --- event schema ------------------------------------------------------------


@pytest.mark.parametrize("data", [
    {"type": "run.started", "capability": "agent:codex", "account_id": "a"},
    {"type": "session.assigned", "session_id": "s1"},
    {"type": "message.delta", "text": "hi"},
    {"type": "tool.call", "call_id": "1", "name": "Bash", "input": {"cmd": "ls"}},
    {"type": "tool.result", "call_id": "1", "ok": False, "output": "x"},
    {"type": "file.changed", "path": "a.txt", "action": "created"},
    {"type": "usage", "input_tokens": 3, "output_tokens": 4, "turns": 1},
    {"type": "run.completed", "final_text": "ok", "files": [{"path": "a.txt", "size": 1}]},
    {"type": "run.failed", "error_class": "x", "message": "m"},
    {"type": "run.cancelled"},
    {"type": "run.timed_out", "limit": "idle"},
])
def test_every_event_type_parses_with_version(data):
    e = ev.parse_event(data)
    assert e.type == data["type"] and e.schema_version == 1


def test_event_schema_rejects_unknown_type_extra_field_and_bad_version():
    with pytest.raises(ValidationError):
        ev.parse_event({"type": "run.exploded"})
    with pytest.raises(ValidationError):
        ev.parse_event({"type": "message.delta", "text": "x", "cost_usd": 1.0})
    with pytest.raises(ValidationError):
        ev.parse_event({"type": "message.delta", "text": "x", "schema_version": 2})


def test_usage_has_no_dollar_field_and_terminal_set_is_exact():
    assert not any("usd" in f or "cost" in f for f in ev.Usage.model_fields)
    assert {"run.completed", "run.failed", "run.cancelled", "run.timed_out"} == ev.TERMINAL_TYPES


# --- scope / signup ----------------------------------------------------------


def test_agent_scope_is_owner_only_and_never_a_signup_default():
    assert AGENT_CAPABILITIES == ("agent:codex", "agent:claude")
    assert AGENT_SCOPE in OWNER_ONLY_SCOPES
    s = Settings(SIGNUP_DEFAULT_SCOPES="llm:chat,llm:agent,llm:embed")
    assert "llm:agent" not in s.signup_scopes and "llm:chat" in s.signup_scopes
    assert "llm:agent" not in Settings().signup_scopes


async def test_without_agent_scope_every_endpoint_is_403():
    key, _ = await _project(["llm:chat"])
    assert (await _submit(key)).status_code == 403
    assert (await _get(key, "/1")).status_code == 403
    assert (await _get(key, "/1/events")).status_code == 403
    assert (await _cancel(key, 1)).status_code == 403


async def test_no_key_is_401():
    async with _client() as c:
        assert (await c.post("/v1/agent/runs", json=_body())).status_code == 401


# --- API validation ----------------------------------------------------------


@pytest.mark.parametrize("patch", [
    {"capability": "chat:fast"}, {"prompt": ""}, {"task_id": ""}, {"account_id": ""},
    {"account_id": "bad id!"}, {"task_id": "../x"}, {"session_id": "a b"},
    {"wall_timeout_s": 1}, {"wall_timeout_s": 99999}, {"idle_timeout_s": 1},
    {"allowed_tools": ["x" * 200]}, {"allowed_tools": ["bad\ntool"]},
])
async def test_submit_validation_rejects(patch):
    key, _ = await _project([AGENT_SCOPE])
    assert (await _submit(key, **patch)).status_code == 422


async def test_account_id_is_required():
    key, _ = await _project([AGENT_SCOPE])
    body = _body()
    del body["account_id"]
    async with _client() as c:
        r = await c.post("/v1/agent/runs", json=body, headers={"X-Project-Key": key})
    assert r.status_code == 422


async def test_submit_creates_pending_run_and_poll_shows_it():
    key, pid = await _project([AGENT_SCOPE])
    r = await _submit(key, allowed_tools=["Read", "Grep(src/**)"])
    assert r.status_code == 202
    run_id = r.json()["run_id"]
    assert r.headers["x-request-id"] == f"job-{run_id}"
    g = (await _get(key, f"/{run_id}")).json()
    assert g["status"] == "pending" and g["task_id"] == "T-1" and g["account_id"] == "acc1"
    async with get_session() as s:
        job = await s.get(DeepJobRow, run_id)
    assert job.capability == "agent:codex" and job.project_id == pid
    assert job.request["allowed_tools"] == ["Read", "Grep(src/**)"]


async def test_other_project_cannot_see_run():
    k1, _ = await _project([AGENT_SCOPE])
    k2, _ = await _project([AGENT_SCOPE])
    run_id = (await _submit(k1)).json()["run_id"]
    assert (await _get(k2, f"/{run_id}")).status_code == 404
    assert (await _get(k2, f"/{run_id}/events")).status_code == 404


# --- dedupe ------------------------------------------------------------------


async def test_same_idempotency_key_returns_same_run_and_conflicts_on_other_payload():
    key, _ = await _project([AGENT_SCOPE])
    a = (await _submit(key, idempotency_key="k1")).json()
    b = (await _submit(key, idempotency_key="k1")).json()
    assert a["run_id"] == b["run_id"] and not a["deduplicated"] and b["deduplicated"]
    assert (await _submit(key, idempotency_key="k1", prompt="other")).status_code == 409


async def test_identical_payload_in_flight_dedupes_but_finished_does_not():
    key, _ = await _project([AGENT_SCOPE])
    a = (await _submit(key)).json()["run_id"]
    assert (await _submit(key)).json()["run_id"] == a
    await append_event(a, ev.RunFailed(error_class="x"))
    assert (await _submit(key)).json()["run_id"] != a


async def test_different_task_is_not_deduped():
    key, _ = await _project([AGENT_SCOPE])
    a = (await _submit(key)).json()["run_id"]
    assert (await _submit(key, task_id="T-2")).json()["run_id"] != a


# --- events, ordering, terminal handling ------------------------------------


async def test_events_are_ordered_polled_by_after_seq_and_session_is_recorded():
    key, _ = await _project([AGENT_SCOPE])
    rid = (await _submit(key)).json()["run_id"]
    await append_event(rid, ev.RunStarted(capability="agent:codex", account_id="acc1"))
    await append_event(rid, ev.SessionAssigned(session_id="sess-1"))
    await append_event(rid, ev.SessionAssigned(session_id="sess-2"))  # first one wins
    await append_event(rid, ev.MessageDelta(text="hello"))
    page = (await _get(key, f"/{rid}/events")).json()
    assert [e["seq"] for e in page["events"]] == [1, 2, 3, 4]
    assert not page["finished"] and page["next_after_seq"] == 4
    assert (await _get(key, f"/{rid}")).json()["session_id"] == "sess-1"
    tail = (await _get(key, f"/{rid}/events?after_seq=3")).json()
    assert [e["type"] for e in tail["events"]] == ["message.delta"]
    empty = (await _get(key, f"/{rid}/events?after_seq=4")).json()
    assert empty["events"] == [] and empty["next_after_seq"] == 4


async def test_completed_run_exposes_result_and_rejects_further_events():
    key, _ = await _project([AGENT_SCOPE])
    rid = (await _submit(key)).json()["run_id"]
    await append_event(rid, ev.RunCompleted(final_text="done!", files=[ev.FileRef(path="a.txt", size=2)]))
    g = (await _get(key, f"/{rid}")).json()
    assert g["status"] == "completed" and g["final_text"] == "done!"
    assert g["files"][0]["path"] == "a.txt"
    assert (await _get(key, f"/{rid}/events")).json()["finished"] is True
    with pytest.raises(RunConflict):
        await append_event(rid, ev.MessageDelta(text="late"))


async def test_failed_and_timed_out_map_to_status():
    key, _ = await _project([AGENT_SCOPE])
    a = (await _submit(key)).json()["run_id"]
    b = (await _submit(key, task_id="T-2")).json()["run_id"]
    await append_event(a, ev.RunFailed(error_class="billing_mode_refused", message="m"))
    await append_event(b, ev.RunTimedOut(limit="idle"))
    ga, gb = (await _get(key, f"/{a}")).json(), (await _get(key, f"/{b}")).json()
    assert ga["status"] == "failed" and ga["error"].startswith("billing_mode_refused")
    assert gb["status"] == "timed_out"


async def test_event_text_is_clipped():
    key, _ = await _project([AGENT_SCOPE])
    rid = (await _submit(key)).json()["run_id"]
    await append_event(rid, ev.MessageDelta(text="x" * (ev.MAX_TEXT_CHARS + 500)))
    async with get_session() as s:
        row = (await s.execute(select(AgentRunEventRow))).scalars().one()
    assert len(row.payload["text"]) == ev.MAX_TEXT_CHARS


# --- resume rules ------------------------------------------------------------


async def _finished_with_session(key, account="acc1", sid="sess-A", **kw):
    rid = (await _submit(key, account_id=account, **kw)).json()["run_id"]
    await append_event(rid, ev.SessionAssigned(session_id=sid))
    await append_event(rid, ev.RunCompleted(final_text="ok"))
    return rid


async def test_resume_exact_session_same_account_is_accepted():
    key, _ = await _project([AGENT_SCOPE])
    await _finished_with_session(key)
    r = await _submit(key, session_id="sess-A", prompt="continue the work")
    assert r.status_code == 202
    async with get_session() as s:
        run = await s.get(AgentRunRow, r.json()["run_id"])
    assert run.resume_session_id == "sess-A"


async def test_resume_rejects_unknown_foreign_account_and_keywords():
    key, _ = await _project([AGENT_SCOPE])
    await _finished_with_session(key)
    assert (await _submit(key, session_id="never-seen", prompt="p2")).status_code == 400
    assert (await _submit(key, session_id="sess-A", account_id="acc2", prompt="p3")).status_code == 400
    for word in ("last", "continue", "latest"):
        assert (await _submit(key, session_id=word, prompt="p4")).status_code == 400


async def test_resume_rejects_session_of_another_project():
    k1, _ = await _project([AGENT_SCOPE])
    k2, _ = await _project([AGENT_SCOPE])
    await _finished_with_session(k1)
    assert (await _submit(k2, session_id="sess-A", prompt="steal")).status_code == 400


async def test_resume_rejects_session_still_in_use():
    key, _ = await _project([AGENT_SCOPE])
    rid = (await _submit(key)).json()["run_id"]
    await append_event(rid, ev.SessionAssigned(session_id="sess-live"))   # not finished
    assert (await _submit(key, session_id="sess-live", prompt="again")).status_code == 409


async def test_resume_rejects_session_of_other_capability():
    key, _ = await _project([AGENT_SCOPE])
    await _finished_with_session(key)
    r = await _submit(key, capability="agent:claude", session_id="sess-A", prompt="x")
    assert r.status_code == 400


# --- cancel ------------------------------------------------------------------


async def test_cancel_pending_run_cancels_immediately():
    key, _ = await _project([AGENT_SCOPE])
    rid = (await _submit(key)).json()["run_id"]
    r = (await _cancel(key, rid)).json()
    assert r["status"] == "cancelled"
    types = [e["type"] for e in (await _get(key, f"/{rid}/events")).json()["events"]]
    assert types == ["run.cancelled"]
    assert (await _cancel(key, rid)).json()["status"] == "cancelled"   # idempotent


async def test_cancel_running_run_sets_flag_for_worker():
    key, _ = await _project([AGENT_SCOPE])
    rid = (await _submit(key)).json()["run_id"]
    async with get_session() as s:
        (await s.get(DeepJobRow, rid)).status = "running"
    r = (await _cancel(key, rid)).json()
    assert r["status"] == "running" and r["cancel_requested"] is True


async def test_cancel_finished_run_is_unchanged_and_unknown_is_404():
    key, _ = await _project([AGENT_SCOPE])
    rid = await _finished_with_session(key)
    assert (await _cancel(key, rid)).json()["status"] == "completed"
    assert (await _cancel(key, 999999)).status_code == 404


# --- adapter contract (fake) and queue-lane exclusion -----------------------


async def _drain(fake, spec):
    return [e async for e in fake.start(spec)]


async def test_fake_adapter_scripts_events_files_and_failure(tmp_path):
    spec = AgentRunSpec(run_id=1, capability="agent:codex", prompt="p",
                        workspace=str(tmp_path), account_id="a")
    fake = FakeAgentCLI([ev.SessionAssigned(session_id="s"), ("write", "out/a.txt", "hi"),
                         ev.RunCompleted(final_text="ok")])
    got = [e.type for e in await _drain(fake, spec)]
    assert got == ["session.assigned", "run.completed"]
    assert (tmp_path / "out" / "a.txt").read_text() == "hi" and fake.supports_resume
    with pytest.raises(RuntimeError):
        await _drain(FakeAgentCLI([("raise", "boom")]), spec)


async def test_fake_adapter_hang_ends_on_cancel_and_reports_mode(tmp_path):
    spec = AgentRunSpec(run_id=1, capability="agent:claude", prompt="p",
                        workspace=str(tmp_path), account_id="a")
    fake = FakeAgentCLI([("hang",)], mode="unknown")
    task = asyncio.create_task(_drain(fake, spec))
    await asyncio.sleep(0.05)
    assert not task.done()
    await fake.cancel()
    assert await asyncio.wait_for(task, 1) == []
    assert fake.cancel_calls == 1 and await fake.billing_mode() == "unknown"


@pytest.mark.skipif("sqlite" in os.environ.get("DATABASE_URL", ""),
                    reason="claim uses SKIP LOCKED (Postgres)")
async def test_llm_dispatcher_never_claims_agent_runs():
    key, _ = await _project([AGENT_SCOPE])
    rid = (await _submit(key)).json()["run_id"]
    assert await job_queue._claim_batch(10) == []
    async with get_session() as s:
        assert (await s.get(DeepJobRow, rid)).status == "pending"


def test_llm_claim_and_stale_sql_exclude_agent_lane():
    assert "NOT LIKE 'agent:%'" in inspect.getsource(job_queue._claim_batch)
    assert "NOT LIKE 'agent:%'" in inspect.getsource(job_queue._requeue_stale_running)
