"""Entry point of the agent-worker container: only the agent lane, no HTTP server and no
LLM job dispatcher. `python -m aibroker.agent.run_worker`."""
from __future__ import annotations

import asyncio
import contextlib
import ctypes
import logging
import signal

from aibroker.agent.rpc import start_worker_rpc, stop_worker_rpc
from aibroker.agent.worker import agent_worker_loop, worker_capabilities
from aibroker.db import close_engine, init_engine

PR_SET_DUMPABLE = 4
log = logging.getLogger(__name__)


def set_non_dumpable(libc: object | None = None) -> bool:
    """prctl(PR_SET_DUMPABLE, 0): /proc/<pid>/environ and memory of this worker become
    unreadable to same-uid processes (the CLI and its tools). The CLI itself is exec'd and
    is dumpable again, as normal. -> True when the call succeeded (False off Linux)."""
    try:
        lib = libc or ctypes.CDLL(None, use_errno=True)
        return lib.prctl(PR_SET_DUMPABLE, 0, 0, 0, 0) == 0   # type: ignore[attr-defined]
    except (OSError, AttributeError):
        return False


async def run_worker() -> None:  # pragma: no cover - process harness
    if not set_non_dumpable():
        raise RuntimeError("could not make the worker non-dumpable (PR_SET_DUMPABLE)")
    await init_engine()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)
    caps = worker_capabilities()
    wake = asyncio.Event()
    rpc = None
    if len(caps) == 1:
        try:
            rpc = await start_worker_rpc(caps[0], wake)
        except (OSError, RuntimeError):
            log.exception("worker RPC unavailable; database polling remains active")
    try:
        await agent_worker_loop(stop, wake)
    finally:
        if len(caps) == 1:
            await stop_worker_rpc(rpc, caps[0])
        await close_engine()


if __name__ == "__main__":  # pragma: no cover
    asyncio.run(run_worker())
