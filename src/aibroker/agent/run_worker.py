"""Entry point of the agent-worker container: only the agent lane, no HTTP server and no
LLM job dispatcher. `python -m aibroker.agent.run_worker`."""
from __future__ import annotations

import asyncio
import contextlib
import signal

from aibroker.agent.worker import agent_worker_loop
from aibroker.db import close_engine, init_engine


async def run_worker() -> None:  # pragma: no cover - process harness
    await init_engine()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)
    try:
        await agent_worker_loop(stop)
    finally:
        await close_engine()


if __name__ == "__main__":  # pragma: no cover
    asyncio.run(run_worker())
