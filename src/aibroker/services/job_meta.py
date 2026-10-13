"""Client-supplied job metadata: trace id, labels, idempotency key (docs/api.md).

One validator for body fields and headers, so both reject the same values.
"""
from __future__ import annotations

import re

MAX_TOKEN_LEN = 128
MAX_LABELS = 8
_TOKEN = re.compile(r"[A-Za-z0-9._:-]+")


def check_token(value: object, what: str) -> str:
    if not isinstance(value, str) or not value or len(value) > MAX_TOKEN_LEN \
            or not _TOKEN.fullmatch(value):
        raise ValueError(f"{what} must be 1-{MAX_TOKEN_LEN} chars of [A-Za-z0-9._:-]")
    return value


def check_labels(labels: dict[str, str] | None) -> dict[str, str] | None:
    if labels is None:
        return None
    if len(labels) > MAX_LABELS:
        raise ValueError(f"labels allows at most {MAX_LABELS} keys")
    return {check_token(k, "label key"): check_token(v, f"label {k!r} value")
            for k, v in labels.items()}


def resolve_trace_id(body_trace_id: str | None, header_trace_id: str | None) -> str | None:
    """Body wins over the X-Trace-Id header; a header is validated only when it is used."""
    if body_trace_id is not None:
        return body_trace_id
    return None if header_trace_id is None else check_token(header_trace_id, "X-Trace-Id")


def log_tag(trace_id: str | None, labels: dict[str, str] | None) -> str:
    """` trace_id=... labels={...}` suffix for job log lines; empty when the job has neither."""
    parts = ([f"trace_id={trace_id}"] if trace_id else []) + ([f"labels={labels}"] if labels else [])
    return (" " + " ".join(parts)) if parts else ""
