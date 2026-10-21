"""GET /v1/jobs?label.<k>=<v>: the read-only label listing (docs/api.md, "List jobs by label").

HTTP-level through the real app; plain jobs are inserted straight into deep_jobs, agent jobs through
create_run. Portable SQL, so the same tests run on SQLite and Postgres.
"""
from __future__ import annotations

import base64
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from aibroker.agent.constants import AGENT_SCOPE
from aibroker.main import app
from aibroker.services import job_list
from aibroker.services.job_queue import JOB_RETENTION_DAYS
from tests.test_job_idempotency import _h, _insert_agent_job, _plain_job, _set
from tests.test_routes_proxy import _make_project

LBL = {"exam_run_id": "run-1"}
FIELDS = {"job_id", "status", "capability", "model", "requested_model", "created_at", "started_at", "completed_at", "labels",
          "trace_id", "idempotency_key", "retry_count", "cancel_requested"}


def _get(client: TestClient, key: str, query: str = "label.exam_run_id=run-1", **headers):
    return client.get(f"/v1/jobs?{query}", headers=_h(key, **headers))


def _ids(resp) -> list[int]:
    return [j["job_id"] for j in resp.json()["jobs"]]


def _cursor(**data) -> str:
    return base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip("=")


class _OpenAgents:
    """Agent gate wide open: every project allowed, any model/account."""

    def __contains__(self, _item) -> bool:
        return True


@pytest.fixture
def agents_open(monkeypatch):
    monkeypatch.setattr("aibroker.agent.constants.allowed_project_ids", lambda: _OpenAgents())
    monkeypatch.setattr("aibroker.agent.policy.policy_for", lambda _pid: SimpleNamespace(
        allows_model=lambda _m: True, allows_account=lambda _a: True))


async def test_lists_only_the_callers_jobs_with_the_label_and_no_request_or_result_fields():
    key, pid = await _make_project(["llm:chat"])
    _, other = await _make_project(["llm:chat"])
    mine = await _plain_job(pid, labels=LBL, trace_id="t-1", idempotency_key="k-1")
    await _plain_job(pid, labels={"exam_run_id": "run-2"})
    await _plain_job(pid)
    await _plain_job(other, labels=LBL)
    await _set(mine, result_text="SECRET-ANSWER", error_message="SECRET-ERR",
               result_meta={"model": "m-served", "tool_calls": "SECRET-META"},
               request={"model": "m-pin", "messages": [{"role": "user", "content": "SECRET-PROMPT"}]})
    r = _get(TestClient(app), key)
    assert r.status_code == 200, r.text
    body = r.json()
    assert _ids(r) == [mine]
    assert set(body["jobs"][0]) == FIELDS
    item = body["jobs"][0]
    assert item["labels"] == LBL and item["trace_id"] == "t-1" and item["idempotency_key"] == "k-1"
    assert item["status"] == "pending" and item["retry_count"] == 0 and item["cancel_requested"] is False
    assert item["model"] == "m-served" and item["requested_model"] == "m-pin"
    assert "SECRET" not in r.text and '"request"' not in r.text and "messages" not in r.text
    assert set(body) == {"jobs", "next_cursor", "pagination_complete", "no_nonterminal_in_filter",
                         "high_watermark", "retention_days", "as_of"}
    assert body["retention_days"] == JOB_RETENTION_DAYS


@pytest.mark.parametrize("query", [
    "", "limit=5",                                       # no label
    "label.a=1&label.b=2",                               # two labels
    "label.a=1&label.a=2",                               # same key twice
    "label.bad%20key=1", "label.a=has%20space", "label.a=", "label.=1",
    "label.a=1&trace_id=bad%20id", "label.a=1&status=weird", "label.a=1&limit=0",
    "label.a=1&limit=201", "label.a=1&surprise=1",
])
async def test_label_pair_validation_is_422(query):
    key, _ = await _make_project(["llm:chat"])
    assert TestClient(app).get(f"/v1/jobs?{query}", headers=_h(key)).status_code == 422


async def test_requires_a_project_key():
    assert TestClient(app).get("/v1/jobs?label.a=1").status_code in (401, 403, 422)


