"""Job trace/labels, Idempotency-Key replay and cancel (migration 020; docs/api.md).

HTTP-level through the real app on SQLite: plain jobs are inserted by submit_job_ex (ORM, Integer PK
variant), agent jobs by create_run. The dispatcher claim / stale reclaim / retention SQL is Postgres-only
and covered by the skipif-SQLite tests at the bottom.
"""
from __future__ import annotations

import os
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from aibroker.agent.constants import AGENT_SCOPE
from aibroker.agent.events import RunCompleted
from aibroker.agent.runs import RunConflict, create_run, get_run
from aibroker.db import get_session
from aibroker.db.models import AgentRunRow, DeepJobRow, ProjectRow
from aibroker.main import app
from aibroker.routes import proxy
from aibroker.services import deep_jobs, job_queue
from aibroker.services.deep_jobs import IdempotencyConflict, payload_hash, submit_job_ex
from aibroker.services.job_control import cancel_job, cancel_matching
from aibroker.services.job_meta import log_tag, resolve_trace_id
from aibroker.services.llm_service import CANCELLED, run_chat
from tests.test_routes_proxy import _make_project

ON_SQLITE = "sqlite" in os.environ.get("DATABASE_URL", "")

_BODY = {"messages": [{"role": "user", "content": "hi"}], "max_tokens": 64}


def _h(key: str, **extra: str) -> dict[str, str]:
    return {"X-Project-Key": key, **extra}


async def _submit(client: TestClient, key: str, body: dict | None = None, **headers: str):
    return client.post("/v1/jobs?capability=chat:fast", headers=_h(key, **headers),
                       json=body or _BODY)


async def _set(job_id: int, **fields) -> None:
    async with get_session() as s:
        row = await s.get(DeepJobRow, job_id)
        for k, v in fields.items():
            setattr(row, k, v)


async def _row(job_id: int) -> DeepJobRow:
    async with get_session() as s:
        return await s.get(DeepJobRow, job_id)


async def _count(project_id: int) -> int:
    async with get_session() as s:
        return len((await s.execute(select(DeepJobRow.id).where(
            DeepJobRow.project_id == project_id))).all())


# ─── trace_id / labels ──────────────────────────────────────────────────────


async def test_trace_and_labels_roundtrip_through_submit_and_both_polls():
    key, pid = await _make_project(["llm:chat"])
    client = TestClient(app)
    labels = {"exam_run_id": "r-1", "case_id": "c.7", "sample": "2", "attempt": "1"}
    r = await _submit(client, key, {**_BODY, "trace_id": "exam:r-1-ok", "labels": labels})
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["trace_id"] == "exam:r-1-ok" and body["labels"] == labels
    job_id = body["job_id"]
    for path in (f"/v1/jobs/{job_id}", f"/v1/deep/{job_id}"):
        polled = client.get(path, headers=_h(key)).json()
        assert polled["status"] == "pending"
        assert polled["trace_id"] == "exam:r-1-ok" and polled["labels"] == labels
    row = await _row(job_id)
    assert row.trace_id == "exam:r-1-ok" and row.labels == labels
    assert "trace_id" not in row.request and "labels" not in row.request


async def test_jobs_without_trace_or_labels_return_nulls():
    key, _ = await _make_project(["llm:chat"])
    body = (await _submit(TestClient(app), key)).json()
    assert body["trace_id"] is None and body["labels"] is None


async def test_x_trace_id_header_is_used_and_body_wins():
    key, _ = await _make_project(["llm:chat"])
    client = TestClient(app)
    only_header = (await _submit(client, key, {**_BODY, "messages": [{"role": "user", "content": "a"}]},
                                 **{"X-Trace-Id": "hdr-1"})).json()
    both = (await _submit(client, key, {**_BODY, "messages": [{"role": "user", "content": "b"}],
                                        "trace_id": "body-1"}, **{"X-Trace-Id": "hdr-2"})).json()
    assert only_header["trace_id"] == "hdr-1"
    assert both["trace_id"] == "body-1"
    assert resolve_trace_id("body", "hdr") == "body" and resolve_trace_id(None, None) is None


