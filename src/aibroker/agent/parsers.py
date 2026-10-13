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
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from aibroker.agent import events as ev
from aibroker.agent.adapter import BillingMode, StreamEvent
from aibroker.agent.billing import BILLING_REFUSED

_QUOTA = re.compile(r"usage limit|quota|limit reached|rate.?limit|too many requests|"
                    r"credit balance|exceeded your|"
                    # Documented Claude Code caps ("You've hit your weekly limit"); a bare
                    # "context limit" / "session limit of 10 MB" is NOT quota.
                    r"hit your (?:weekly|session|5-hour|daily|opus|fable|sonnet) limit|"
                    r"\b(?:weekly|5-hour) limit reached", re.I)
# Model not usable on the subscription account. VERIFIED wordings (Claude Code errors page,
# https://code.claude.com/docs/en/errors.md): "There's an issue with the selected model",
# "Model ... not found", "Model ... is not a recognized model id", "Claude Opus is not available
# with the Claude Pro plan". UNVERIFIED (Codex, no fixture in this repo; recalled wording, keep it
# anchored): "The 'X' model is not supported when using Codex with a ChatGPT account".
# Deliberately NOT matched: bare "not available" / "unavailable" / "limit" text.
_MODEL_UNAVAILABLE = re.compile(
    r"issue with the selected model|"
    r"\bmodel\b[^\n]{0,80}\bis not a recognized model id|"
    r"\bmodel\b[^\n.]{0,80}\bnot found\b|"
    r"\bnot available with the claude \w+ plan|"
    r"\bmodel is not supported when using codex with a chatgpt account", re.I)
_MAX_SCAN = 2000     # classify/parse only the head of a message: bounded work on hostile text
_MONTHS = {m: i for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}
_WEEKDAYS = {d: i for i, d in enumerate(("mon", "tue", "wed", "thu", "fri", "sat", "sun"))}
_RESETS = re.compile(
    r"resets?\s+(?:(?P<mon>[A-Za-z]{3})[a-z]*\.?\s+(?P<day>\d{1,2}),?\s+|"
    r"(?P<wd>[A-Za-z]{3})[a-z]*,?\s+)?"
    r"(?P<hour>\d{1,2})(?::(?P<min>\d{2}))?\s*(?P<ap>[ap]m)\s*(?:\((?P<tz>[^)]{0,300})\))?", re.I)
_TZ_TOKEN = re.compile(r"[A-Za-z_]+(?:/[A-Za-z_+-]+){0,2}")


_SANDBOX = re.compile(
    r"bwrap:|landlock|seccomp|no permissions to create a new namespace|user namespace|"
    r"sandbox (?:could not|couldn't|failed|is unavailable|error|denied to start)|"
    r"failed to (?:apply|create|enter|set up) (?:the )?sandbox", re.IGNORECASE)


def sandbox_failure(text: str) -> bool:
    """True when `text` says the Codex sandbox could not be applied (landlock / seccomp / user
    namespace / bwrap). Such a run fails closed as `sandbox_unavailable` - never retried without
    the sandbox. Wording UNVERIFIED beyond the bwrap message seen on the pinned image."""
    return bool(_SANDBOX.search(text or ""))


def classify_failure(message: str) -> str:
    """quota_exhausted for limit/quota wording (checked first), model_unavailable for a model the
    account cannot use, else cli_error. Never triggers a fallback; only quota is ever waited."""
    head = (message or "")[:_MAX_SCAN]
    if _QUOTA.search(head):
        return "quota_exhausted"
    return "model_unavailable" if _MODEL_UNAVAILABLE.search(head) else "cli_error"


def parse_reset_at(message: str, now: datetime | None = None) -> str | None:
    """When a quota message states the reset time -> ISO UTC, else None. Understands
    'limit reached|<epoch seconds>' and 'in N minutes/hours'. UNVERIFIED wording."""
    now = now or datetime.now(UTC)
    message = (message or "")[:_MAX_SCAN]
    m = re.search(r"\|\s*(\d{10})\b", message or "")
    if m:
        return datetime.fromtimestamp(int(m.group(1)), UTC).isoformat()
    m = re.search(r"in (\d+)\s*(minute|hour|min|hr)s?", message or "", re.I)
    if m:
        step = timedelta(hours=1) if m.group(2).lower().startswith("h") else timedelta(minutes=1)
        return (now + int(m.group(1)) * step).isoformat()
    return _parse_resets_clock(message or "", now)


