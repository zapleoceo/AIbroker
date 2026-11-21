"""Submit-time validation of the inline images in a vision request.

Why this exists (2026-09-12): vera's Telegram ingestor labelled an MP4
animation as `image/jpeg` and the media-worker sent it as a photo. Nothing
in the pipeline could ever decode it — llama-server 400'd, gemini 400'd, the
free tail rate-limited — yet the job walked the whole chain, was re-queued
eight times with backoff, failed, and vera re-submitted it: the same
payload was processed 8 times in 24h, each pass burning a local-vision slot
and cloud quota for a file that will never be an image. Rejecting it at
submit with a 400 is the only honest answer: vera's own retry policy treats
a 400 as permanent and stops resubmitting.

Only `data:` URLs are checked — a remote URL is fetched by the cloud
providers, not by us. Only the header is parsed (Pillow's lazy open), so a
20 MB photo costs microseconds here.
"""
from __future__ import annotations

import base64
import binascii
import io
from typing import Any

from aibroker.providers.media_inputs import (
    PDF_MIME,
    VIDEO_MIMES,
    block_kind,
    data_url_of,
    declared_mime,
)

# Inline video / PDF (2026-11-21, SIM v2): each attachment and all of them together.
# The request body itself is capped at 28 MB by nginx (/v1/jobs), i.e. ~20 MB decoded.
MAX_MEDIA_BYTES = 20 * 1024 * 1024

# A few container signatures that reach us disguised as images. Anything
# else undecodable is reported generically — the point is the 400, not
# forensics.
_KNOWN_NON_IMAGES: tuple[tuple[bytes, int, str], ...] = (
    (b"ftyp", 4, "an MP4/MOV video container"),
    (b"\x1aE\xdf\xa3", 0, "a Matroska/WebM video"),
    (b"OggS", 0, "an Ogg audio/video stream"),
    (b"ID3", 0, "an MP3 audio file"),
    (b"%PDF", 0, "a PDF document"),
)


_HINT = {"an MP4/MOV video container": " — send video as a video_url block",
         "a Matroska/WebM video": " — send video as a video_url block",
         "a PDF document": " — send a PDF as a file block"}


def _what(raw: bytes) -> str:
    for magic, offset, label in _KNOWN_NON_IMAGES:
        if raw[offset:offset + len(magic)] == magic:
            return label
    return "not a decodable image"


def _media_matches(mime: str, raw: bytes) -> bool:
    """The bytes are the container the data URL declares."""
    if mime in ("video/mp4", "video/quicktime"):
        return raw[4:8] == b"ftyp"
    if mime == "video/webm":
        return raw[:4] == b"\x1aE\xdf\xa3"
    return raw[:5] == b"%PDF-"


def inline_media_problem(messages: list[dict[str, Any]]) -> str | None:
    """None when every video_url / file block is an inline video or PDF within the
    limits, else a one-line reason naming the first offender (a 400 body)."""
    n = 0
    total = 0
    for msg in messages:
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            kind = block_kind(block)
            if kind is None:
                continue
            n += 1
            url = data_url_of(block) or ""
            if not url.startswith("data:"):
                return (f"media block #{n}: only inline base64 data URLs are accepted "
                        "(data:<mime>;base64,...)")
            if kind == "unsupported":
                allowed = ", ".join(sorted(VIDEO_MIMES)) + f", {PDF_MIME} (file block only)"
                return (f"media block #{n}: type {declared_mime(url) or 'unknown'!r} is not "
                        f"supported; accepted: {allowed}")
            if "base64," not in url[:120]:
                return f"media block #{n}: data URL is not base64-encoded"
            encoded = url.split("base64,", 1)[1]
            if len(encoded) > 4 * ((MAX_MEDIA_BYTES + 2) // 3):
                return f"media block #{n}: exceeds {MAX_MEDIA_BYTES // (1024 * 1024)} MB"
            try:
                raw = base64.b64decode(encoded, validate=True)
            except (binascii.Error, ValueError):
                return f"media block #{n}: invalid base64"
            if not raw:
                return f"media block #{n}: empty"
            total += len(raw)
            if total > MAX_MEDIA_BYTES:
                return (f"inline video/PDF exceed {MAX_MEDIA_BYTES // (1024 * 1024)} MB "
                        "in total")
            if not _media_matches(declared_mime(url), raw):
                return (f"media block #{n}: the bytes are not {declared_mime(url)} "
                        f"(found {_what(raw)})")
    return None


def _decodable(raw: bytes) -> bool:
    try:
        from PIL import Image
    except ImportError:  # pragma: no cover — Pillow is a hard dependency
        return True
    try:
        Image.open(io.BytesIO(raw))
    except Exception:  # noqa: BLE001 — any Pillow failure means "not an image"
        return False
    return True


def inline_image_problem(messages: list[dict[str, Any]]) -> str | None:
    """None when every inline image can be decoded, else a one-line reason
    naming the first offender — suitable as a 400 body."""
    n = 0
    for msg in messages:
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "image_url":
                continue
            url = (block.get("image_url") or {}).get("url") or ""
            if not url.startswith("data:"):
                continue
            n += 1
            if "base64," not in url:
                return f"inline image #{n}: data URL is not base64-encoded"
            try:
                raw = base64.b64decode(url.split("base64,", 1)[1], validate=False)
            except (binascii.Error, ValueError):
                return f"inline image #{n}: invalid base64"
            if not raw:
                return f"inline image #{n}: empty"
            if not _decodable(raw):
                what = _what(raw)
                return (f"inline image #{n} is {what} — the declared "
                        f"{url[5:url.index(';')] if ';' in url[:64] else 'type'} "
                        "cannot be decoded by any vision provider" + _HINT.get(what, ""))
    return None