async def test_paging_three_pages_returns_each_id_once_in_order():
    key, pid = await _make_project(["llm:chat"])
    ids = [await _plain_job(pid, labels=LBL) for _ in range(5)]
    client = TestClient(app)
    seen: list[int] = []
    cursor, pages = None, 0
    while True:
        q = "label.exam_run_id=run-1&limit=2" + (f"&cursor={cursor}" if cursor else "")
        r = _get(client, key, q)
        assert r.status_code == 200, r.text
        seen += _ids(r)
        pages += 1
        cursor = r.json()["next_cursor"]
        assert r.json()["pagination_complete"] is (cursor is None)
        if cursor is None:
            break
    assert pages == 3 and seen == ids


async def test_high_watermark_excludes_a_job_created_after_the_first_page():
    key, pid = await _make_project(["llm:chat"])
    ids = [await _plain_job(pid, labels=LBL) for _ in range(3)]
    client = TestClient(app)
    first = _get(client, key, "label.exam_run_id=run-1&limit=2").json()
    assert first["high_watermark"] == ids[-1]
    late = await _plain_job(pid, labels=LBL)
    second = _get(client, key, f"label.exam_run_id=run-1&limit=2&cursor={first['next_cursor']}").json()
    assert [j["job_id"] for j in second["jobs"]] == [ids[2]] and late not in [j["job_id"] for j in second["jobs"]]
    assert second["high_watermark"] == ids[-1] and second["pagination_complete"] is True
    assert late > second["high_watermark"]
    fresh = _get(client, key).json()      # a new walk sees it, with a newer watermark
    assert [j["job_id"] for j in fresh["jobs"]] == [*ids, late] and fresh["high_watermark"] == late


async def test_pagination_complete_and_no_nonterminal_are_independent_flags():
    key, pid = await _make_project(["llm:chat"])
    await _plain_job(pid, labels=LBL, status="running")     # on page 1
    await _plain_job(pid, labels=LBL, status="done")
    await _plain_job(pid, labels=LBL, status="done")        # last page is all done
    client = TestClient(app)
    p1 = _get(client, key, "label.exam_run_id=run-1&limit=2").json()
    p2 = _get(client, key, f"label.exam_run_id=run-1&limit=2&cursor={p1['next_cursor']}").json()
    assert (p1["pagination_complete"], p1["no_nonterminal_in_filter"]) == (False, False)
    assert [j["status"] for j in p2["jobs"]] == ["done"]
    assert (p2["pagination_complete"], p2["no_nonterminal_in_filter"]) == (True, False)
    key2, pid2 = await _make_project(["llm:chat"])
    await _plain_job(pid2, labels=LBL, status="done")
    r = _get(client, key2).json()
    assert (r["pagination_complete"], r["no_nonterminal_in_filter"]) == (True, True)


async def test_no_nonterminal_ignores_the_status_filter_and_other_labels():
    key, pid = await _make_project(["llm:chat"])
    await _plain_job(pid, labels=LBL, status="done")
    await _plain_job(pid, labels=LBL, status="pending")
    await _plain_job(pid, labels={"exam_run_id": "run-2"}, status="running")
    r = _get(TestClient(app), key, "label.exam_run_id=run-1&status=done").json()
    assert [j["status"] for j in r["jobs"]] == ["done"]
    assert r["no_nonterminal_in_filter"] is False       # the pending job is filtered out of the list only
    r2 = _get(TestClient(app), key, "label.exam_run_id=run-2&status=done").json()
    assert r2["jobs"] == [] and r2["no_nonterminal_in_filter"] is False


async def test_no_nonterminal_is_computed_on_the_first_page_and_carried_in_the_cursor():
    key, pid = await _make_project(["llm:chat"])
    await _plain_job(pid, labels=LBL, status="done")
    live = await _plain_job(pid, labels=LBL, status="running")
    client = TestClient(app)
    first = _get(client, key, "label.exam_run_id=run-1&limit=1").json()
    assert first["no_nonterminal_in_filter"] is False
    await _set(live, status="done")   # a rescan on page 2 would now say True; the carried value must not
    second = _get(client, key, f"label.exam_run_id=run-1&limit=1&cursor={first['next_cursor']}").json()
    assert second["no_nonterminal_in_filter"] is False and second["pagination_complete"] is True


