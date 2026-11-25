"""MCP protocol and authorization through the actual FastAPI entry points."""
from __future__ import annotations

import base64
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from aibroker.db import get_session
from aibroker.db.models import DeepJobRow, ProjectRow
from aibroker.main import app
from tests.test_routes_proxy import _make_project

client = TestClient(app)
URL = "/mcp/video"
VIDEO = "llm:video"
HEYGEN = "llm:video:heygen"
ARGS = {"provider": "gemini", "prompt": "Test fixture", "idempotency_key": "run-1",
        "confirm_paid_generation": True}
HD = {"Accept": "application/json, text/event-stream", "MCP-Protocol-Version": "2025-11-25"}


def _rpc(token, method, params=None, **headers):
    msg = {"jsonrpc": "2.0", "id": 123, "method": method}
    if params is not None:
        msg["params"] = params
    return client.post(URL, headers={**HD, **_auth(token), **headers}, json=msg)


def _auth(token):
    return {"Authorization": f"Bearer {token}"} if token else {}


def _call(token, name, args=None):
    return _rpc(token, "tools/call", {"name": name, "arguments": {} if args is None else args}).json()


def _generate(token, **overrides):
    return _call(token, "video_generate", ARGS | overrides)["result"]


async def _finish(job_id, video_bytes):
    async with get_session() as session:
        await session.execute(update(DeepJobRow).where(DeepJobRow.id == job_id).values(
            status="done", result_text=base64.b64encode(video_bytes).decode() if video_bytes else None,
            result_meta={"provider_url": "https://internal.invalid", "key_label": "never-return",
                         "mime_type": "video/mp4"}))


async def test_mcp_handshake_and_read_only_discovery(monkeypatch):
    token, _ = await _make_project([VIDEO])
    submit = AsyncMock()
    monkeypatch.setattr("aibroker.routes.video_mcp.submit_video_clip", submit)
    init = _rpc(token, "initialize", {"protocolVersion": "2025-06-18"}).json()
    assert init["result"]["protocolVersion"] == "2025-06-18"
    assert init["result"]["capabilities"] == {"tools": {}}
    assert _rpc(token, "initialize", {"protocolVersion": "2026-07-28"}).json()["result"][
        "protocolVersion"] == "2025-11-25"
    assert _rpc(token, "ping").json() == {"jsonrpc": "2.0", "id": 123, "result": {}}
    listing = _rpc(token, "tools/list")
    assert "Mcp-Session-Id" not in listing.headers
    assert {t["name"] for t in listing.json()["result"]["tools"]} == {
        "video_providers", "video_status", "video_generate"}
    assert listing.headers["Cache-Control"] == "private, no-store"
    providers = _call(token, "video_providers")["result"]
    assert providers["structuredContent"] == {
        "providers": ["gemini"], "billing": "paid_api", "credential": "project_bearer"}
    assert _call(token, "video_providers", {"unexpected": "x"})["result"]["isError"]
    assert client.get(URL, headers=_auth(token)).status_code == 405
    submit.assert_not_awaited()


async def test_requires_private_least_privilege_token():
    token, pid = await _make_project([VIDEO])
    chat, _ = await _make_project(["llm:chat"])
    sim, _ = await _make_project([VIDEO, HEYGEN, "llm:chat"])
    assert _rpc(None, "tools/list").headers["WWW-Authenticate"] == "Bearer"
    assert _rpc("aib_prj_unknown", "tools/list").status_code == 401
    assert _rpc(None, "tools/list", **{"X-Project-Key": token}).status_code == 401
    assert _rpc(None, "tools/list", **{"X-Admin-Key": token}).status_code == 401
    assert _rpc(chat, "tools/list").status_code == 403
    assert _rpc(sim, "tools/list").status_code == 403
    assert _rpc(token, "tools/list", Origin="https://example.test").status_code == 403
    assert _rpc(token, "tools/list", Origin="null").status_code == 403
    async with get_session() as session:
        await session.execute(update(ProjectRow).where(ProjectRow.id == pid).values(self_signup=True))
    assert _rpc(token, "tools/list").status_code == 403
    async with get_session() as session:
        await session.execute(update(ProjectRow).where(ProjectRow.id == pid).values(is_active=False))
    assert _rpc(token, "tools/list").status_code == 401


async def test_stateless_rpc_rejects_malformed_and_never_charges_for_notifications(monkeypatch):
    token, _ = await _make_project([VIDEO])
    submit = AsyncMock()
    monkeypatch.setattr("aibroker.routes.video_mcp.submit_video_clip", submit)
    assert _rpc(token, "ping", **{"MCP-Protocol-Version": "wrong"}).status_code == 400
    assert _rpc(token, "ping", **{"Content-Type": "text/plain"}).status_code == 415
    assert client.post(URL, headers={**_auth(token), "Content-Type": "application/json"},
                       content=" " * (4 * 1024 * 1024 + 1)).status_code == 413
    parse = client.post(URL, headers={**_auth(token), "Content-Type": "application/json"}, content="{")
    assert parse.json()["error"]["code"] == -32700
    assert client.post(URL, headers=_auth(token), json=[]).status_code == 400
    assert client.post(URL, headers=_auth(token), json={"jsonrpc": "2.0", "id": True}).status_code == 400
    assert _rpc(token, "ping", "invalid").json()["error"]["code"] == -32602
    assert _call(token, "not_allowed")["error"]["code"] == -32602
    assert _rpc(token, "unknown").json()["error"]["code"] == -32601
    notified = client.post(URL, headers=_auth(token), json={"jsonrpc": "2.0", "method": "tools/call",
                           "params": {"name": "video_generate", "arguments": ARGS}})
    assert notified.status_code == 202 and not notified.content
    submit.assert_not_awaited()


