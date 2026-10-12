"""The optional worker socket only signals the existing database queue."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

import httpx
import pytest

from aibroker.agent import rpc

pytestmark = pytest.mark.skipif(os.name == "nt", reason="Unix sockets require Linux")


async def test_worker_rpc_exposes_bounded_metadata_and_wakes_without_job_lookup(
        monkeypatch, tmp_path: Path):
    tmp_path.chmod(0o700)
    socket = tmp_path / "worker.sock"
    monkeypatch.setitem(rpc.RPC_SOCKETS, "agent:codex", socket)
    wake = asyncio.Event()
    server = await rpc.start_worker_rpc("agent:codex", wake)
    try:
        assert socket.stat().st_mode & 0o777 == 0o600
        transport = httpx.AsyncHTTPTransport(uds=str(socket))
        async with httpx.AsyncClient(transport=transport, base_url="http://worker") as client:
            assert (await client.get("/internal/v1/health")).json()["ready_for_queue"]
            metadata = (await client.get("/internal/v1/provider")).json()
            assert metadata["provider"] == "codex-sub"
            assert metadata["job_features"] == {"text": True, "tools": False, "vision": False}
            assert "codex-sub/gpt-6-luna" in metadata["candidate_models"]
            assert (await client.post("/internal/v1/wake", json={"job_id": 987654321})).status_code == 202
            assert wake.is_set()
            assert (await client.post("/internal/v1/wake", json={"job_id": -1})).status_code == 400
            assert (await client.get("/internal/v1/unknown")).status_code == 404
        wake.clear()
        await rpc.wake_worker("agent:codex", 44)
        assert wake.is_set()
    finally:
        await rpc.stop_worker_rpc(server, "agent:codex")
    assert not socket.exists()


async def test_worker_rpc_refuses_permissive_directory(monkeypatch, tmp_path: Path):
    tmp_path.chmod(0o755)
    monkeypatch.setitem(rpc.RPC_SOCKETS, "agent:codex", tmp_path / "worker.sock")
    with pytest.raises(RuntimeError, match="mode 0700"):
        await rpc.start_worker_rpc("agent:codex", asyncio.Event())


async def test_missing_worker_rpc_is_optional(monkeypatch, tmp_path: Path):
    monkeypatch.setitem(rpc.RPC_SOCKETS, "agent:codex", tmp_path / "missing" / "worker.sock")
    assert await rpc.start_worker_rpc("agent:codex", asyncio.Event()) is None
    await rpc.wake_worker("agent:codex", 1)
