"""Subscription agent runs (deep_jobs `agent:*` + agent_runs, never in usage_log)
on the project page and in the shared Requests journal.

Counting rule under test: a plain job's attempts are counted through usage_log
only; an agent job is counted through deep_jobs/agent_runs only - never both.
"""
from __future__ import annotations

import csv
import io
from datetime import timedelta
from typing import Any

from fastapi.testclient import TestClient

from aibroker.db.models import AgentRunRow
from aibroker.main import app
from aibroker.routes import dashboard_agent as da
from aibroker.routes import dashboard_requests as rq
from tests import dash_seed as ds

client = TestClient(app)


def _agent(id_: int, *, cap: str = "agent:codex", status: str = "done", pid: int = 1,
           minutes_ago: int = 10, model: str | None = "gpt-6.1-sol", tin: int = 1000,
           tout: int = 200, cache: int = 800, meta: dict[str, Any] | None = None,
           labels: dict[str, str] | None = None, dur_s: int | None = 30) -> list[Any]:
    created = ds.now() - timedelta(minutes=minutes_ago)
    started = created + timedelta(seconds=2) if status not in ("pending",) else None
    done = started + timedelta(seconds=dur_s) if started and dur_s is not None and status in (
        "done", "error", "cancelled") else None
    job = ds.job(id_, status, pid, minutes_ago, capability=cap, labels=labels, result_meta=meta,
                 started_at=started, completed_at=done)
    run = AgentRunRow(run_id=id_, project_id=pid, capability=cap, task_id=f"t{id_}",
                      account_id="owner", model=model, usage_input_tokens=tin,
                      usage_output_tokens=tout, usage_cache_read_tokens=cache,
                      billing_mode="subscription")
    return [job, run]


def _seed_project_one() -> None:
    ds.seed(ds.project(1, "stepan"), ds.project(2, "vera"), ds.key(1, "gemini", "k1"))
    ds.seed(ds.usage(1, minutes_ago=30), ds.usage(2, minutes_ago=20, provider="groq",
                                                  model="groq/x"))
    ds.seed(*_agent(101, labels={"exam_run_id": "ex-1", "run_id": "r-1"},
                    meta={"model_served": "gpt-6.1-sol-2026"}, minutes_ago=15),
            *_agent(102, status="error", labels={"exam_run_id": "ex-2"}, minutes_ago=14),
            *_agent(103, cap="agent:claude", status="cancelled", model="claude-opus-5",
                    tin=10, tout=2, cache=0, minutes_ago=13),
            *_agent(104, status="running", minutes_ago=2, dur_s=None))
    # another project's agent job must never show up on project 1
    ds.seed(*_agent(900, pid=2, labels={"exam_run_id": "other"}, model="other-model"))


def _bd(pid: int = 1):
    return ds.run(da.project_breakdown_with_agents(pid, None, None, "day"))


def test_project_totals_and_breakdowns_include_agent_jobs():
    _seed_project_one()
    d = _bd()
    t = d["totals"]
    assert t["api_calls"] == 2 and t["agent"]["n"] == 4 and t["calls"] == 6
    assert t["agent"]["done"] == 1 and t["agent"]["error"] == 1
    assert t["agent"]["cancelled"] == 1 and t["agent"]["active"] == 1
    assert t["spend"] == 0.0                         # subscription: no invented price
    assert t["cache_read"] == 800 * 3 + 0            # 101, 102, 104 (agent_runs.usage_*)
    prov = {r["k"]: r for r in d["by_provider"]}
    assert prov["codex-sub"]["n"] == 3 and prov["claude-sub"]["n"] == 1
    assert prov["gemini"]["n"] == 1 and prov["groq"]["n"] == 1
    model = {r["k"]: r for r in d["by_model"]}
    assert model["gpt-6.1-sol"]["n"] == 3 and model["gpt-6.1-sol"]["tin"] == 3000
    assert model["gpt-6.1-sol"]["served"] == ["gpt-6.1-sol-2026"]
    assert model["claude-opus-5"]["n"] == 1
    assert "other-model" not in model
    caps = {r["k"]: r["n"] for r in d["by_capability"]}
    assert caps["agent:codex"] == 3 and caps["agent:claude"] == 1
    assert sum(x["agent"] for x in d["daily"]) == 4
    assert sum(x["api"] for x in d["daily"]) == 2


def test_result_meta_provider_wins_over_capability_default():
    ds.seed(ds.project(1, "stepan"))
    ds.seed(*_agent(1, meta={"provider": "codex-sub-b"}), *_agent(2))
    prov = {r["k"]: r["n"] for r in _bd()["by_provider"]}
    assert prov == {"codex-sub-b": 1, "codex-sub": 1}
    assert da.agent_provider("agent:claude", None) == "claude-sub"


def test_project_page_renders_agent_totals_models_and_runs():
    _seed_project_one()
    body = ds.get(client, "/dashboard/projects/1", params={"range": "all"}).text
    assert "Agent runs" in body and "codex-sub" in body and "claude-sub" in body
    assert "gpt-6.1-sol" in body and "subscription" in body
    assert "ex-1" in body and "ex-2" in body        # labels shown
    assert "other-model" not in body and "ex-other" not in body and ">other<" not in body
    models = ds.get(client, "/dashboard/projects/1", params={"range": "all", "tab": "models"}).text
    assert "gpt-6.1-sol" in models and "gpt-6.1-sol-2026" in models