@pytest.mark.parametrize("patch_body,headers", [
    ({"trace_id": "x" * 129}, {}),
    ({"trace_id": "has space"}, {}),
    ({"trace_id": ""}, {}),
    ({"labels": {f"k{i}": "v" for i in range(9)}}, {}),
    ({"labels": {"bad key": "v"}}, {}),
    ({"labels": {"k": "bad value!"}}, {}),
    ({"labels": {"k": "x" * 129}}, {}),
    ({"labels": {"k": 7}}, {}),
    ({"labels": {"k": ""}}, {}),
    ({}, {"X-Trace-Id": "no good"}),
    ({}, {"Idempotency-Key": "no good"}),
    ({}, {"Idempotency-Key": "k" * 129}),
])
async def test_bad_trace_labels_or_key_are_422(patch_body, headers):
    key, pid = await _make_project(["llm:chat"])
    r = await _submit(TestClient(app), key, {**_BODY, **patch_body}, **headers)
    assert r.status_code == 422, r.text
    assert await _count(pid) == 0


async def test_eight_labels_are_accepted():
    key, _ = await _make_project(["llm:chat"])
    labels = {f"k{i}": "v" for i in range(8)}
    r = await _submit(TestClient(app), key, {**_BODY, "labels": labels})
    assert r.status_code == 202 and r.json()["labels"] == labels


def test_trace_id_and_labels_never_enter_the_payload_hash():
    base = proxy.ChatRequest(messages=[proxy.ChatMessage(role="user", content="hi")], max_tokens=64)
    tagged = proxy.ChatRequest(messages=[proxy.ChatMessage(role="user", content="hi")], max_tokens=64,
                               trace_id="t-1", labels={"a": "b"})
    assert proxy._client_request(base) == proxy._client_request(tagged)
    assert payload_hash(1, "chat:fast", proxy._client_request(base)) \
        == payload_hash(1, "chat:fast", proxy._client_request(tagged))
    assert set(deep_jobs.build_request(messages=[], model=None, max_tokens=1, temperature=0.0,
                                       response_format=None, workflow=None)) == {
        "messages", "model", "max_tokens", "temperature", "response_format", "workflow"}


def test_log_tag_is_empty_without_trace_or_labels():
    assert log_tag(None, None) == ""
    assert log_tag("t", {"a": "b"}) == " trace_id=t labels={'a': 'b'}"


# ─── Idempotency-Key ────────────────────────────────────────────────────────


async def test_same_key_and_payload_replays_the_same_job_with_200_and_header():
    key, pid = await _make_project(["llm:chat"])
    client = TestClient(app)
    first = await _submit(client, key, **{"Idempotency-Key": "run-1:case-1"})
    assert first.status_code == 202 and "idempotent-replay" not in first.headers
    again = await _submit(client, key, **{"Idempotency-Key": "run-1:case-1"})
    assert again.status_code == 200, again.text
    assert again.headers["Idempotent-Replay"] == "true"
    body = again.json()
    assert body["job_id"] == first.json()["job_id"] and body["status"] == "pending"
    assert body["poll_url"] == f"/v1/jobs/{body['job_id']}"
    assert await _count(pid) == 1


async def test_replay_returns_the_current_result_once_the_job_is_done():
    key, _ = await _make_project(["llm:chat"])
    client = TestClient(app)
    job_id = (await _submit(client, key, **{"Idempotency-Key": "k-done"})).json()["job_id"]
    await _set(job_id, status="done", result_text="the answer",
               result_meta={"provider": "groq", "model": "m", "tokens_in": 3, "tokens_out": 4})
    again = await _submit(client, key, **{"Idempotency-Key": "k-done"})
    assert again.status_code == 200
    assert again.json()["status"] == "done" and again.json()["text"] == "the answer"


async def test_same_key_with_a_different_payload_is_409_with_existing_job_id():
    key, pid = await _make_project(["llm:chat"])
    client = TestClient(app)
    job_id = (await _submit(client, key, **{"Idempotency-Key": "k-409"})).json()["job_id"]
    other = {**_BODY, "messages": [{"role": "user", "content": "something else"}]}
    r = await _submit(client, key, other, **{"Idempotency-Key": "k-409"})
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "idempotency_key_conflict"
    assert r.json()["detail"]["existing_job_id"] == job_id
    assert await _count(pid) == 1


async def test_explicit_defaults_and_null_message_fields_hash_like_omitted_ones():
    key, pid = await _make_project(["llm:chat"])
    client = TestClient(app)
    first = await _submit(client, key, {"messages": [{"role": "user", "content": "hi"}]},
                          **{"Idempotency-Key": "k-norm"})
    explicit = {"messages": [{"role": "user", "content": "hi", "tool_calls": None,
                              "tool_call_id": None}],
                "max_tokens": proxy.ChatRequest.model_fields["max_tokens"].default,
                "temperature": proxy.ChatRequest.model_fields["temperature"].default,
                "model": None, "response_format": None, "workflow": None}
    again = await _submit(client, key, explicit, **{"Idempotency-Key": "k-norm"})
    assert first.status_code == 202 and again.status_code == 200
    assert again.json()["job_id"] == first.json()["job_id"]
    assert await _count(pid) == 1