async def test_status_and_trace_id_filters():
    key, pid = await _make_project(["llm:chat"])
    a = await _plain_job(pid, labels=LBL, status="done", trace_id="t-a")
    b = await _plain_job(pid, labels=LBL, status="error", trace_id="t-b")
    c = await _plain_job(pid, labels=LBL, status="done", trace_id="t-b")
    client = TestClient(app)
    assert _ids(_get(client, key, "label.exam_run_id=run-1&status=done")) == [a, c]
    assert _ids(_get(client, key, "label.exam_run_id=run-1&status=error")) == [b]
    assert _ids(_get(client, key, "label.exam_run_id=run-1&trace_id=t-b")) == [b, c]
    assert _ids(_get(client, key, "label.exam_run_id=run-1&trace_id=t-b&status=done")) == [c]


async def test_agent_rows_are_listed_only_when_the_poll_gate_would_allow_them(agents_open, monkeypatch):
    key, pid = await _make_project(["llm:chat", AGENT_SCOPE])
    plain = await _plain_job(pid, labels=LBL, status="done")
    agent = await _insert_agent_job(pid)
    await _set(agent, labels=LBL, status="running")
    client = TestClient(app)
    r = _get(client, key).json()
    assert [j["job_id"] for j in r["jobs"]] == [plain, agent] and r["no_nonterminal_in_filter"] is False
    # policy refuses the run now: a poll would answer 403/policy error, so it is not listed
    monkeypatch.setattr("aibroker.agent.policy.policy_for", lambda _pid: None)
    r = _get(client, key).json()
    assert [j["job_id"] for j in r["jobs"]] == [plain] and r["no_nonterminal_in_filter"] is True


async def test_agent_rows_are_excluded_without_the_agent_scope(agents_open):
    key, pid = await _make_project(["llm:chat"])
    plain = await _plain_job(pid, labels=LBL, status="done")
    agent = await _insert_agent_job(pid)
    await _set(agent, labels=LBL, status="running")
    r = _get(TestClient(app), key).json()
    assert [j["job_id"] for j in r["jobs"]] == [plain] and r["no_nonterminal_in_filter"] is True
    assert TestClient(app).get(f"/v1/jobs/{agent}", headers=_h(key)).status_code == 403   # poll agrees


@pytest.mark.parametrize("cursor", [
    "!!!", "bm90LWpzb24", _cursor(after=1), _cursor(after=2**63, hw=2**63, nn=True), _cursor(after=1, hw=10**30, nn=True),
    _cursor(after=1, hw=2), _cursor(after=1, hw=2, nn="x"), _cursor(after="1", hw=2, nn=True), _cursor(after=-1, hw=2, nn=True),
    _cursor(after=5, hw=2, nn=True), _cursor(after=True, hw=2, nn=True), _cursor(after=1, hw=2, nn=True, x=3),
    base64.urlsafe_b64encode(b"[1,2]").decode(),
])
async def test_malformed_or_tampered_cursor_is_422(cursor):
    key, _ = await _make_project(["llm:chat"])
    r = TestClient(app).get(f"/v1/jobs?label.exam_run_id=run-1&cursor={cursor}", headers=_h(key))
    assert r.status_code == 422, r.text


async def test_a_forged_cursor_cannot_reach_another_projects_jobs():
    key, pid = await _make_project(["llm:chat"])
    _, other = await _make_project(["llm:chat"])
    foreign = await _plain_job(other, labels=LBL)
    mine = await _plain_job(pid, labels=LBL)
    cur = _cursor(after=0, hw=mine + 1000, nn=True)
    assert _ids(_get(TestClient(app), key, f"label.exam_run_id=run-1&cursor={cur}")) == [mine]
    assert foreign not in _ids(_get(TestClient(app), key))


def test_cursor_roundtrip():
    c = job_list.Cursor(7, 20, True)
    assert job_list.decode_cursor(job_list.encode_cursor(c)) == c
    with pytest.raises(job_list.BadCursor):
        job_list.decode_cursor(job_list.encode_cursor(job_list.Cursor(0, 0, False)) + "!")