@pytest.mark.parametrize("arguments", [
    {"confirm_paid_generation": False}, {"confirm_paid_generation": 1}, {"provider": "vivix"},
    {"provider": "heygen"}, {"provider": "gemini", "duration_s": 2}, {"idempotency_key": ""},
    {"image_base64": "not valid"}, {"prompt": " "}, {"extra": "do not echo this input"},
    {"image_base64": "a" * 2_796_204}, {"aspect_ratio": "1:1"},
])
async def test_invalid_tool_args_and_disallowed_provider_cannot_enqueue(arguments):
    token, pid = await _make_project([VIDEO])
    report = _generate(token, **arguments)
    assert report["isError"] and "do not echo this input" not in str(report)
    async with get_session() as session:
        assert (await session.execute(select(DeepJobRow).where(
            DeepJobRow.project_id == pid))).scalar_one_or_none() is None


async def test_paid_approval_must_be_explicit():
    token, _ = await _make_project([VIDEO])
    args = {k: v for k, v in ARGS.items() if k != "confirm_paid_generation"}
    result = _call(token, "video_generate", args)["result"]
    assert result["isError"]


async def test_async_job_replays_one_charge_and_securely_delivers_result():
    token, pid = await _make_project([VIDEO])
    other, _ = await _make_project([VIDEO])
    async with get_session() as session:
        await session.execute(update(ProjectRow).where(ProjectRow.id == pid).values(total_request_cap=1))
    submitted = _generate(token)["structuredContent"]
    job_id = submitted["job_id"]
    assert submitted == {"job_id": job_id, "poll_after_s": 10}
    assert _generate(token)["structuredContent"]["job_id"] == job_id
    assert _generate(token, prompt="changed")["structuredContent"]["status_code"] == 409
    assert _generate(token, idempotency_key="run-2")["structuredContent"]["status_code"] == 429
    async with get_session() as session:
        row = (await session.execute(select(DeepJobRow).where(
            DeepJobRow.project_id == pid))).scalar_one()
        assert row.request["workflow"] == "mcp-video" and row.status == "pending"
        used = (await session.execute(select(ProjectRow.total_requests_used).where(
            ProjectRow.id == pid))).scalar_one()
        assert used == 1
    poll = {"job_id": job_id}
    path = f"{URL}/jobs/{job_id}/result"
    pending = _call(token, "video_status", poll)["result"]["structuredContent"]
    assert pending["status"] == "pending" and pending["poll_after_s"] >= 5
    assert client.get(path, headers=_auth(token)).status_code == 404
    assert _call(other, "video_status", poll)["result"]["isError"]
    video = b"\x00\x00\x00\x18ftypmp42"
    await _finish(job_id, video)
    done = _call(token, "video_status", poll)["result"]
    assert done["structuredContent"]["result_url"].endswith(path)
    assert done["structuredContent"]["provider"] == "gemini"
    assert done["content"][-1]["type"] == "resource_link"
    assert "provider_url" not in str(done) and "never-return" not in str(done)
    assert client.get(f"/v1/jobs/{job_id}", headers={"X-Project-Key": token}).status_code == 404
    assert client.get(path).status_code == 401
    assert client.get(path, headers=_auth(other)).status_code == 404
    assert client.get(path, headers={**_auth(token), "Origin": "https://evil.test"}).status_code == 403
    downloaded = client.get(path, headers=_auth(token))
    assert downloaded.content == video and downloaded.headers["Content-Type"] == "video/mp4"
    assert downloaded.headers["Cache-Control"] == "private, no-store"
    await _finish(job_id, None)
    gone = _call(token, "video_status", poll)["result"]["structuredContent"]
    assert not gone["result_available"] and "result_url" not in gone


async def test_heygen_is_explicit_and_rechecks_provider_scope_on_every_read():
    token, pid = await _make_project([VIDEO, HEYGEN])
    assert _call(token, "video_providers")["result"]["structuredContent"]["providers"] == [
        "gemini", "heygen"]
    args = {"provider": "heygen", "aspect_ratio": "1:1", "resolution": "768p",
            "image_base64": "dGVzdA==", "image_mime": "image/png"}
    heygen = _generate(token, **args)["structuredContent"]["job_id"]
    assert _generate(token, **args)["structuredContent"]["job_id"] == heygen
    poll = {"job_id": heygen}
    path = f"{URL}/jobs/{heygen}/result"
    await _finish(heygen, b"fixture")
    assert _call(token, "video_status", poll)["result"]["structuredContent"]["provider"] == "heygen"
    assert client.get(path, headers=_auth(token)).status_code == 200
    async with get_session() as session:
        await session.execute(update(ProjectRow).where(ProjectRow.id == pid).values(
            allowed_scopes=[VIDEO]))
    assert _call(token, "video_status", poll)["result"]["structuredContent"]["status_code"] == 404
    assert client.get(path, headers=_auth(token)).status_code == 404


async def test_cannot_read_other_broker_workflows_with_video_mcp():
    token, _ = await _make_project([VIDEO])
    claimed_mcp = client.post("/v1/video/jobs", headers={"X-Project-Key": token},
                              json={"prompt": "rest input", "workflow": "mcp-video"})
    assert claimed_mcp.status_code == 400
    ordinary = client.post("/v1/video/jobs", headers={"X-Project-Key": token},
                           json={"prompt": "same project other workflow"}).json()["job_id"]
    assert client.get(f"/v1/jobs/{ordinary}", headers={"X-Project-Key": token}).status_code == 200
    assert _call(token, "video_status", {"job_id": ordinary})["result"]["isError"]
    assert client.get(f"{URL}/jobs/{ordinary}/result", headers=_auth(token)).status_code == 404