async def test_replay_comes_before_pin_validation_and_availability_checks():
    key, _ = await _make_project(["llm:chat"])
    client = TestClient(app)
    first = await _submit(client, key, **{"Idempotency-Key": "k-cfg"})
    with patch.object(proxy, "_validate_pin", side_effect=AssertionError("must not run")):
        again = await _submit(client, key, **{"Idempotency-Key": "k-cfg"})
    assert again.status_code == 200 and again.json()["job_id"] == first.json()["job_id"]


async def test_a_changed_model_or_max_tokens_is_a_different_payload():
    key, _ = await _make_project(["llm:chat"])
    client = TestClient(app)
    await _submit(client, key, **{"Idempotency-Key": "k-mt"})
    r = await _submit(client, key, {**_BODY, "max_tokens": 65}, **{"Idempotency-Key": "k-mt"})
    assert r.status_code == 409


async def test_trace_and_labels_do_not_make_a_replay_conflict():
    key, pid = await _make_project(["llm:chat"])
    client = TestClient(app)
    first = await _submit(client, key, {**_BODY, "trace_id": "t-a"}, **{"Idempotency-Key": "k-tr"})
    again = await _submit(client, key, {**_BODY, "trace_id": "t-b", "labels": {"x": "y"}},
                          **{"Idempotency-Key": "k-tr"})
    assert again.status_code == 200 and again.json()["job_id"] == first.json()["job_id"]
    assert again.json()["trace_id"] == "t-a"          # the stored job is the answer
    assert await _count(pid) == 1


async def test_different_keys_with_an_identical_payload_are_independent_jobs():
    key, pid = await _make_project(["llm:chat"])
    client = TestClient(app)
    a = await _submit(client, key, **{"Idempotency-Key": "ka"})
    b = await _submit(client, key, **{"Idempotency-Key": "kb"})
    assert a.status_code == b.status_code == 202
    assert a.json()["job_id"] != b.json()["job_id"]
    assert await _count(pid) == 2


async def test_a_keyed_request_ignores_a_matching_unkeyed_inflight_job_and_vice_versa():
    key, pid = await _make_project(["llm:chat"])
    client = TestClient(app)
    unkeyed = (await _submit(client, key)).json()["job_id"]
    keyed = await _submit(client, key, **{"Idempotency-Key": "k-own"})
    assert keyed.status_code == 202 and keyed.json()["job_id"] != unkeyed
    assert await _count(pid) == 2


async def test_without_a_key_the_legacy_inflight_dedup_still_collapses_resubmits():
    key, pid = await _make_project(["llm:chat"])
    client = TestClient(app)
    a = await _submit(client, key)
    b = await _submit(client, key)
    assert a.status_code == b.status_code == 202
    assert a.json()["job_id"] == b.json()["job_id"]
    assert await _count(pid) == 1


async def test_the_same_key_in_two_projects_makes_two_jobs():
    key1, _ = await _make_project(["llm:chat"])
    key2, _ = await _make_project(["llm:chat"])
    client = TestClient(app)
    a = await _submit(client, key1, **{"Idempotency-Key": "shared"})
    b = await _submit(client, key2, **{"Idempotency-Key": "shared"})
    assert a.status_code == b.status_code == 202 and a.json()["job_id"] != b.json()["job_id"]


async def test_losing_the_insert_race_returns_the_winner_or_conflicts():
    _, pid = await _make_project(["llm:chat"])
    project = SimpleNamespace(id=pid)
    kwargs = {"project": project, "capability": "chat:fast",
              "messages": [{"role": "user", "content": "hi"}], "model": None, "max_tokens": 64,
              "temperature": 0.7, "response_format": None, "workflow": None,
              "idempotency_key": "race"}
    winner = await submit_job_ex(**kwargs)
    assert winner.replayed is False
    real = deep_jobs.replayed_job_id
    calls = {"n": 0}

    async def blind_first(*a, **kw):
        calls["n"] += 1
        return None if calls["n"] == 1 else await real(*a, **kw)

    with patch.object(deep_jobs, "replayed_job_id", blind_first):
        lost = await submit_job_ex(**kwargs)           # pre-check blind -> INSERT -> IntegrityError
    assert lost == (winner.job_id, True) and calls["n"] == 2
    calls["n"] = 0
    with patch.object(deep_jobs, "replayed_job_id", blind_first), pytest.raises(IdempotencyConflict) as exc:
        await submit_job_ex(**{**kwargs, "messages": [{"role": "user", "content": "other"}]})
    assert exc.value.existing_job_id == winner.job_id
    assert await _count(pid) == 1


