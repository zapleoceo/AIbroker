"""The optional worker socket only signals the existing database queue."""
from __future__ import annotations

import asyncio
import errno
import logging
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


def _boom(code: int):
    async def raiser(*_args, **_kwargs):
        raise OSError(code, os.strerror(code))
    return raiser


async def test_erofs_on_bind_means_polling_only_mode(monkeypatch, tmp_path: Path, caplog):
    tmp_path.chmod(0o700)
    sock = tmp_path / "worker.sock"
    monkeypatch.setitem(rpc.RPC_SOCKETS, "agent:codex", sock)
    monkeypatch.setattr(asyncio, "start_unix_server", _boom(errno.EROFS))
    with caplog.at_level(logging.INFO, logger=rpc.log.name):
        assert await rpc.start_worker_rpc("agent:codex", asyncio.Event()) is None
    lines = [r.getMessage() for r in caplog.records if "polling only" in r.getMessage()]
    assert len(lines) == 1
    assert "agent:codex" in lines[0] and str(sock) in lines[0]
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


async def test_stale_own_socket_unlink_erofs_is_polling_only(monkeypatch, tmp_path: Path, caplog):
    tmp_path.chmod(0o700)
    sock = tmp_path / "worker.sock"
    import socket as socket_mod
    s = socket_mod.socket(socket_mod.AF_UNIX)
    s.bind(str(sock))
    s.close()
    monkeypatch.setitem(rpc.RPC_SOCKETS, "agent:codex", sock)
    real_unlink = Path.unlink

    def ro_unlink(self, *a, **k):
        if self == sock:
            raise OSError(errno.EROFS, os.strerror(errno.EROFS))
        return real_unlink(self, *a, **k)

    monkeypatch.setattr(Path, "unlink", ro_unlink)
    with caplog.at_level(logging.INFO, logger=rpc.log.name):
        assert await rpc.start_worker_rpc("agent:codex", asyncio.Event()) is None
    assert len([r for r in caplog.records if "polling only" in r.getMessage()]) == 1


async def test_non_socket_path_still_refused_even_if_dir_unwritable(monkeypatch, tmp_path: Path):
    tmp_path.chmod(0o700)
    sock = tmp_path / "worker.sock"
    sock.write_text("not a socket")
    monkeypatch.setitem(rpc.RPC_SOCKETS, "agent:codex", sock)
    monkeypatch.setattr(rpc.os, "access", lambda *_a, **_k: False)
    monkeypatch.setattr(asyncio, "start_unix_server", _boom(errno.EROFS))
    with pytest.raises(RuntimeError, match="not an owned socket"):
        await rpc.start_worker_rpc("agent:codex", asyncio.Event())


async def test_unwritable_dir_without_erofs_is_not_polling_only(monkeypatch, tmp_path: Path, caplog):
    tmp_path.chmod(0o700)
    monkeypatch.setitem(rpc.RPC_SOCKETS, "agent:codex", tmp_path / "worker.sock")
    monkeypatch.setattr(rpc.os, "access", lambda *_a, **_k: False)
    monkeypatch.setattr(asyncio, "start_unix_server", _boom(errno.EACCES))
    with pytest.raises(OSError) as exc:
        await rpc.start_worker_rpc("agent:codex", asyncio.Event())
    assert exc.value.errno == errno.EACCES
    assert not [r for r in caplog.records if "polling only" in r.getMessage()]


@pytest.mark.parametrize("code", [errno.EACCES, errno.EADDRINUSE, errno.ENOSPC])
async def test_other_oserrors_still_propagate(monkeypatch, tmp_path: Path, code: int):
    tmp_path.chmod(0o700)
    monkeypatch.setitem(rpc.RPC_SOCKETS, "agent:codex", tmp_path / "worker.sock")
    monkeypatch.setattr(asyncio, "start_unix_server", _boom(code))
    with pytest.raises(OSError) as exc:
        await rpc.start_worker_rpc("agent:codex", asyncio.Event())
    assert exc.value.errno == code


async def test_non_oserror_still_propagates(monkeypatch, tmp_path: Path):
    tmp_path.chmod(0o700)
    monkeypatch.setitem(rpc.RPC_SOCKETS, "agent:codex", tmp_path / "worker.sock")

    async def bad(*_a, **_k):
        raise ValueError("boom")

    monkeypatch.setattr(asyncio, "start_unix_server", bad)
    with pytest.raises(ValueError, match="boom"):
        await rpc.start_worker_rpc("agent:codex", asyncio.Event())
