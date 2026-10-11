"""Scripted AgentCLI for tests - no process, no network, no credentials."""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

from aibroker.agent.adapter import (
    AgentRunSpec,
    AuthAttestation,
    BillingMode,
    ModelInfo,
    StreamEvent,
)


class FakeAgentCLI:
    """Plays `script`: events are yielded; ("sleep", s), ("hang",) block;
    ("write", relpath, text) creates a file in the workspace; ("raise", msg) fails."""
    supports_resume = True

    def __init__(self, script: list[object], mode: BillingMode = "subscription",
                 models: list[ModelInfo] | None = None) -> None:
        self.script = script
        self.models = models
        self.mode: BillingMode = mode
        self.auth: AuthAttestation | None = AuthAttestation("claude.ai", "firstParty")
        self.cancel_calls = 0
        self.started: AgentRunSpec | None = None
        self._cancelled = asyncio.Event()

    async def billing_mode(self) -> BillingMode:
        return self.mode

    def attestation(self) -> AuthAttestation | None:
        return self.auth if self.mode == "subscription" else None

    async def list_models(self) -> list[ModelInfo] | None:
        return self.models

    async def cancel(self) -> None:
        self.cancel_calls += 1
        self._cancelled.set()

    async def start(self, run: AgentRunSpec) -> AsyncIterator[StreamEvent]:
        self.started = run
        for step in self.script:
            if isinstance(step, tuple):
                kind = step[0]
                if kind == "sleep":
                    await asyncio.sleep(float(step[1]))
                elif kind == "hang":
                    await self._cancelled.wait()
                    return
                elif kind == "write":
                    target = Path(run.workspace) / str(step[1])
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(str(step[2]), encoding="utf-8")
                elif kind == "raise":
                    raise RuntimeError(str(step[1]))
                continue
            yield step  # type: ignore[misc]