async def test_subscription_submit_passes_key_hash_trace_and_labels_to_create_run(monkeypatch):
    from aibroker.config import get_settings
    monkeypatch.setattr(get_settings(), "AGENT_SUBSCRIPTION_MODELS", "default")
    monkeypatch.setattr(get_settings(), "AGENT_SUBSCRIPTION_VERIFIED_MODELS",
                        '{"claude-sub":["claude-sonnet-5-5"]}')
    monkeypatch.setattr(proxy, "project_allowed", lambda _id: True)
    monkeypatch.setattr(proxy, "policy_violation", lambda *_: None)
    monkeypatch.setattr(proxy, "wake_worker", AsyncMock())
    create = AsyncMock(return_value=(41, True))
    monkeypatch.setattr(proxy, "create_run", create)
    key, pid = await _make_project(["llm:chat", AGENT_SCOPE])
    client = TestClient(app)
    body = {"model": "claude-sub/claude-sonnet-5-5", "messages": [{"role": "user", "content": "hi"}],
            "trace_id": "t-sub", "labels": {"case_id": "c1"}}
    r = client.post("/v1/jobs?capability=chat:fast", headers=_h(key, **{"Idempotency-Key": "sub-1"}),
                    json=body)
    assert r.status_code == 202, r.text
    assert r.json()["trace_id"] == "t-sub" and r.json()["labels"] == {"case_id": "c1"}
    kw = create.await_args.kwargs
    assert kw["job_idempotency_key"] == "sub-1" and kw["trace_id"] == "t-sub"
    assert kw["labels"] == {"case_id": "c1"}
    assert kw["job_payload_hash"] == payload_hash(pid, "chat:fast", proxy._client_request(
        proxy.ChatRequest(**body)))
    # create_run says "existing run" -> a replay: 200 + header, no wake-up
    await _insert_agent_job(pid, "agent:claude", key="sub-1")
    create.return_value = (await _only_job_id(pid), False)
    again = client.post("/v1/jobs?capability=chat:fast", headers=_h(key, **{"Idempotency-Key": "sub-2"}),
                        json=body)
    assert again.status_code == 200 and again.headers["Idempotent-Replay"] == "true"
    proxy.wake_worker.assert_awaited_once()


async def test_subscription_early_replay_skips_create_run_and_conflicts_on_other_payload(monkeypatch):
    from aibroker.config import get_settings
    monkeypatch.setattr(get_settings(), "AGENT_SUBSCRIPTION_MODELS", "default")
    monkeypatch.setattr(get_settings(), "AGENT_SUBSCRIPTION_VERIFIED_MODELS",
                        '{"claude-sub":["claude-sonnet-5-5"]}')
    monkeypatch.setattr(proxy, "project_allowed", lambda _id: True)
    monkeypatch.setattr(proxy, "policy_violation", lambda *_: None)
    create = AsyncMock(side_effect=AssertionError("must not create"))
    monkeypatch.setattr(proxy, "create_run", create)
    admit = AsyncMock()
    monkeypatch.setattr(proxy, "admit_request", admit)
    key, pid = await _make_project(["llm:chat", AGENT_SCOPE])
    body = {"model": "claude-sub/claude-sonnet-5-5", "messages": [{"role": "user", "content": "hi"}]}
    phash = payload_hash(pid, "chat:fast", proxy._client_request(proxy.ChatRequest(**body)))
    job_id = await _insert_agent_job(pid, "agent:claude", key="early", phash=phash)
    client = TestClient(app)
    r = client.post("/v1/jobs?capability=chat:fast", headers=_h(key, **{"Idempotency-Key": "early"}),
                    json=body)
    assert r.status_code == 200 and r.json()["job_id"] == job_id
    admit.assert_not_awaited()                         # a replay spends no request-cap unit
    monkeypatch.setattr(get_settings(), "AGENT_SUBSCRIPTION_VERIFIED_MODELS", "{}")   # config drifted
    again = client.post("/v1/jobs?capability=chat:fast", headers=_h(key, **{"Idempotency-Key": "early"}),
                        json=body)
    assert again.status_code == 200 and again.json()["job_id"] == job_id
    other = {**body, "messages": [{"role": "user", "content": "different"}]}
    r = client.post("/v1/jobs?capability=chat:fast", headers=_h(key, **{"Idempotency-Key": "early"}),
                    json=other)
    assert r.status_code == 409 and r.json()["detail"]["existing_job_id"] == job_id


