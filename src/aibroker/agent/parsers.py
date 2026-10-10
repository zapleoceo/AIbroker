"""Parser for the Claude Code CLI's JSON-lines output -> our typed events.

UNVERIFIED against the real CLIs (phase 4): the shapes below are written from the
public docs (Claude Code `-p --output-format stream-json`)
and must be re-checked against real output before the route is called ready.
"""
from __future__ import annotations

import json
import os
import re
from datetime import UTC, datetime, timedelta
from typing import Any

from aibroker.agent import events as ev
from aibroker.agent.adapter import BillingMode, StreamEvent
from aibroker.agent.billing import BILLING_REFUSED

_QUOTA = re.compile(r"usage limit|quota|limit reached|rate.?limit|too many requests|"
                    r"credit balance|exceeded your", re.I)


def classify_failure(message: str) -> str:
    """quota_exhausted for limit/quota wording, else cli_error. Never triggers a fallback."""
    return "quota_exhausted" if _QUOTA.search(message or "") else "cli_error"


def parse_reset_at(message: str, now: datetime | None = None) -> str | None:
    """When a quota message states the reset time -> ISO UTC, else None. Understands
    'limit reached|<epoch seconds>' and 'in N minutes/hours'. UNVERIFIED wording."""
    now = now or datetime.now(UTC)
    m = re.search(r"\|\s*(\d{10})\b", message or "")
    if m:
        return datetime.fromtimestamp(int(m.group(1)), UTC).isoformat()
    m = re.search(r"in (\d+)\s*(minute|hour|min|hr)s?", message or "", re.I)
    if m:
        step = timedelta(hours=1) if m.group(2).lower().startswith("h") else timedelta(minutes=1)
        return (now + int(m.group(1)) * step).isoformat()
    return None


def _json(line: str) -> dict[str, Any] | None:
    line = line.strip()
    if not line.startswith("{"):
        return None
    try:
        data = json.loads(line)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


_FILE_TOOLS = {"Write": "created", "Edit": "modified", "MultiEdit": "modified",
               "NotebookEdit": "modified"}


class ClaudeParser:
    """`claude -p --output-format stream-json --verbose` messages: system/init, assistant,
    user (tool_result), result."""

    def __init__(self, workspace: str = "") -> None:
        self.workspace = workspace

    def _rel(self, path: str) -> str:
        if self.workspace and os.path.isabs(path):
            try:
                return os.path.relpath(path, self.workspace)
            except ValueError:
                return path
        return path

    def feed(self, line: str) -> list[StreamEvent]:
        data = _json(line)
        if data is None:
            return []
        kind = data.get("type")
        if kind == "system" and data.get("subtype") == "init":
            src = data.get("apiKeySource")
            if src not in (None, "none"):   # runtime double-check of the billing route
                return [ev.RunFailed(error_class=BILLING_REFUSED,
                                     message=f"CLI reports apiKeySource={src!r}, not subscription")]
            return [ev.SessionAssigned(session_id=str(data["session_id"]))] \
                if data.get("session_id") else []
        if kind == "assistant":
            out: list[StreamEvent] = []
            for block in (data.get("message") or {}).get("content") or []:
                if block.get("type") == "text" and block.get("text"):
                    out.append(ev.MessageDelta(text=str(block["text"])))
                elif block.get("type") == "tool_use":
                    inp = block.get("input") or {}
                    out.append(ev.ToolCall(call_id=str(block.get("id")), name=str(block.get("name")),
                                           input=inp))
                    if block.get("name") in _FILE_TOOLS and inp.get("file_path"):
                        out.append(ev.FileChanged(path=self._rel(str(inp["file_path"])),
                                                  action=_FILE_TOOLS[block["name"]]))
            return out
        if kind == "user":
            content = (data.get("message") or {}).get("content")
            return [ev.ToolResult(call_id=str(b.get("tool_use_id")), ok=not b.get("is_error"),
                                  output=_text(b.get("content")))
                    for b in content or [] if isinstance(b, dict) and b.get("type") == "tool_result"]
        if kind == "result":
            u = data.get("usage") or {}
            out = [ev.Usage(input_tokens=u.get("input_tokens"), output_tokens=u.get("output_tokens"),
                            cache_read_tokens=u.get("cache_read_input_tokens"),
                            turns=data.get("num_turns"))]
            text = str(data.get("result") or "")
            if data.get("is_error") or str(data.get("subtype", "success")).startswith("error"):
                msg = text or str(data.get("subtype"))
                out.append(ev.RunFailed(error_class=classify_failure(msg), message=msg[:2000],
                                        reset_at=parse_reset_at(msg)))
            else:
                out.append(ev.RunCompleted(final_text=text))
            return out
        return []

    def finish(self, returncode: int, stderr_tail: str = "") -> StreamEvent:
        msg = stderr_tail.strip() or f"claude exited {returncode} without a result"
        return ev.RunFailed(error_class=classify_failure(msg), message=msg[:2000],
                            reset_at=parse_reset_at(msg))


def _text(content: Any) -> str:
    if isinstance(content, list):
        return "".join(str(b.get("text", "")) for b in content if isinstance(b, dict))
    return str(content or "")


def parse_claude_auth_status(text: str) -> BillingMode:
    """`claude auth status` JSON -> subscription only for a claude.ai login on first party."""
    data = _json(text)
    if data is None or not data.get("loggedIn"):
        return "unknown"
    method, provider = data.get("authMethod"), data.get("apiProvider") or "firstParty"
    if method == "claude.ai" and provider == "firstParty":
        return "subscription"
    return "api" if method in ("api_key", "console", "oauth_token") or provider != "firstParty" \
        else "unknown"
