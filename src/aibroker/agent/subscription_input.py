"""Convert universal job image blocks into bounded, temporary CLI attachments."""
from __future__ import annotations

import io
import re
from typing import Any

from PIL import Image, UnidentifiedImageError

from aibroker.agent.attachments import (
    MAX_BYTES,
    MAX_FILES,
    MAX_TOTAL_BYTES,
    decode_attachment,
    validate_attachments,
)

_DATA_URL = re.compile(r"\Adata:(image/(?:png|jpeg|webp|gif));base64,([A-Za-z0-9+/]*={0,2})\Z")
_SUFFIX = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp", "image/gif": "gif"}
_FORMAT = {"image/png": "PNG", "image/jpeg": "JPEG", "image/webp": "WEBP", "image/gif": "GIF"}
MAX_PIXELS = 25_000_000


def subscription_messages(
    messages: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Replace user image blocks with file markers; never put image bytes in the CLI prompt."""
    turns: list[dict[str, Any]] = []
    attachments: list[dict[str, str]] = []
    total_bytes = 0
    for message in messages:
        content = message.get("content")
        if not isinstance(content, list):
            turns.append(message)
            continue
        if message.get("role") != "user":
            raise ValueError("image blocks require a user message")
        parts: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                raise ValueError("invalid image or text block")
            if block.get("type") == "text" and isinstance(block.get("text"), str):
                parts.append(block["text"])
                continue
            if block.get("type") != "image_url" or not isinstance(block.get("image_url"), dict):
                raise ValueError("unsupported subscription message block")
            url = block["image_url"].get("url")
            match = _DATA_URL.fullmatch(url) if isinstance(url, str) else None
            if match is None:
                raise ValueError("subscription images must be embedded base64 data URLs")
            if len(attachments) >= MAX_FILES:
                raise ValueError(f"at most {MAX_FILES} attachments")
            media_type, encoded = match.groups()
            if len(encoded) > 4 * ((MAX_BYTES + 2) // 3):
                raise ValueError(f"image exceeds {MAX_BYTES} bytes")
            number = len(attachments) + 1
            name = f"image_{number}.{_SUFFIX[media_type]}"
            attachment = {"name": name, "media_type": media_type, "data_b64": encoded}
            raw = decode_attachment(attachment)
            total_bytes += len(raw)
            if total_bytes > MAX_TOTAL_BYTES:
                raise ValueError(f"attachments exceed {MAX_TOTAL_BYTES} bytes in total")
            try:
                with Image.open(io.BytesIO(raw)) as image:
                    if image.format != _FORMAT[media_type] or image.width * image.height > MAX_PIXELS:
                        raise ValueError(f"{name}: image format or pixel limit exceeded")
                    if getattr(image, "n_frames", 1) != 1:
                        raise ValueError(f"{name}: animated images are not supported")
                    image.load()
            except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
                raise ValueError(f"{name}: image cannot be fully decoded") from exc
            attachments.append(attachment)
            parts.append(f"[attached image: inputs/{name}]")
        turns.append({**message, "content": "\n".join(parts)})
    validate_attachments(attachments)
    return turns, attachments