async def test_create_run_job_key_replays_conflicts_and_skips_legacy_dedup():
    _, pid = await _make_project(["llm:chat", AGENT_SCOPE])
    project = SimpleNamespace(id=pid)
    common = {"project": project, "capability": "agent:claude", "prompt": "p", "task_id": "jobs",
              "account_id": "owner", "session_id": None, "idempotency_key": None,
              "wall_timeout_s": 30, "idle_timeout_s": 30, "allowed_tools": []}
    a, created = await create_run(**common, job_idempotency_key="rk", job_payload_hash="h1",
                                  trace_id="t", labels={"a": "b"})
    assert created is True
    row = await _row(a)
    assert (row.idempotency_key, row.trace_id, row.labels, row.payload_hash) == ("rk", "t", {"a": "b"}, "h1")
    assert await create_run(**common, job_idempotency_key="rk", job_payload_hash="h1") == (a, False)
    with pytest.raises(IdempotencyConflict):
        await create_run(**common, job_idempotency_key="rk", job_payload_hash="h2")
    b, created = await create_run(**common, job_idempotency_key="rk2", job_payload_hash="h1")
    assert created is True and b != a                  # same payload, other key: legacy dedup is off
    real = deep_jobs.replayed_job_id
    calls = {"n": 0}

    async def blind_first(*args, **kw):
        calls["n"] += 1
        return None if calls["n"] == 1 else await real(*args, **kw)

    from aibroker.agent import runs
    with patch.object(runs, "replayed_job_id", blind_first):
        assert await create_run(**common, job_idempotency_key="rk", job_payload_hash="h1") == (a, False)


# ─── poll shape ─────────────────────────────────────────────────────────────


def test_poll_response_maps_cancelled_and_cancel_requested():
    cancelled = proxy._job_response(SimpleNamespace(
        id=5, status="cancelled", request={}, error_message=None, result_meta=None,
        trace_id="t", labels={"a": "b"}))
    assert cancelled.status == "cancelled" and cancelled.trace_id == "t" and cancelled.error is None
    pending = proxy._job_response(SimpleNamespace(
        id=5, status="running", request={}, result_meta=None, trace_id=None, labels=None,
        cancel_requested=True, created_at=datetime.now(UTC).replace(tzinfo=None)))
    assert pending.status == "pending" and pending.cancel_requested is True
    plain = proxy._job_response(SimpleNamespace(
        id=5, status="pending", request={}, result_meta=None,
        created_at=datetime.now(UTC).replace(tzinfo=None)))
    assert plain.cancel_requested is None and plain.trace_id is None


# ─── cancel ─────────────────────────────────────────────────────────────────


async def _plain_job(pid: int, status: str = "pending", **kw) -> int:
    async with get_session() as s:
        row = DeepJobRow(project_id=pid, capability="chat:fast", status=status,
                         request={"messages": [{"role": "user", "content": "hi"}], "model": None,
                                  "max_tokens": 64, "temperature": 0.7, "response_format": None,
                                  "workflow": None}, **kw)
        s.add(row)
        await s.flush()
        return row.id


async def _insert_agent_job(pid: int, cap: str = "agent:claude", *, key: str | None = None,
                            phash: str | None = None) -> int:
    rid, _ = await create_run(
        project=SimpleNamespace(id=pid), capability=cap, prompt="p", task_id="jobs",
        account_id="owner", session_id=None, idempotency_key=None, wall_timeout_s=30,
        idle_timeout_s=30, allowed_tools=[], job_idempotency_key=key, job_payload_hash=phash)
    return rid


async def _only_job_id(pid: int) -> int:
    async with get_session() as s:
        return (await s.execute(select(DeepJobRow.id).where(
            DeepJobRow.project_id == pid).order_by(DeepJobRow.id.desc()))).scalars().first()


async def test_cancel_pending_job_is_terminal_at_once_and_idempotent():
    key, pid = await _make_project(["llm:chat"])
    client = TestClient(app)
    job_id = (await _submit(client, key)).json()["job_id"]
    r = client.post(f"/v1/jobs/{job_id}/cancel", headers=_h(key))
    assert r.status_code == 200 and r.json() == {"job_id": job_id, "state": "cancelled",
                                                 "status": "cancelled"}
    polled = client.get(f"/v1/jobs/{job_id}", headers=_h(key)).json()
    assert polled["status"] == "cancelled" and polled["poll_after_s"] is None
    row = await _row(job_id)
    assert row.status == "cancelled" and row.completed_at is not None
    again = client.post(f"/v1/jobs/{job_id}/cancel", headers=_h(key))
    assert again.json()["state"] == "already_terminal" and again.json()["status"] == "cancelled"


