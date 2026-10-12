"""Versioned, typed event schema of an agent run (docs/agent-runs.md).

Adapters yield these; the recorder stamps seq/ts and stores them in
agent_run_events. `usage` carries tokens/turns exactly as the CLI reported -
never dollars. Exactly one terminal event ends every run.
"""
from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

EVENT_SCHEMA_VERSION = 1
MAX_TEXT_CHARS = 64_000


class _Event(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal[1] = EVENT_SCHEMA_VERSION


class FileRef(BaseModel):
    """A produced file. `content_b64` is filled by the executor within the caps."""
    model_config = ConfigDict(extra="forbid")
    path: str
    size: int = 0
    content_b64: str | None = None
    skipped: str | None = None   # too_large | cap_reached | unsafe_path | missing
    expires_at: str | None = None   # ISO UTC; past it the content is no longer served
    expired: bool = False
    sha256: str | None = None       # set for exported images
    width: int | None = None
    height: int | None = None


class RunStarted(_Event):
    type: Literal["run.started"] = "run.started"
    capability: str
    account_id: str
    resumed_session_id: str | None = None


class SessionAssigned(_Event):
    type: Literal["session.assigned"] = "session.assigned"
    session_id: str = Field(min_length=1, max_length=128)


class ModelRerouted(_Event):
    type: Literal["model.rerouted"] = "model.rerouted"
    from_model: str
    to_model: str
    source: Literal["codex_app_server"] = "codex_app_server"


class MessageDelta(_Event):
    type: Literal["message.delta"] = "message.delta"
    text: str


class ToolCall(_Event):
    type: Literal["tool.call"] = "tool.call"
    call_id: str
    name: str
    input: dict[str, object] = Field(default_factory=dict)


class ToolResult(_Event):
    type: Literal["tool.result"] = "tool.result"
    call_id: str
    ok: bool = True
    output: str = ""


class FileChanged(_Event):
    type: Literal["file.changed"] = "file.changed"
    path: str
    action: Literal["created", "modified", "deleted"] = "modified"


class Usage(_Event):
    type: Literal["usage"] = "usage"
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    cache_read_tokens: int | None = None
    turns: int | None = None
    reasoning_tokens: int | None = None
    source: str = "cli_reported"   # where the numbers come from; never a price


class RunWaitingQuota(_Event):
    """Non-terminal: the CLI hit its usage limit; the run waits and is retried later on the
    SAME account and billing route (never an API fallback)."""
    type: Literal["run.waiting_quota"] = "run.waiting_quota"
    not_before: str
    attempt: int
    reset_reported: bool = False


class BillingAttested(_Event):
    """What the broker actually checked on THIS run, written after the billing guard passed and
    before the CLI starts. Never written when the guard refuses. No secrets: only the auth
    method / provider the `auth status` probe reported, the setting, and the project cap."""
    type: Literal["billing.attested"] = "billing.attested"
    auth_method: str
    api_provider: str
    paid_continuation_verified_off: bool
    fallback: Literal["none"]            # set by the code path that refuses API fallback
    project_daily_cap_usd: float | None = None


class RunCompleted(_Event):
    type: Literal["run.completed"] = "run.completed"
    final_text: str = ""
    files: list[FileRef] = Field(default_factory=list)


class RunFailed(_Event):
    type: Literal["run.failed"] = "run.failed"
    error_class: str
    message: str = ""
    reset_at: str | None = None   # ISO UTC when a quota limit resets, if the CLI said


class RunCancelled(_Event):
    type: Literal["run.cancelled"] = "run.cancelled"


class RunTimedOut(_Event):
    type: Literal["run.timed_out"] = "run.timed_out"
    limit: Literal["wall", "idle", "deadline"] = "wall"


AnyEvent = (RunStarted | SessionAssigned | ModelRerouted | MessageDelta | ToolCall | ToolResult | FileChanged
            | Usage | RunCompleted | RunFailed | RunCancelled | RunTimedOut | RunWaitingQuota
            | BillingAttested)
AgentEvent = Annotated[AnyEvent, Field(discriminator="type")]
_ADAPTER: TypeAdapter[AgentEvent] = TypeAdapter(AgentEvent)

TERMINAL_TYPES = frozenset({"run.completed", "run.failed", "run.cancelled", "run.timed_out"})
OUTCOME_BY_TYPE = {
    "run.completed": "completed", "run.failed": "failed",
    "run.cancelled": "cancelled", "run.timed_out": "timed_out",
}


def parse_event(data: dict[str, object]) -> AnyEvent:
    """Validate a raw dict into its typed event (ValidationError on unknown type/extra field)."""
    return _ADAPTER.validate_python(data)
