"""Adapter contract between the executor and a CLI (real or fake)."""
from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from aibroker.agent.events import (
    FileChanged,
    FileRef,
    MessageDelta,
    ModelObserved,
    ModelRerouted,
    RunCompleted,
    RunFailed,
    SessionAssigned,
    ToolCall,
    ToolResult,
    Usage,
)
from aibroker.agent.lock import Lease

BillingMode = Literal["subscription", "api", "unknown"]
StreamEvent = (SessionAssigned | ModelRerouted | ModelObserved | MessageDelta | ToolCall | ToolResult
               | FileChanged | Usage | RunCompleted | RunFailed)


@dataclass(frozen=True)
class AgentRunSpec:
    """Everything an adapter needs to start one run."""
    run_id: int
    capability: str
    prompt: str
    workspace: str
    account_id: str
    session_id: str | None = None          # exact CLI session to resume
    allowed_tools: list[str] = field(default_factory=list)
    model: str | None = None               # EXACT id the caller chose; never substituted
    attachments: list[str] = field(default_factory=list)   # workspace-relative input files
    client_tools: list[dict[str, Any]] = field(default_factory=list)
    tool_choice: str | dict[str, Any] | None = None
    fence_leases: tuple[Lease, ...] = ()


@dataclass(frozen=True)
class ModelInfo:
    """One entry of the model/capability catalog, as reported BY the CLI."""
    id: str
    capabilities: tuple[str, ...] = ()


@dataclass(frozen=True)
class AuthAttestation:
    """The non-secret facts of the auth probe behind `billing_mode()`."""
    auth_method: str
    api_provider: str


class AgentCLI(Protocol):
    """One adapter instance serves ONE run (cancel() targets that run)."""
    supports_resume: bool

    async def billing_mode(self) -> BillingMode:
        """Positively report the auth mode; anything unsure is "unknown" (refused)."""
        ...

    def attestation(self) -> AuthAttestation | None:
        """Auth method / provider of the probe `billing_mode()` just ran; None = no probe result
        (the executor then refuses the run - never a success with "unknown")."""
        ...

    def export_artifacts(self, run: AgentRunSpec, since: float) -> list[FileRef]:
        """After a run, the WORKER (not the model) collects artifacts the CLI produced outside the
        workspace (Codex: generated images) -> ready file refs. No artifacts = []."""
        ...

    async def list_models(self) -> list[ModelInfo] | None:
        """Catalog from the CLI itself (codex app-server model/list + provider/capabilities;
        claude: its own model list). Never hardcoded. None = catalog unavailable."""
        ...

    def start(self, run: AgentRunSpec) -> AsyncIterator[StreamEvent]:
        """Async iterator of events; ends with run.completed / run.failed."""
        ...

    async def cancel(self) -> None:
        """Stop the run (real adapters: SIGTERM, grace, SIGKILL). Idempotent."""
        ...