async def test_cancelling_a_pending_transcription_drops_the_audio_blob():
    key, pid = await _make_project(["llm:chat"])
    async with get_session() as s:
        row = DeepJobRow(project_id=pid, capability="transcription", status="pending",
                         request={"audio_b64": "AAAA", "filename": "a.ogg"})
        s.add(row)
        await s.flush()
        job_id = row.id
    r = TestClient(app).post(f"/v1/jobs/{job_id}/cancel", headers=_h(key))
    assert r.json()["state"] == "cancelled"
    assert (await _row(job_id)).request == {"audio_b64": None, "filename": "a.ogg"}


async def test_cancel_running_plain_job_flags_it_and_the_next_attempt_boundary_ends_it():
    key, pid = await _make_project(["llm:chat"])
    client = TestClient(app)
    job_id = await _plain_job(pid, "running")
    r = client.post(f"/v1/jobs/{job_id}/cancel", headers=_h(key))
    assert r.json() == {"job_id": job_id, "state": "cancel_requested", "status": "running"}
    polled = client.get(f"/v1/jobs/{job_id}", headers=_h(key)).json()
    assert polled["status"] == "pending" and polled["cancel_requested"] is True
    assert client.post(f"/v1/jobs/{job_id}/cancel", headers=_h(key)).json()["state"] == "cancel_requested"
    # The dispatcher side: the claimed in-memory row predates the flag; run_chat polls should_stop.
    claimed = DeepJobRow(id=job_id, project_id=pid, capability="chat:fast", status="running",
                         retry_count=0, cancel_requested=False,
                         request={"messages": [{"role": "user", "content": "hi"}], "model": None,
                                  "max_tokens": 64, "temperature": 0.7, "response_format": None,
                                  "workflow": None})
    seen: dict = {}

    async def fake_run_chat(**kw):
        seen["stop"] = await kw["should_stop"]()
        return CANCELLED

    with patch.object(job_queue, "run_chat", fake_run_chat):
        await job_queue._execute(claimed)
    assert seen["stop"] is True
    final = await _row(job_id)
    assert final.status == "cancelled" and final.completed_at is not None
    assert client.get(f"/v1/jobs/{job_id}", headers=_h(key)).json()["status"] == "cancelled"


async def test_run_chat_stops_before_the_first_attempt_when_should_stop_says_so():
    _, pid = await _make_project(["llm:chat"])
    async with get_session() as s:
        project = await s.get(ProjectRow, pid)
    stop = AsyncMock(return_value=True)
    out = await run_chat(project=project, capability="chat:fast",
                         messages=[{"role": "user", "content": "hi"}], model=None, max_tokens=16,
                         temperature=0.0, response_format=None, workflow=None, should_stop=stop)
    assert out is CANCELLED and stop.await_count == 1


async def test_execute_skips_the_provider_when_cancel_was_already_requested_at_claim():
    _, pid = await _make_project(["llm:chat"])
    job_id = await _plain_job(pid, "running", cancel_requested=True)
    claimed = await _row(job_id)
    with patch.object(job_queue, "run_chat", AsyncMock(side_effect=AssertionError("no call"))):
        await job_queue._execute(claimed)
    assert (await _row(job_id)).status == "cancelled"


async def test_a_cancelled_job_is_not_requeued_after_a_failed_attempt():
    _, pid = await _make_project(["llm:chat"])
    job_id = await _plain_job(pid, "running", cancel_requested=True)
    await job_queue._requeue_or_fail(job_id, 0, "no provider available")
    row = await _row(job_id)
    assert row.status == "cancelled" and row.retry_count == 0


async def test_cancel_terminal_job_is_already_terminal():
    key, pid = await _make_project(["llm:chat"])
    done = await _plain_job(pid, "done")
    failed = await _plain_job(pid, "error")
    client = TestClient(app)
    for job_id, status in ((done, "done"), (failed, "error")):
        r = client.post(f"/v1/jobs/{job_id}/cancel", headers=_h(key))
        assert r.json() == {"job_id": job_id, "state": "already_terminal", "status": status}
        assert (await _row(job_id)).status == status


async def test_cancel_other_projects_or_unknown_job_is_404():
    key1, pid1 = await _make_project(["llm:chat"])
    key2, _ = await _make_project(["llm:chat"])
    job_id = await _plain_job(pid1, "pending")
    client = TestClient(app)
    assert client.post(f"/v1/jobs/{job_id}/cancel", headers=_h(key2)).status_code == 404
    assert client.post("/v1/jobs/99999/cancel", headers=_h(key1)).status_code == 404
    assert (await _row(job_id)).status == "pending"
    assert client.post(f"/v1/jobs/{job_id}/cancel").status_code in (401, 403, 422)


