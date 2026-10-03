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


def _what(raw: bytes) -> str:
    for magic, offset, label in _KNOWN_NON_IMAGES:
        if raw[offset:offset + len(magic)] == magic:
            return label
    return "not a decodable image"


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
                return (f"inline image #{n} is {_what(raw)} — the declared "
                        f"{url[5:url.index(';')] if ';' in url[:64] else 'type'} "
                        "cannot be decoded by any vision provider")
    return None