def test_no_double_counting_plain_job_counts_via_usage_log_agent_via_jobs():
    ds.seed(ds.project(1, "stepan"), ds.key(1, "gemini", "k1"))
    # plain job: 2 attempts (one failed, one ok) in usage_log, job row done
    ds.seed(ds.job(7, "done", 1, 20, capability="chat:deep"),
            ds.usage(1, minutes_ago=19, status="error", request_id="job-7",
                     capability="chat:deep"),
            ds.usage(2, minutes_ago=18, request_id="job-7", capability="chat:deep"))
    d = _bd()
    assert d["totals"]["calls"] == 2 and d["totals"]["agent"]["n"] == 0
    rows = ds.run(rq.query_requests(rq.RequestFilter(project_id=1)))[0]
    assert [r["request_id"] for r in rows] == ["job-7"]            # one request, 2 tries
    # now an agent job: +1 everywhere, exactly once
    ds.seed(*_agent(8, minutes_ago=5))
    d = _bd()
    assert d["totals"]["calls"] == 3 and d["totals"]["agent"]["n"] == 1
    assert {r["k"]: r["n"] for r in d["by_provider"]} == {
        "gemini": 2, "codex-sub": 1}
    rows = ds.run(rq.query_requests(rq.RequestFilter(project_id=1)))[0]
    assert sorted(r["request_id"] for r in rows) == ["job-7", "job-8"]
    assert len(d["agent_recent"]) == 1 and d["agent_recent"][0]["request_id"] == "job-8"


def test_journal_lists_agent_jobs_with_all_fields():
    _seed_project_one()
    rows = ds.run(rq.query_requests(rq.RequestFilter(project_id=1, kind="agent")))[0]
    by = {r["request_id"]: r for r in rows}
    assert set(by) == {"job-101", "job-102", "job-103", "job-104"}
    a = by["job-101"]
    assert (a["state"], a["is_agent"], a["billing"]) == ("ok", True, "subscription")
    assert (a["provider"], a["model"], a["model_served"]) == (
        "codex-sub", "gpt-6.1-sol", "gpt-6.1-sol-2026")
    assert (a["tokens_in"], a["tokens_out"], a["cache_read_tokens"]) == (1000, 200, 800)
    assert a["duration_ms"] == 30_000 and a["latency_ms"] == 30_000 and a["cost_usd"] == 0.0
    assert a["labels"] == {"exam_run_id": "ex-1", "run_id": "r-1"}
    assert a["capability"] == "agent:codex" and a["created_at"] is not None
    assert {k: v["state"] for k, v in by.items()} == {
        "job-101": "ok", "job-102": "failed", "job-103": "cancelled", "job-104": "running"}
    assert by["job-103"]["provider"] == "claude-sub" and by["job-104"]["duration_ms"] is None


def test_journal_filters_label_provider_model_type_and_existing_ones():
    _seed_project_one()

    def ids(**params: str) -> set[str]:
        f = rq.RequestFilter.from_params(params, None, None)
        return {r["request_id"] for r in ds.run(rq.query_requests(f))[0]}
    assert ids(label="ex-1") == {"job-101"}
    assert ids(label="exam_run_id:ex-2") == {"job-102"}
    assert ids(label="run_id:r-1") == {"job-101"}
    assert ids(label="run_id:ex-1") == set()          # key pinned: wrong key never matches
    assert ids(label="other") == {"job-900"}
    assert ids(provider="claude-sub") == {"job-103"}
    assert ids(provider="codex-sub", project="1") == {"job-101", "job-102", "job-104"}
    assert ids(model="gpt-6.1-sol", project="1") == {"job-101", "job-102", "job-104"}
    assert ids(type="agent", project="1", status="failed") == {"job-102"}
    assert ids(type="agent", status="cancelled") == {"job-103"}
    assert ids(type="job", project="1", status="running") == {"job-104"}
    assert ids(project="1", type="direct") == {"u-1", "u-2"}
    assert ids(provider="gemini", project="1") == {"u-1"}


def test_requests_page_and_csv_show_agent_columns_and_label_filter():
    _seed_project_one()
    body = ds.get(client, "/dashboard/requests",
                  params={"range": "all", "label": "exam_run_id:ex-1"}).text
    assert "job-101" in body and "gpt-6.1-sol" in body and "codex-sub" in body
    assert "ex-1" in body and "job-102" not in body
    assert "f-label" in body
    csv_text = ds.get(client, "/dashboard/requests.csv",
                      params={"range": "all", "type": "agent", "project": "1"}).text
    rows = list(csv.DictReader(io.StringIO(csv_text)))
    assert len(rows) == 4
    first = {r["request_id"]: r for r in rows}["job-101"]
    assert first["billing"] == "subscription" and "exam_run_id=ex-1" in first["labels"]
    assert first["model"] == "gpt-6.1-sol" and first["cache_read_tokens"] == "800"


def test_agent_job_drawer_opens():
    _seed_project_one()
    r = client.get("/dashboard/requests/job-101", cookies=ds.cookies(),
                   headers={"hx-request": "true"})
    assert r.status_code == 200 and "job-101" in r.text
