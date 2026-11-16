"""Public disabled gate and mock route -> dispatcher -> ledger -> poll integration."""
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from aibroker.db.engine import get_session
from aibroker.db.models import DeepJobRow, ProjectRow, UsageLogRow
from aibroker.main import app
from aibroker.providers.vivix_admission import request_digest
from aibroker.services import job_queue, vivix_runtime
from aibroker.services.job_control import cancel_job
from aibroker.services.vivix_runtime import Authorization, Receipt
from tests.test_routes_proxy import _make_project
from tests.test_vivix_billing import binding as binding
from tests.test_vivix_service import INPUTS, LIMITS, MODEL, quote, transport
from tests.test_vivix_service import ledger as ledger

client = TestClient(app)
BODY = {"provider": "vivix", "model": MODEL, "inputs": INPUTS}


@pytest.fixture
async def runtime(binding, monkeypatch):
    calls = []

    async def authorize(project, model, inputs):
        return Authorization(1, "account", frozenset({model}),
                             replace(quote(), request_hash=request_digest(model, inputs)), LIMITS, True)

    async def receipt(snapshot):
        return Receipt("1.25", "fixture-receipt") if snapshot.body["state"] == "terminal" else None

    runtime = SimpleNamespace(transport=transport(calls), authorize=authorize, receipt=receipt, calls=calls)
    monkeypatch.setattr(vivix_runtime, "get_runtime", lambda: runtime)
    monkeypatch.setattr("aibroker.services.vivix_jobs.decrypt", lambda _: "FAKE")
    return runtime


async def tick(job_id):
    async with get_session() as session:
        row = await session.get(DeepJobRow, job_id)
        row.status = "running"
        row.started_at = datetime.now(UTC).replace(tzinfo=None)
        await session.flush()
    await job_queue._execute(row)
    async with get_session() as session:
        return await session.get(DeepJobRow, job_id)


async def test_default_route_disabled_no_job_or_reserve():
    key, _ = await _make_project(["llm:video"])
    response = client.post("/v1/video/jobs", json=BODY,
                           headers={"X-Project-Key": key, "Idempotency-Key": "fixture-1"})
    assert response.status_code == 503 and "disabled" in response.text
    async with get_session() as session:
        assert await session.scalar(select(func.count()).select_from(DeepJobRow)) == 0


@pytest.mark.parametrize("extra", [{"quote": {"ceiling_usd": "0"}}, {"api_key": "FAKE"},
                                  {"enabled": True}, {"account_id": "account"}])
async def test_public_request_cannot_supply_trusted_evidence(extra):
    key, _ = await _make_project(["llm:video"])
    response = client.post("/v1/video/jobs", json={**BODY, **extra},
                           headers={"X-Project-Key": key, "Idempotency-Key": "fixture-1"})
    assert response.status_code == 422


async def test_vivix_cannot_fall_back_to_gemini_request_shape():
    key, _ = await _make_project(["llm:video"])
    response = client.post("/v1/video/jobs", json={"provider": "vivix", "prompt": "mock"},
                           headers={"X-Project-Key": key})
    assert response.status_code == 422


async def test_route_scope_and_idempotency_required(runtime):
    key, _ = await _make_project(["llm:chat"])
    assert client.post("/v1/video/jobs", json=BODY, headers={"X-Project-Key": key}).status_code == 403
    key, _ = await _make_project(["llm:video"])
    assert client.post("/v1/video/jobs", json=BODY, headers={"X-Project-Key": key}).status_code == 400
    assert runtime.calls == []


async def test_route_dispatch_poll_settlement_and_idempotency(runtime):
    key, pid = await _make_project(["llm:video"])
    headers = {"X-Project-Key": key, "Idempotency-Key": "fixture-1"}
    response = client.post("/v1/video/jobs", json=BODY, headers=headers)
    assert response.status_code == 202
    job_id = response.json()["job_id"]
    assert client.post("/v1/video/jobs", json=BODY, headers=headers).json()["job_id"] == job_id
    first = await tick(job_id)
    assert first.status == "pending" and first.retry_count == 0
    assert first.result_meta["cost_usd"] is None
    last = await tick(job_id)
    assert last.status == "done"
    polled = client.get(f"/v1/jobs/{job_id}", headers=headers)
    assert polled.status_code == 200 and polled.json()["video_url"].endswith("fixture.mp4")
    assert polled.json()["cost_usd"] == 1.25 and polled.json()["billing_status"] == "settled"
    assert runtime.calls == ["GET", "POST", "GET"]
    async with get_session() as session:
        assert await session.scalar(select(func.count()).select_from(UsageLogRow).where(
            UsageLogRow.project_id == pid)) == 1
    other, _ = await _make_project(["llm:video"])
    assert client.get(f"/v1/jobs/{job_id}", headers={"X-Project-Key": other}).status_code == 404


async def test_ambiguous_dispatch_never_enters_generic_retry(runtime):
    runtime.transport = transport(runtime.calls, post="timeout")
    key, _ = await _make_project(["llm:video"])
    job_id = client.post("/v1/video/jobs", json=BODY, headers={
        "X-Project-Key": key, "Idempotency-Key": "fixture-1"}).json()["job_id"]
    row = await tick(job_id)
    assert row.status == "error" and "reconciliation" in row.error_message
    await tick(job_id)
    assert runtime.calls == ["GET", "POST"]


async def test_cancel_pending_post_submission_does_not_drop_accounting(runtime):
    key, pid = await _make_project(["llm:video"])
    job_id = client.post("/v1/video/jobs", json=BODY, headers={
        "X-Project-Key": key, "Idempotency-Key": "fixture-1"}).json()["job_id"]
    await tick(job_id)
    cancelled = await cancel_job(job_id, pid)
    assert cancelled.state == "cancel_requested"
    row = await tick(job_id)
    assert row.status == "done" and row.result_meta["cost_usd"] == 1.25
    assert runtime.calls == ["GET", "POST", "GET"]


async def test_disabled_dispatcher_rejects_preexisting_queued_vivix():
    key, pid = await _make_project(["llm:video"])
    async with get_session() as session:
        row = DeepJobRow(project_id=pid, capability="video", status="pending", request={
            "model": MODEL, "vivix": INPUTS})
        session.add(row)
        await session.flush()
        job_id = row.id
    assert (await tick(job_id)).status == "error"


async def test_revoked_project_cannot_resume(runtime):
    key, pid = await _make_project(["llm:video"])
    job_id = client.post("/v1/video/jobs", json=BODY, headers={
        "X-Project-Key": key, "Idempotency-Key": "fixture-1"}).json()["job_id"]
    await tick(job_id)
    async with get_session() as session:
        project = await session.get(ProjectRow, pid)
        project.is_active = False
    assert (await tick(job_id)).status == "error"
    assert runtime.calls == ["GET", "POST"]


async def test_idempotency_replay_does_not_spend_request_cap_twice(runtime):
    key, pid = await _make_project(["llm:video"])
    async with get_session() as session:
        project = await session.get(ProjectRow, pid)
        project.total_request_cap = 1
    headers = {"X-Project-Key": key, "Idempotency-Key": "fixture-1"}
    first = client.post("/v1/video/jobs", json=BODY, headers=headers)
    assert first.status_code == 202
    assert client.post("/v1/video/jobs", json=BODY, headers=headers).json() == first.json()
    async with get_session() as session:
        project = await session.get(ProjectRow, pid)
        assert project.total_requests_used == 1