async def test_cancel_agent_job_pending_cancels_and_running_requests_cancel_via_cancel_run():
    key, pid = await _make_project(["llm:chat", AGENT_SCOPE])
    client = TestClient(app)
    queued = await _insert_agent_job(pid)
    r = client.post(f"/v1/jobs/{queued}/cancel", headers=_h(key)).json()
    assert r["state"] == "cancelled" and r["status"] == "cancelled"
    run, job = await get_run(queued, pid)
    assert run.outcome == "cancelled" and job.status == "cancelled"
    polled = proxy._job_response(job)
    assert polled.status == "cancelled" and polled.error is None
    running = await _insert_agent_job(pid, "agent:codex")
    await _set(running, status="running")
    r = client.post(f"/v1/jobs/{running}/cancel", headers=_h(key)).json()
    assert r == {"job_id": running, "state": "cancel_requested", "status": "running"}
    async with get_session() as s:
        assert (await s.get(AgentRunRow, running)).cancel_requested is True
        assert (await s.get(DeepJobRow, running)).cancel_requested is True
    again = client.post(f"/v1/jobs/{queued}/cancel", headers=_h(key)).json()
    assert again["state"] == "already_terminal"


async def test_cancel_agent_job_needs_the_agent_scope():
    key, pid = await _make_project(["llm:chat"])
    job_id = await _insert_agent_job(pid)
    r = TestClient(app).post(f"/v1/jobs/{job_id}/cancel", headers=_h(key))
    assert r.status_code == 403
    assert (await get_run(job_id, pid))[0].outcome is None


async def test_cancel_agent_race_with_a_finished_run_reports_already_terminal():
    _, pid = await _make_project(["llm:chat", AGENT_SCOPE])
    job_id = await _insert_agent_job(pid)
    from aibroker.agent import runs
    from aibroker.services import job_control

    async def finish_then_cancel(run_id, project_id):
        await runs.append_event(run_id, RunCompleted(final_text="x"))
        raise RunConflict("finished underneath")

    with patch.object(job_control, "cancel_run", finish_then_cancel):
        res = await cancel_job(job_id, pid)
    assert res.state == "already_terminal" and res.status == "done"


async def test_bulk_cancel_by_trace_id_touches_only_own_project_and_in_flight_jobs():
    key, pid = await _make_project(["llm:chat"])
    _, other = await _make_project(["llm:chat"])
    pending = await _plain_job(pid, "pending", trace_id="exam-1")
    running = await _plain_job(pid, "running", trace_id="exam-1")
    done = await _plain_job(pid, "done", trace_id="exam-1")
    untraced = await _plain_job(pid, "pending", trace_id="exam-2")
    foreign = await _plain_job(other, "pending", trace_id="exam-1")
    r = TestClient(app).post("/v1/jobs/cancel", headers=_h(key), json={"trace_id": "exam-1"})
    assert r.status_code == 200
    assert r.json() == {"cancelled": [pending], "cancel_requested": [running], "already_terminal": 0}
    assert (await _row(pending)).status == "cancelled"
    assert (await _row(running)).cancel_requested is True
    assert (await _row(done)).status == "done"
    assert (await _row(untraced)).status == "pending"
    assert (await _row(foreign)).status == "pending"


async def test_bulk_cancel_by_labels_matches_a_subset_and_only_own_project():
    key, pid = await _make_project(["llm:chat"])
    _, other = await _make_project(["llm:chat"])
    a = await _plain_job(pid, "pending", labels={"exam_run_id": "r1", "case_id": "c1"})
    b = await _plain_job(pid, "running", labels={"exam_run_id": "r1"})
    c = await _plain_job(pid, "pending", labels={"exam_run_id": "r2"})
    d = await _plain_job(pid, "pending")
    foreign = await _plain_job(other, "pending", labels={"exam_run_id": "r1"})
    client = TestClient(app)
    r = client.post("/v1/jobs/cancel", headers=_h(key), json={"labels": {"exam_run_id": "r1"}})
    assert r.json() == {"cancelled": [a], "cancel_requested": [b], "already_terminal": 0}
    assert [(await _row(i)).status for i in (c, d, foreign)] == ["pending"] * 3
    r = client.post("/v1/jobs/cancel", headers=_h(key),
                    json={"labels": {"exam_run_id": "r2", "case_id": "c1"}})
    assert r.json() == {"cancelled": [], "cancel_requested": [], "already_terminal": 0}
    assert (await _row(c)).status == "pending"


