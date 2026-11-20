"""Minimal project-scoped receipt view for keyed async transcription."""
from __future__ import annotations

from typing import NamedTuple

from sqlalchemy import LargeBinary, case, cast, func, select

from aibroker.db import get_session
from aibroker.db.models import DeepJobRow

# Exact outbound UTF-8 bytes, evaluated in the DB before loading text.
_TRANSCRIPT_MAX_BYTES = 256 * 1024


class _TranscriptionRow(NamedTuple):
    id: int
    state: str
    text: str | None
    too_large: bool
    provider: str | None
    model: str | None
    model_served: str | None
    cost_usd: float | None
    latency_ms: int | None


async def _read_transcription(
    project_id: int, *, job_id: int | None = None, key: str | None = None,
) -> _TranscriptionRow | None:
    if (job_id is None) == (key is None):
        raise ValueError("expected exactly one typed ASR locator")
    row = DeepJobRow
    done = row.status == "done"
    # Never load raw audio, provider error, or full result JSON through a typed read.
    locator = row.id == job_id if job_id is not None else row.idempotency_key == key
    async with get_session() as session:
        size = (func.length(cast(row.result_text, LargeBinary))
                if session.get_bind().dialect.name == "sqlite"
                else func.octet_length(row.result_text))
        hit = (await session.execute(select(
            row.id, row.status,
            case((done & (size <= _TRANSCRIPT_MAX_BYTES), row.result_text), else_=None),
            case((done & (size > _TRANSCRIPT_MAX_BYTES), True), else_=False),
            row.result_meta["provider"].as_string(),
            row.result_meta["model"].as_string(),
            row.result_meta["model_served"].as_string(),
            row.result_meta["cost_usd"].as_float(),
            row.result_meta["latency_ms"].as_integer(),
        ).where(
            row.project_id == project_id, row.capability == "transcription",
            row.idempotency_key.is_not(None), locator,
        ))).one_or_none()
    return _TranscriptionRow(*hit) if hit is not None else None
