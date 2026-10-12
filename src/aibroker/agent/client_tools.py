"""Durable handoff for subscription tool calls; Broker never executes the tool."""
from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select

from aibroker.agent.lock import Lease, check_fence
from aibroker.db import get_session
from aibroker.db.models import AgentRunRow, DeepJobRow
from aibroker.services.tool_contract import validate_result

MAX_CALLS = 16
MAX_ARGUMENT_BYTES = 64 * 1024
MAX_RESULT_BYTES = 64 * 1024
TOOL_WAIT_S = 300


class ClientToolConflict(Exception):
    """A tool call or result is absent, expired, invalid, or no longer current."""


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _digest(success: bool, content: str) -> str:
    return hashlib.sha256(json.dumps([success, content], ensure_ascii=False).encode()).hexdigest()


async def publish_call(run_id: int, call_id: str, name: str, arguments: Any,
                       fences: tuple[Lease, ...] = ()) -> None:
    """Expose a validated App Server callback only on this running subscription job."""
    if not isinstance(call_id, str) or not 1 <= len(call_id) <= 128:
        raise ClientToolConflict("invalid call id")
    raw = json.dumps(arguments, ensure_ascii=False, separators=(",", ":"))
    if len(raw.encode()) > MAX_ARGUMENT_BYTES:
        raise ClientToolConflict("tool arguments exceed limit")
    async with get_session() as s:
        for lease in fences:
            await check_fence(s, lease)
        job = (await s.execute(select(DeepJobRow).where(DeepJobRow.id == run_id)
                               .with_for_update())).scalar_one_or_none()
        if job is None or job.status != "running" or not job.capability == "agent:codex":
            raise ClientToolConflict("job is not running")
        run = await s.get(AgentRunRow, run_id)
        if run is None or run.cancel_requested:
            raise ClientToolConflict("job was cancelled")
        client = (job.request or {}).get("client_job") or {}
        tools = client.get("tools") or []
        call = {"id": call_id, "type": "function",
                "function": {"name": name, "arguments": raw}}
        if validate_result("", {"finish_reason": "tool_calls", "tool_calls": [call]},
                           tools, client.get("tool_choice")):
            raise ClientToolConflict("tool call is outside the declared schema")
        meta = dict(job.result_meta or {})
        history = list(meta.get("client_tool_history") or [])
        if meta.get("client_tool") or len(history) >= MAX_CALLS or any(
            item["call_id"] == call_id for item in history
        ):
            raise ClientToolConflict("tool call already pending or limit reached")
        meta["client_tool"] = {"call_id": call_id, "name": name, "arguments": raw,
                               "deadline": (_now() + timedelta(seconds=TOOL_WAIT_S)).isoformat(),
                               "state": "waiting"}
        job.result_meta = meta


async def submit_result(run_id: int, project_id: int, call_id: str,
                        success: bool, content: str) -> bool:
    """Commit a same-project result once; identical retries return False."""
    if len(content.encode()) > MAX_RESULT_BYTES:
        raise ValueError("tool result exceeds limit")
    digest = _digest(success, content)
    async with get_session() as s:
        job = (await s.execute(select(DeepJobRow).where(
            DeepJobRow.id == run_id, DeepJobRow.project_id == project_id
        ).with_for_update())).scalar_one_or_none()
        if job is None:
            raise LookupError("job not found")
        if job.status != "running" or job.capability != "agent:codex":
            raise ClientToolConflict("job is not waiting for a client tool")
        run = await s.get(AgentRunRow, run_id)
        if run is None or run.cancel_requested:
            raise ClientToolConflict("job was cancelled")
        meta = dict(job.result_meta or {})
        if any(item["call_id"] == call_id and item["digest"] == digest for item in
               meta.get("client_tool_history") or []):
            return False
        call = dict(meta.get("client_tool") or {})
        if call.get("call_id") != call_id:
            raise ClientToolConflict("call id does not match the pending tool")
        if call.get("state") == "ready":
            if call.get("digest") == digest:
                return False
            raise ClientToolConflict("a different result was already submitted")
        if call.get("state") != "waiting" or _now() >= datetime.fromisoformat(call["deadline"]):
            raise ClientToolConflict("tool call expired")
        call.update(state="ready", success=success, content=content, digest=digest)
        meta["client_tool"] = call
        job.result_meta = meta
        return True


async def take_result(run_id: int, call_id: str,
                      fences: tuple[Lease, ...] = ()) -> tuple[bool, str] | None:
    """Worker consumes a committed result and retains only its digest for replay checks."""
    async with get_session() as s:
        for lease in fences:
            await check_fence(s, lease)
        job = (await s.execute(select(DeepJobRow).where(DeepJobRow.id == run_id)
                               .with_for_update())).scalar_one_or_none()
        if job is None or job.status != "running":
            raise ClientToolConflict("job stopped while waiting for a tool")
        run = await s.get(AgentRunRow, run_id)
        if run is None or run.cancel_requested:
            raise ClientToolConflict("job was cancelled")
        meta = dict(job.result_meta or {})
        call = dict(meta.get("client_tool") or {})
        if call.get("call_id") != call_id:
            raise ClientToolConflict("pending tool changed")
        if _now() >= datetime.fromisoformat(call["deadline"]):
            raise ClientToolConflict("tool call expired")
        if call.get("state") != "ready":
            return None
        result = bool(call["success"]), str(call["content"])
        history = list(meta.get("client_tool_history") or [])
        history.append({"call_id": call_id, "digest": call["digest"]})
        meta["client_tool_history"] = history
        meta.pop("client_tool", None)
        job.result_meta = meta
        return result
