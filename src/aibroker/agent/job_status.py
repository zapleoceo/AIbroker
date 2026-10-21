"""Client-facing status details of a Broker job, derived from its queue row alone (no queries).

The poll response used to carry only free text (`error`), no timings and a bare `pending`: a client
could not tell a quota wait from a queue, a worker restart from a CLI error, or where its time
went. These pure functions read what the row already holds:

- `error_code`: a stable, machine-readable reason next to the free-text `error`;
- `job_timing`: `queued_ms` (submit -> first execution start) and `run_ms` (first start -> end);
- `pending_detail`: why a not-yet-finished job waits, and until when.
"""
from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

# error_class (agent RunFailed / runs.py) -> stable code. Classes not listed keep their own name
# when they look like one; anything else is `internal_error`.
_CLASS_CODES = {
    "worker_lost": "worker_restarted",
    "codex_auth_busy": "capacity_busy",
    "session_busy": "capacity_busy",
    "account_unavailable": "capacity_busy",
    "billing_mode_refused": "billing_refused",
}
# (error_class, case-insensitive text in the message) -> a narrower code; first match wins.
_MESSAGE_CODES = (
    ("cli_error", "organization has disabled", "subscription_disabled"),
    ("cli_error", "failed to refresh oauth token", "auth_refresh_failed"),
    ("client_tool_protocol", "tool call expired", "tool_call_expired"),
    ("client_tool_protocol", "required client tool was not called", "tool_not_called"),
)
_CLASS = re.compile(r"^[a-z][a-z_]{1,40}$")
_PREFIX = re.compile(r"^([a-z][a-z_]{1,40}): ")

STATUS_ERROR = "error"
STATUS_CANCELLED = "cancelled"


def error_code(status: str, error_class: str | None, message: str | None) -> str | None:
    """The stable reason of a failed or cancelled job; None while it has not failed. Older rows
    without a stored error_class fall back to the `<class>: ` prefix of the message."""
    if status == STATUS_CANCELLED:
        return "cancelled"
    if status != STATUS_ERROR:
        return None
    text = message or ""
    if not error_class and (m := _PREFIX.match(text)):
        error_class = m.group(1)
    if not error_class or not _CLASS.match(error_class):
        return "internal_error"
    lowered = text.lower()
    for cls, needle, code in _MESSAGE_CODES:
        if cls == error_class and needle in lowered:
            return code
    return _CLASS_CODES.get(error_class, error_class)


def _ms(start: datetime | None, end: datetime | None) -> int | None:
    if start is None or end is None or end < start:
        return None
    return int((end - start).total_seconds() * 1000)


def _parsed(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


def job_timing(created_at: datetime | None, first_started_at: Any, started_at: datetime | None,
               completed_at: datetime | None, now: datetime) -> tuple[int | None, int | None]:
    """(queued_ms, run_ms). The first execution start counts (an agent run's gate requeue or
    quota wait does not reset it); a job without one falls back to the row's `started_at`. While
    nothing has started, queued_ms grows with `now` and run_ms is None."""
    start = _parsed(first_started_at) or started_at
    if start is None:
        return (_ms(created_at, completed_at or now), None)
    return (_ms(created_at, start), _ms(start, completed_at or now))


def pending_detail(status: str, run_after: datetime | None, reason: str | None,
                   tool_waiting: bool, now: datetime) -> tuple[str | None, datetime | None]:
    """(pending_reason, retry_at) for a job that is not finished; (None, None) otherwise.
    pending_reason: queued | waiting_quota | capacity_busy | running | tool_wait."""
    if status in ("done", STATUS_ERROR, STATUS_CANCELLED):
        return (None, None)
    if status == "running":
        return ("tool_wait" if tool_waiting else "running", None)
    if run_after is not None and run_after > now:
        # the DB stores naive UTC; say so on the wire
        at = run_after if run_after.tzinfo else run_after.replace(tzinfo=UTC)
        return (reason if reason in ("waiting_quota", "capacity_busy") else "queued", at)
    return ("queued", None)
