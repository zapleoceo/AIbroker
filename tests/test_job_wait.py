"""Long-poll job delivery: GET /v1/jobs/{id}?wait and POST /v1/jobs/wait (services/job_wait)."""
from __future__ import annotations

import asyncio
import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import update

from aibroker.db import get_session
from aibroker.db.models import DeepJobRow
from aibroker.main import app
from aibroker.services import job_wait
from aibroker.services.deep_jobs import _finish
from tests.test_routes_proxy import _make_project

client = TestClient(app)
_MSGS = [{"role": "user", "content": "hi"}]


@pytest.fixture(autouse=True)
def _no_redis(monkeypatch):
    monkeypatch.delenv("REDIS_URL", raising=False)
    monkeypatch.setattr(job_wait, "SWEEP_S", 0.05)


def _submit(plain: str, n: int = 1) -> list[int]:
    ids = []
    for i in range(n):
        r = client.post("/v1/jobs?capability=structured", headers={"X-Project-Key": plain},
                        json={"messages": _MSGS, "workflow": f"wait-{time.time_ns()}-{i}"})
        assert r.status_code == 202, r.text
        ids.append(r.json()["job_id"])
    return ids


async def test_wait_any_returns_at_once_for_a_terminal_job():
    plain, pid = await _make_project(["llm:chat"])
    [job] = _submit(plain)
    await _finish(job, status="done", result_text="{}")
    t0 = time.monotonic()
    await job_wait.wait_any(pid, [job], 5)
    assert time.monotonic() - t0 < 1


async def test_finish_wakes_a_waiter_without_waiting_for_the_timeout():
    plain, pid = await _make_project(["llm:chat"])
    [job] = _submit(plain)
    monkey_sweep = job_wait.SWEEP_S
    job_wait.SWEEP_S = 60          # only the publish path may wake it
    try:
        waiter = asyncio.create_task(job_wait.wait_any(pid, [job], 10))
        await asyncio.sleep(0.2)
        t0 = time.monotonic()
        await _finish(job, status="error", error_message="x")
        await asyncio.wait_for(waiter, 2)
        assert time.monotonic() - t0 < 0.5
    finally:
        job_wait.SWEEP_S = monkey_sweep
        await job_wait.stop()


async def test_sweep_catches_a_transition_that_did_not_publish():
    plain, pid = await _make_project(["llm:chat"])
    [job] = _submit(plain)
    waiter = asyncio.create_task(job_wait.wait_any(pid, [job], 10))
    await asyncio.sleep(0.1)
    async with get_session() as s:      # e.g. the stale-run reaper's raw UPDATE
        await s.execute(update(DeepJobRow).where(DeepJobRow.id == job).values(status="error"))
    await asyncio.wait_for(waiter, 2)
    await job_wait.stop()


async def test_wait_any_times_out_and_cleans_up():
    plain, pid = await _make_project(["llm:chat"])
    [job] = _submit(plain)
    t0 = time.monotonic()
    await job_wait.wait_any(pid, [job], 0.3)
    assert 0.25 < time.monotonic() - t0 < 2
    assert job not in job_wait._waiters and pid not in job_wait._per_project
    await job_wait.stop()


async def test_waiter_cap_per_project(monkeypatch):
    monkeypatch.setattr(job_wait, "MAX_WAITERS_PER_PROJECT", 1)
    plain, pid = await _make_project(["llm:chat"])
    [job] = _submit(plain)
    first = asyncio.create_task(job_wait.wait_any(pid, [job], 5))
    await asyncio.sleep(0.1)
    with pytest.raises(job_wait.TooManyWaiters):
        await job_wait.wait_any(pid, [job], 5)
    await _finish(job, status="done", result_text="{}")
    await asyncio.wait_for(first, 2)
    await job_wait.stop()


async def test_get_wait_returns_pending_after_the_wait_and_done_at_once():
    plain, _ = await _make_project(["llm:chat"])
    h = {"X-Project-Key": plain}
    [job] = _submit(plain)
    t0 = time.monotonic()
    r = client.get(f"/v1/jobs/{job}?wait=0.3", headers=h)
    assert r.status_code == 200 and r.json()["status"] == "pending"
    assert time.monotonic() - t0 >= 0.25
    await _finish(job, status="done", result_text='{"a": 1}')
    t0 = time.monotonic()
    r = client.get(f"/v1/jobs/{job}?wait=20", headers=h)
    assert r.json()["status"] == "done" and r.json()["text"] == '{"a": 1}'
    assert time.monotonic() - t0 < 2


async def test_batch_wait_splits_done_pending_and_unknown():
    plain, _ = await _make_project(["llm:chat"])
    other, _ = await _make_project(["llm:chat"])
    a, b = _submit(plain, 2)
    [foreign] = _submit(other)
    await _finish(a, status="done", result_text="{}")
    r = client.post("/v1/jobs/wait", headers={"X-Project-Key": plain},
                    json={"ids": [a, str(b), foreign, "nope", 999999999, a], "wait": 5})
    assert r.status_code == 200, r.text
    body = r.json()
    assert [d["job_id"] for d in body["done"]] == [a]
    assert body["done"][0] == client.get(f"/v1/jobs/{a}", headers={"X-Project-Key": plain}).json()
    assert body["pending"] == [b]
    assert sorted(body["unknown"]) == sorted([str(foreign), "nope", "999999999"])


async def test_batch_wait_times_out_with_everything_pending():
    plain, _ = await _make_project(["llm:chat"])
    ids = _submit(plain, 2)
    t0 = time.monotonic()
    r = client.post("/v1/jobs/wait", headers={"X-Project-Key": plain},
                    json={"ids": ids, "wait": 0.3})
    assert r.json() == {"done": [], "pending": ids, "unknown": []}
    assert time.monotonic() - t0 >= 0.25


async def test_batch_wait_limits():
    plain, _ = await _make_project(["llm:chat"])
    h = {"X-Project-Key": plain}
    assert client.post("/v1/jobs/wait", headers=h,
                       json={"ids": list(range(1, 102))}).status_code == 422
    assert client.post("/v1/jobs/wait", headers=h, json={"ids": []}).status_code == 422
    assert client.post("/v1/jobs/wait", headers=h,
                       json={"ids": [1], "wait": -1}).status_code == 422
    assert client.post("/v1/jobs/wait", json={"ids": [1]}).status_code in (401, 403)


async def test_too_many_waiters_is_a_429_with_retry_after(monkeypatch):
    plain, pid = await _make_project(["llm:chat"])
    monkeypatch.setattr(job_wait, "MAX_WAITERS_PER_PROJECT", 0)
    [job] = _submit(plain)
    r = client.get(f"/v1/jobs/{job}?wait=1", headers={"X-Project-Key": plain})
    assert r.status_code == 429 and r.headers["retry-after"] == str(job_wait.RETRY_AFTER_S)
    r = client.post("/v1/jobs/wait", headers={"X-Project-Key": plain},
                    json={"ids": [job], "wait": 1})
    assert r.status_code == 429
