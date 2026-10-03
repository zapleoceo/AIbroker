"""Request trace id — groups every provider attempt of one client request.

A contextvar set at the edge (ASGI middleware for sync endpoints, the job
dispatcher for queued jobs) and read when an attempt row is written, so every
`usage_log` row of a request's fallback trail shares one `request_id`. The same
id goes back to the client in the `X-Request-Id` response header.

Stdlib-only leaf module (importable from anywhere, including db/routing code).
"""
from __future__ import annotations

import re
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

REQUEST_ID_HEADER = "X-Request-Id"

_request_id: ContextVar[str | None] = ContextVar("aibroker_request_id", default=None)

# A client-supplied id is honoured only when it is short and boring — it ends up in
# a DB column and a response header.
_SAFE_ID = re.compile(r"[A-Za-z0-9._\-]{8,64}")


def new_request_id() -> str:
    return uuid.uuid4().hex


def job_request_id(job_id: int) -> str:
    """Deterministic id for a queued job: every dispatcher retry of the same job
    shares it, so the whole retry history reads as one request."""
    return f"job-{job_id}"


def sanitize_request_id(candidate: str | None) -> str | None:
    return candidate if candidate and _SAFE_ID.fullmatch(candidate) else None


def current_request_id() -> str | None:
    return _request_id.get()


@contextmanager
def request_scope(request_id: str | None) -> Iterator[str | None]:
    """Bind `request_id` for the duration of the block."""
    token = _request_id.set(request_id)
    try:
        yield request_id
    finally:
        _request_id.reset(token)
