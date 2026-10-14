"""Durable handoff for subscription tool calls; Broker never executes the tool."""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime, timedelta
from itertools import islice
from typing import Any

from jsonschema import Draft202012Validator
from sqlalchemy import select

from aibroker.agent.lock import Lease, check_fence
from aibroker.config import get_settings
from aibroker.db import get_session
from aibroker.db.models import AgentRunRow, DeepJobRow
from aibroker.services.tool_contract import validate_result

log = logging.getLogger(__name__)

MAX_CALLS = 16
MAX_ARGUMENT_BYTES = 64 * 1024
MAX_RESULT_BYTES = 64 * 1024


class ClientToolConflict(Exception):
    """A tool call or result is absent, expired, invalid, or no longer current."""


class ClientToolCancelled(ClientToolConflict):
    """The caller cancelled the run while it waited for a tool result: not a protocol fault."""


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def is_waiting(call: dict[str, Any]) -> bool:
    """True while a call may still be answered; past its deadline submit_result refuses it,
    so the poll response must stop advertising it too."""
    return call.get("state") == "waiting" and _now() < datetime.fromisoformat(call["deadline"])


def _digest(success: bool, content: str) -> str:
    return hashlib.sha256(json.dumps([success, content], ensure_ascii=False).encode()).hexdigest()


_SHOWN_RULES = {"maxLength", "minLength", "maxItems", "minItems", "maximum", "minimum", "type",
                "required", "additionalProperties", "enum", "const", "pattern", "format"}


def _safe_path(schema: dict[str, Any], path: Any) -> str:
    """The instance path with ONLY names the client's schema declares in `properties`; a key the
    model chose (free-form dict, additional/pattern properties) is shown as `*`, an index as-is."""
    cur: Any = schema
    out = []
    for seg in path:
        if isinstance(seg, int):
            out.append(str(seg))
            cur = cur.get("items") if isinstance(cur, dict) else None
        elif isinstance(cur, dict) and isinstance(cur.get("properties"), dict)                 and seg in cur["properties"]:
            out.append(str(seg)[:40])
            cur = cur["properties"][seg]
        else:
            out.append("*")
            cur = cur.get("additionalProperties") if isinstance(cur, dict) else None
    return "/".join(out)[:80] or "<root>"


def schema_violation(tools: list[dict[str, Any]], name: str, arguments: Any) -> str:
    """Which declared-schema rule a tool call broke, WITHOUT any argument value or model-chosen
    key: JSON path (schema-declared names only), the rule, its limit and the observed size/type
    (the first 3 errors - never a full scan of a large argument). '' when it cannot be told."""
    tool = next((t.get("function", {}) for t in tools
                 if t.get("function", {}).get("name") == name), None)
    if tool is None:
        return f"undeclared tool {name[:40]!r}" if isinstance(name, str) else ""
    schema = tool.get("parameters")
    if not isinstance(schema, dict):
        return "tool has no parameters schema"
    try:
        errors = sorted(islice(Draft202012Validator(schema).iter_errors(arguments), 3),
                        key=lambda e: [str(p) for p in e.absolute_path])
    except Exception:  # noqa: BLE001 - diagnosis only; the call is rejected either way
        return ""
    parts = []
    for e in errors:
        path = _safe_path(schema, e.absolute_path)
        rule = e.validator if e.validator in _SHOWN_RULES else "schema"
        sized = rule != "type" and isinstance(e.instance, str | list | dict)
        got = f"len {len(e.instance)}" if sized else f"type {type(e.instance).__name__}"
        limit = "" if rule in ("required", "additionalProperties", "enum", "const", "pattern",
                               "schema") else f" {str(e.validator_value)[:20]}"
        parts.append(f"{path}: {rule}{limit} (got {got})")
    return "; ".join(parts)


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
        label = validate_result("", {"finish_reason": "tool_calls", "tool_calls": [call]},
                                tools, client.get("tool_choice"))
        if label:
            detail = schema_violation(tools, name, arguments)
            # ids, label and rule only - never argument values
            log.warning("client tool call rejected: run=%d call=%r tool=%r label=%s %s",
                        run_id, call_id[:40], str(name)[:40], label, detail)
            raise ClientToolConflict(f"tool call is outside the declared schema [{label}]"
                                     + (f": {detail}" if detail else ""))
        meta = dict(job.result_meta or {})
        history = list(meta.get("client_tool_history") or [])
        if meta.get("client_tool") or len(history) >= MAX_CALLS or any(
            item["call_id"] == call_id for item in history
        ):
            raise ClientToolConflict("tool call already pending or limit reached")
        meta["client_tool"] = {"call_id": call_id, "name": name, "arguments": raw,
                               "deadline": (_now() + timedelta(seconds=get_settings().AGENT_TOOL_WAIT_S)).isoformat(),
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
            raise ClientToolCancelled("job was cancelled")
        meta = dict(job.result_meta or {})
        call = dict(meta.get("client_tool") or {})
        if call.get("call_id") != call_id:
            raise ClientToolConflict("pending tool changed")
        if call.get("state") != "ready":
            # The deadline bounds the wait only: a result the client already got a 202 for is
            # delivered even if the worker's ~1s poll lands just after the deadline.
            if _now() >= datetime.fromisoformat(call["deadline"]):
                raise ClientToolConflict("tool call expired")
            return None
        result = bool(call["success"]), str(call["content"])
        history = list(meta.get("client_tool_history") or [])
        history.append({"call_id": call_id, "digest": call["digest"]})
        meta["client_tool_history"] = history
        meta.pop("client_tool", None)
        job.result_meta = meta
        return result


async def clear_pending_call(run_id: int) -> None:
    """On lease loss, withdraw a still-waiting call so poll stops advertising it and
    submit_result refuses a result nobody will read. A `ready` call is kept: its result was
    already acknowledged with 202 and an identical retry must stay idempotent. A crash needs
    no call here: the terminal write in runs.py replaces result_meta."""
    async with get_session() as s:
        job = (await s.execute(select(DeepJobRow).where(DeepJobRow.id == run_id)
                               .with_for_update())).scalar_one_or_none()
        if job is None or job.status != "running":
            return
        meta = dict(job.result_meta or {})
        if (meta.get("client_tool") or {}).get("state") == "waiting":
            del meta["client_tool"]
            job.result_meta = meta
