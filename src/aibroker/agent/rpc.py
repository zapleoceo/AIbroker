"""Optional, local-only Unix socket signal from Broker to a subscription worker.

The database remains authoritative. A missing socket merely leaves the existing
one-second polling path in charge of claiming queued jobs.
"""
from __future__ import annotations

import asyncio
import errno
import json
import logging
import os
import stat
from pathlib import Path

import httpx

from aibroker.providers.subscriptions import PROVIDERS, subscription_models

log = logging.getLogger(__name__)

RPC_SOCKETS = {
    "agent:claude": Path("/run/aibroker-agent-rpc/claude/worker.sock"),
    "agent:codex": Path("/run/aibroker-agent-rpc/codex/worker.sock"),
}


async def wake_worker(capability: str, job_id: int) -> None:
    """Best-effort hint after commit; never changes queue correctness or response."""
    path = RPC_SOCKETS.get(capability)
    if path is None or not path.exists():
        return
    try:
        transport = httpx.AsyncHTTPTransport(uds=str(path))
        async with httpx.AsyncClient(transport=transport, timeout=0.25) as client:
            await client.post("http://worker/internal/v1/wake", json={"job_id": job_id})
    except (httpx.HTTPError, OSError):
        log.debug("worker RPC wake unavailable for %s", capability)


def _reply(status: int, payload: dict[str, object]) -> bytes:
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    reason = {200: "OK", 202: "Accepted", 400: "Bad Request", 404: "Not Found"}[status]
    return (f"HTTP/1.1 {status} {reason}\r\nContent-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n").encode() + body


async def _handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
                  capability: str, wake: asyncio.Event) -> None:
    try:
        header = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=2.0)
        if len(header) > 4096:
            raise ValueError("oversize header")
        lines = header.decode("ascii").split("\r\n")
        method, path, version = lines[0].split(" ")
        if version != "HTTP/1.1":
            raise ValueError("unsupported HTTP version")
        lengths = [int(line.split(":", 1)[1].strip()) for line in lines[1:]
                   if line.lower().startswith("content-length:")]
        if len(lengths) > 1 or (lengths and not 0 <= lengths[0] <= 256):
            raise ValueError("invalid body length")
        body = await asyncio.wait_for(reader.readexactly(lengths[0]), timeout=2.0) if lengths else b""
        if method == "GET" and path == "/internal/v1/health":
            response = _reply(200, {"ready_for_queue": True, "capability": capability})
        elif method == "GET" and path == "/internal/v1/provider":
            provider = next(name for name, cap in PROVIDERS.items() if cap == capability)
            candidates = [spec.id for spec in subscription_models()
                          if spec.provider == provider]
            response = _reply(200, {
                "provider": provider, "capability": capability,
                "candidate_models": candidates,
                "job_features": {"text": True, "tools": False, "vision": False},
                "statuses": ["pending", "waiting_quota", "running", "completed", "failed",
                             "cancelled", "timed_out"],
            })
        elif method == "POST" and path == "/internal/v1/wake":
            value = json.loads(body)
            job_id = value.get("job_id") if isinstance(value, dict) else None
            if type(job_id) is not int or job_id <= 0:
                raise ValueError("invalid job_id")
            wake.set()
            response = _reply(202, {"accepted": True})
        else:
            response = _reply(404, {"error": "not_found"})
    except (ValueError, UnicodeError, asyncio.IncompleteReadError,
            asyncio.LimitOverrunError, TimeoutError):
        response = _reply(400, {"error": "bad_request"})
    try:
        writer.write(response)
        await writer.drain()
    finally:
        writer.close()
        await writer.wait_closed()


def _log_polling_only(capability: str, path: Path) -> None:
    log.warning("worker RPC not mounted: capability=%s socket=%s mode=polling only "
                "(RPC dir read-only / not mounted); instant wake is inactive, "
                "database polling claims jobs", capability, path)


async def start_worker_rpc(capability: str, wake: asyncio.Event) -> asyncio.AbstractServer | None:
    """Listen only when the approved worker-only RPC volume is mounted correctly."""
    path = RPC_SOCKETS.get(capability)
    if path is None or not path.parent.is_dir():
        return None
    directory = path.parent.stat()
    if directory.st_uid != os.getuid() or stat.S_IMODE(directory.st_mode) != 0o700:
        raise RuntimeError("worker RPC directory must be owned by worker uid and mode 0700")
    if not os.access(path.parent, os.W_OK):
        _log_polling_only(capability, path)
        return None
    if os.path.lexists(path):
        existing = path.lstat()
        if not stat.S_ISSOCK(existing.st_mode) or existing.st_uid != os.getuid():
            raise RuntimeError("worker RPC path is not an owned socket")
        path.unlink()
    try:
        server = await asyncio.start_unix_server(
            lambda r, w: _handle(r, w, capability, wake), path=str(path), limit=4096)
    except OSError as exc:
        if exc.errno != errno.EROFS:
            raise  # only the expected read-only mount is a mode; everything else stays an error
        _log_polling_only(capability, path)
        return None
    path.chmod(0o600)
    return server


async def stop_worker_rpc(server: asyncio.AbstractServer | None, capability: str) -> None:
    if server is None:
        return
    server.close()
    await server.wait_closed()
    path = RPC_SOCKETS[capability]
    if os.path.lexists(path) and stat.S_ISSOCK(path.lstat().st_mode) and path.lstat().st_uid == os.getuid():
        path.unlink()