async def test_bulk_cancel_includes_agent_jobs_only_with_the_agent_scope():
    key, pid = await _make_project(["llm:chat", AGENT_SCOPE])
    plain_key, plain_pid = await _make_project(["llm:chat"])
    agent = await _insert_agent_job(pid)
    async with get_session() as s:
        (await s.get(DeepJobRow, agent)).trace_id = "mix"
    r = TestClient(app).post("/v1/jobs/cancel", headers=_h(key), json={"trace_id": "mix"})
    assert r.json()["cancelled"] == [agent]
    legacy = await _insert_agent_job(plain_pid)
    async with get_session() as s:
        (await s.get(DeepJobRow, legacy)).trace_id = "mix"
    r = TestClient(app).post("/v1/jobs/cancel", headers=_h(plain_key), json={"trace_id": "mix"})
    assert r.json() == {"cancelled": [], "cancel_requested": [], "already_terminal": 0}
    assert (await get_run(legacy, plain_pid))[0].outcome is None


async def test_bulk_cancel_counts_a_job_that_vanished_or_finished_meanwhile():
    _, pid = await _make_project(["llm:chat"])
    job_id = await _plain_job(pid, "pending", trace_id="gone")
    from aibroker.services import job_control
    real = job_control._cancel_plain

    async def finish_first(jid, project_id):
        await _set(jid, status="done")
        return await real(jid, project_id)

    with patch.object(job_control, "_cancel_plain", finish_first):
        out = await cancel_matching(pid, trace_id="gone")
    assert out == ([], [], 1)
    assert (await _row(job_id)).status == "done"


@pytest.mark.parametrize("payload", [{}, {"trace_id": "a", "labels": {"b": "c"}}, {"labels": {}},
                                     {"trace_id": "bad id"}, {"labels": {"k": 1}}])
async def test_bulk_cancel_needs_exactly_one_valid_selector(payload):
    key, _ = await _make_project(["llm:chat"])
    r = TestClient(app).post("/v1/jobs/cancel", headers=_h(key), json=payload)
    assert r.status_code == 422


# ─── schema / dispatcher invariants ─────────────────────────────────────────


def test_claim_returning_list_is_every_deep_jobs_column():
    wanted = {c.strip() for c in job_queue._JOB_COLUMNS.split(",")}
    assert wanted == {c.name for c in DeepJobRow.__table__.columns}


async def test_the_unique_index_is_partial_and_per_project():
    _, p1 = await _make_project(["llm:chat"])
    _, p2 = await _make_project(["llm:chat"])
    async with get_session() as s:
        s.add_all([DeepJobRow(project_id=p1, capability="c", request={}),
                   DeepJobRow(project_id=p1, capability="c", request={}),            # NULL keys never clash
                   DeepJobRow(project_id=p1, capability="c", request={}, idempotency_key="k"),
                   DeepJobRow(project_id=p2, capability="c", request={}, idempotency_key="k")])
    with pytest.raises(IntegrityError):
        async with get_session() as s:
            s.add(DeepJobRow(project_id=p1, capability="c", request={}, idempotency_key="k"))


@pytest.mark.skipif(ON_SQLITE, reason="claim / stale-reclaim / purge are Postgres-only SQL")
async def test_claim_stale_reclaim_and_purge_treat_cancelled_correctly():
    _, pid = await _make_project(["llm:chat"])
    old = "now() - interval '40 days'"
    ids = {}
    async with get_session() as s:
        for name, status in (("cancelled", "cancelled"), ("pending", "pending")):
            ids[name] = (await s.execute(text(
                "INSERT INTO deep_jobs (project_id, capability, status, request, retry_count, "
                f"started_at, completed_at, created_at) VALUES (:p, 'chat:fast', :s, '{{}}', 0, "
                f"{old}, {old}, {old}) RETURNING id"), {"p": pid, "s": status})).scalar_one()
    claimed = await job_queue._claim_batch(10)
    assert [r.id for r in claimed] == [ids["pending"]]          # cancelled is never claimed
    await job_queue._requeue_stale_running()
    async with get_session() as s:
        assert (await s.get(DeepJobRow, ids["cancelled"])).status == "cancelled"   # not reclaimed
    assert await job_queue.purge_finished_jobs() >= 1
    async with get_session() as s:
        assert await s.get(DeepJobRow, ids["cancelled"]) is None                   # pruned like done/error
        assert (await s.get(DeepJobRow, ids["pending"])).status == "running"