def _parse_resets_clock(message: str, now: datetime) -> str | None:
    """'resets Oct 15, 8pm (UTC)' / 'resets Mon 12:00am' / 'resets 8:30am (UTC)' -> ISO UTC.
    No date: today if still ahead, else tomorrow. Weekday: next such day at that time. A past
    date means next year. No zone -> UTC; a malformed/unknown zone -> None."""
    m = _RESETS.search(message)
    if not m or not 1 <= int(m["hour"]) <= 12:
        return None
    tzname = (m["tz"] or "UTC").strip()
    if len(tzname) > 64 or not _TZ_TOKEN.fullmatch(tzname):
        return None
    try:
        tz = UTC if tzname.upper() in ("UTC", "GMT", "Z") else ZoneInfo(tzname)
    except (ZoneInfoNotFoundError, ValueError, OSError):   # OSError: "America" is a directory
        return None
    hour = int(m["hour"]) % 12 + (12 if m["ap"].lower() == "pm" else 0)
    minute = int(m["min"] or 0)
    local_now = now.astimezone(tz)
    try:
        if m["mon"]:
            mon = _MONTHS.get(m["mon"].lower())
            if mon is None:
                return None
            at = datetime(local_now.year, mon, int(m["day"]), hour, minute, tzinfo=tz)
            if at < local_now:
                at = at.replace(year=at.year + 1)
        else:
            at = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if m["wd"]:
                wd = _WEEKDAYS.get(m["wd"].lower())
                if wd is None:
                    return None
                at += timedelta(days=(wd - local_now.weekday()) % 7)
            if at < local_now:
                at += timedelta(days=7 if m["wd"] else 1)
    except ValueError:
        return None
    return at.astimezone(UTC).isoformat()


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
                return os.path.relpath(path, self.workspace).replace("\\", "/")
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


def parse_claude_auth_details(text: str) -> tuple[str, str] | None:
    """(authMethod, apiProvider) from `claude auth status` JSON of a logged-in CLI, else None.
    Only these two non-secret fields are read."""
    data = _json(text)
    if data is None or not data.get("loggedIn") or not data.get("authMethod"):
        return None
    return str(data["authMethod"]), str(data.get("apiProvider") or "firstParty")


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


class CodexParser:
    """`codex exec --json` JSONL: thread.started, item.completed (agent_message, command_execution,
    file_change, mcp_tool_call), turn.completed(usage), turn.failed, error. Shapes from the public
    docs (UNVERIFIED against a real run); quota wording is classified by `classify_failure`."""

    def __init__(self) -> None:
        self.final_text = ""
        self.turn_done = False
        self.error: str | None = None
        self._calls = 0

    def feed(self, line: str) -> list[StreamEvent]:
        data = _json(line)
        if data is None:
            return []
        kind = data.get("type")
        if kind == "thread.started" and data.get("thread_id"):
            return [ev.SessionAssigned(session_id=str(data["thread_id"]))]
        if kind == "turn.completed":
            self.turn_done = True
            u = data.get("usage") or {}
            return [ev.Usage(input_tokens=u.get("input_tokens"), output_tokens=u.get("output_tokens"),
                             total_tokens=u.get("total_tokens"),
                             cache_read_tokens=u.get("cached_input_tokens"),
                             reasoning_tokens=u.get("reasoning_output_tokens"), turns=1)]
        if kind == "turn.failed":
            self.error = str((data.get("error") or {}).get("message") or "turn failed")
            return []
        if kind == "error":
            self.error = str(data.get("message") or "error")
            return []
        if kind == "item.completed":
            return self._item(data.get("item") or {})
        return []

    def _item(self, item: dict[str, Any]) -> list[StreamEvent]:
        t = item.get("type")
        if t == "agent_message":
            self.final_text = str(item.get("text") or "")
            return [ev.MessageDelta(text=self.final_text)]
        if t in ("command_execution", "mcp_tool_call"):
            self._calls += 1
            cid = str(item.get("id") or f"call-{self._calls}")
            name = "shell" if t == "command_execution" else str(item.get("tool") or "mcp")
            inp = {"command": item.get("command")} if t == "command_execution" else \
                {"arguments": item.get("arguments")}
            ok = item.get("exit_code", 0) in (0, None) and item.get("status") != "failed"
            out = str(item.get("aggregated_output") or item.get("result") or "")
            events: list[StreamEvent] = [ev.ToolCall(call_id=cid, name=name, input=inp),
                                         ev.ToolResult(call_id=cid, ok=bool(ok), output=out)]
            if not ok and sandbox_failure(out):      # the sandbox could not be applied: stop, fail closed
                events.append(ev.RunFailed(error_class="sandbox_unavailable", message=out[:500]))
            return events
        if t == "file_change":
            kinds = {"add": "created", "update": "modified", "delete": "deleted"}
            return [ev.FileChanged(path=str(c.get("path")), action=kinds.get(c.get("kind"), "modified"))
                    for c in item.get("changes") or [] if c.get("path")]
        return []

    def finish(self, returncode: int, stderr_tail: str = "") -> StreamEvent:
        if self.turn_done and returncode == 0 and self.error is None:
            return ev.RunCompleted(final_text=self.final_text)
        msg = self.error or stderr_tail.strip() or f"codex exited {returncode} without a finished turn"
        if sandbox_failure(msg) or sandbox_failure(stderr_tail):
            return ev.RunFailed(error_class="sandbox_unavailable", message=msg[:2000])
        return ev.RunFailed(error_class=classify_failure(msg), message=msg[:2000],
                            reset_at=parse_reset_at(msg))


def parse_codex_login_status(text: str) -> BillingMode:
    """`codex login status` text -> "subscription" only when it states a ChatGPT login (exact
    wording UNVERIFIED, so anything else is "unknown"); an API-key login is "api"."""
    low = text.lower()
    if "api key" in low or "apikey" in low:
        return "api"
    return "subscription" if "chatgpt" in low and "not logged in" not in low else "unknown"
