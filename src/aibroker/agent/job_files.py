"""Bytes of an agent job's produced files: stored raw in deep_job_files, served as base64.

result_meta.files[i] keeps the metadata plus `content_ref` (the row's idx); readers rebuild the
exact legacy JSON (`content_b64`, or null + `expired` past `expires_at`). Rows written before
migration 025 still carry `content_b64` inline and are served unchanged."""
from __future__ import annotations

import base64
import binascii
import hashlib
import logging
import posixpath
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from aibroker.db import get_session
from aibroker.db.models import DeepJobFileRow

log = logging.getLogger(__name__)

REF_KEY = "content_ref"
# Already-compressed formats only: pglz cannot shrink them (measured 733 vs 723 MB on PNG), so
# they go to a STORAGE EXTERNAL column. Text-like files are small and compressible: stay inline.
_BINARY_MIMES = {".png": "image/png",                                    # measured 2026-10-22: 100% of inline content
                 ".jpg": "image/jpeg", ".jpeg": "image/jpeg",             # assumption: image codecs are
                 ".webp": "image/webp", ".gif": "image/gif"}              # already compressed (not measured)


def _mime(path: Any) -> str:
    """By extension from our own table (mimetypes differs per OS); unknown = octet-stream."""
    ext = posixpath.splitext(str(path or "").lower())[1]
    return _BINARY_MIMES.get(ext, "application/octet-stream")


def _decode_exact(content_b64: str) -> bytes | None:
    """The raw bytes, only when re-encoding gives the identical string (so the served JSON is
    byte-for-byte what was stored inline before); otherwise None and the file stays inline."""
    try:
        data = base64.b64decode(content_b64, validate=True)
    except (binascii.Error, ValueError):
        return None
    return data if base64.b64encode(data).decode() == content_b64 else None


def _naive_utc(iso: str | None) -> datetime | None:
    if not iso:
        return None
    try:
        parsed = datetime.fromisoformat(iso)
    except ValueError:
        return None
    return parsed.astimezone(UTC).replace(tzinfo=None) if parsed.tzinfo else parsed


async def store_files(s: AsyncSession, job_id: int, files: list[dict[str, Any]], *,
                      enabled: bool) -> list[dict[str, Any]]:
    """Move each file's base64 into a deep_job_files row in the caller's transaction and return
    the metadata list for result_meta. `enabled` False returns the files unchanged (inline).
    Idempotent: rows of this job are rewritten, never doubled."""
    if not enabled:
        return files
    await s.execute(delete(DeepJobFileRow).where(DeepJobFileRow.job_id == job_id))
    out: list[dict[str, Any]] = []
    for idx, f in enumerate(files):
        content = f.get("content_b64")
        mime = _mime(f.get("path"))
        data = _decode_exact(content) if content and mime != "application/octet-stream" else None
        if data is None:
            if content and mime != "application/octet-stream":
                log.warning("job %d file %s: content is not canonical base64, kept inline",
                            job_id, f.get("path"))
            out.append(f)
            continue
        digest = hashlib.sha256(data).hexdigest()
        if f.get("sha256") and f["sha256"] != digest:
            log.warning("job %d file %s: event sha256 does not match the bytes, stored the computed one",
                        job_id, f.get("path"))
        s.add(DeepJobFileRow(
            job_id=job_id, idx=idx, sha256=digest, mime=mime,
            size=len(data), expires_at=_naive_utc(f.get("expires_at")), data=data))
        out.append({**{k: v for k, v in f.items() if k != "content_b64"},
                    "content_b64": None, REF_KEY: idx})
    return out


def _expired(f: dict[str, Any], now_iso: str) -> bool:
    return bool(f.get("expires_at")) and f["expires_at"] < now_iso


def _public(f: dict[str, Any], **over: Any) -> dict[str, Any]:
    return {**{k: v for k, v in f.items() if k != REF_KEY}, **over}


def live_files_inline(files: list[dict[str, Any]] | None) -> list[dict[str, Any]] | None:
    """Synchronous legacy view (no DB): inline content served, expired files blanked."""
    if files is None:
        return None
    now = datetime.now(UTC).isoformat()
    return [_public(f, content_b64=None, expired=True) if _expired(f, now) else _public(f)
            for f in files]


async def live_files(job_id: int, files: list[dict[str, Any]] | None) -> list[dict[str, Any]] | None:
    """The files exactly as the API returned them when the bytes were inline."""
    if files is None:
        return None
    now = datetime.now(UTC).isoformat()
    wanted = [f[REF_KEY] for f in files if f.get(REF_KEY) is not None and not _expired(f, now)]
    blobs: dict[int, bytes] = {}
    if wanted:
        async with get_session() as s:
            rows = await s.execute(select(DeepJobFileRow.idx, DeepJobFileRow.data)
                                   .where(DeepJobFileRow.job_id == job_id,
                                          DeepJobFileRow.idx.in_(wanted)))
            blobs = {idx: bytes(data) for idx, data in rows}
    out = []
    for f in files:
        if _expired(f, now):
            out.append(_public(f, content_b64=None, expired=True))
        elif f.get(REF_KEY) is not None:
            data = blobs.get(f[REF_KEY])
            if data is None:
                log.error("job %d file %s: content_ref %s has no deep_job_files row",
                          job_id, f.get("path"), f[REF_KEY])
            out.append(_public(f, content_b64=base64.b64encode(data).decode() if data is not None else None))
        else:
            out.append(_public(f))
    return out
